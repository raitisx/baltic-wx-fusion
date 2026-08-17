"""The 1:1 fused map: the meteogram's own multi-model blend, rendered.

The meteogram fuses five models (ICON, MEPS, IFS, GFS, UKMO) at a point with
skill weights; this renders that SAME fusion across a grid, so the map and the
meteogram agree instead of the map jumping from MEPS to GFS at +66 h. The blend
is sampled on the station network plus the background fill grid (grid_fill),
fused per hour with the weights from wx.model_weights, interpolated to the 1 km
EPSG:3059 grid, sharpened by the static geography (downscale), and drawn with
the very same cloud/rain/ceiling/fog functions the MEPS map uses.

Inputs, all already produced elsewhere:
  * wx.forecasts_h            station forecasts (hot tier, all five models)
  * maps/grid_forecast/*.parquet  the background fill grid (jobs/run_grid_fill)
  * wx.model_weights          the skill weights the meteogram blends with
  * maps/static/static_3059.npz   the geography rasters (jobs/build_static_grid)
"""
from __future__ import annotations

import datetime as dt
import io
import json
import logging
import os
import tempfile

import numpy as np

from . import config, proj3059 as P
from .maps_meps import (BORDERS_FILE, THRESHOLDS, _cloud_cmap, _draw_clouds,
                        _draw_rain, rain_cmaps, r2_client)

log = logging.getLogger(__name__)

MODELS = ["icon", "meps", "ifs", "gfs", "ukmo"]
# The fields the map draws (and the moisture/cloud they are derived from). No
# wind — the fused map has no arrows.
MAP_PARAMS = ["t2m", "td2m", "rh", "prcp_1h", "cc_low", "cc_mid", "cc_total",
              "cb", "cb_lcl"]
GRID_PARQUET = "maps/grid_forecast/latest.parquet"
STATIC_NPZ = "maps/static/static_3059.npz"

# Fog, same rule as the ingest: saturated at the surface.
FOG_RH = 97.0
FOG_DEP = 0.3


def lead_bin(h: int) -> str:
    return ("0-6" if h < 6 else "6-12" if h < 12 else "12-24" if h < 24
            else "24-48" if h < 48 else "48-72" if h < 72 else "72+")


# --- inputs -----------------------------------------------------------------
def load_weights(conn) -> dict:
    """{(model, parameter, lead_bin): weight} — the meteogram's skill weights."""
    w = {}
    with conn.cursor() as cur:
        cur.execute("select model, parameter, lead_bin, weight from wx.model_weights")
        for m, p, lb, wt in cur.fetchall():
            w[(m, p, lb)] = float(wt)
    log.info("weights: %d rows", len(w))
    return w


def _station_rows(conn):
    """Latest run per model from the hot tier, forecast hours only.

    Returns list of (model, run_time, valid_time, point_id, {param: value}) and
    a {point_id: (lat, lon)} map."""
    cols = ", ".join(MAP_PARAMS)
    with conn.cursor() as cur:
        cur.execute(f"""
            with latest as (
              select model, max(run_time) run_time from wx.forecasts_h group by model)
            select f.model, f.run_time, f.valid_time, f.point_id, {cols}
            from wx.forecasts_h f join latest using (model, run_time)
            where f.valid_time >= now() - interval '1 hour'
        """)
        rows = cur.fetchall()
        names = [d.name for d in cur.description]
        cur.execute("select point_id, lat, lon from wx.points where active and lat is not null")
        pts = {pid: (la, lo) for pid, la, lo in cur.fetchall()}
    out = []
    for r in rows:
        d = dict(zip(names, r))
        out.append((d["model"], d["run_time"], d["valid_time"], d["point_id"],
                    {p: d[p] for p in MAP_PARAMS}))
    log.info("station rows: %d over %d points", len(out), len(pts))
    return out, pts


def _grid_rows(s3):
    """The background fill grid from R2 Parquet: same shape as station rows."""
    import pyarrow.parquet as pq
    try:
        body = s3.get_object(Bucket=config.R2_BUCKET, Key=GRID_PARQUET)["Body"].read()
    except Exception as e:
        log.warning("grid parquet unreadable (%s) — stations only", type(e).__name__)
        return [], {}
    t = pq.read_table(io.BytesIO(body)).to_pydict()
    n = len(t["model"])
    have = [p for p in MAP_PARAMS if p in t]
    out, pts = [], {}
    for i in range(n):
        pid = t["point_id"][i]
        out.append((t["model"][i], t["run_time"][i], t["valid_time"][i], pid,
                    {p: t[p][i] for p in have}))
        if pid not in pts:
            la, lo = pid.split(":")[1:]
            pts[pid] = (float(la), float(lo))
    log.info("grid rows: %d over %d points", len(out), len(pts))
    return out, pts


# --- fusion (a faithful port of the meteogram's fuse()) ---------------------
def fuse(rows, weights):
    """Per (valid_time, point_id), the skill-weighted blend of the models.

    Returns {valid_time: {point_id: {param: value, 'fog': bool}}}. Mirrors the
    page: weighted means for the continuous fields, the ceiling 'vote' (a
    ceiling exists only if enough weight says so, then average those that have
    one), tcc from low+mid under random overlap, fog from surface saturation."""
    # index: param -> valid -> point -> model -> value ; and run per (model,valid,point)
    idx: dict = {}
    runat: dict = {}
    for model, run, valid, pid, vals in rows:
        runat[(model, valid, pid)] = run
        for p, v in vals.items():
            if v is None:
                continue
            idx.setdefault(p, {}).setdefault(valid, {}).setdefault(pid, {})[model] = float(v)

    def w_for(model, param, valid, pid):
        run = runat.get((model, valid, pid), valid)
        lead = max(0, round((valid - run).total_seconds() / 3600))
        return weights.get((model, param, lead_bin(lead)), 1.0)

    valids = sorted({v for p in idx.values() for v in p})
    out: dict = {}
    for valid in valids:
        per_point: dict = {}
        # gather the union of points seen this hour
        pids = set()
        for p in ("t2m", "prcp_1h", "cc_low"):
            pids |= set(idx.get(p, {}).get(valid, {}).keys())
        for pid in pids:
            row = {}
            for p in ("t2m", "td2m", "rh", "prcp_1h", "cc_low", "cc_mid"):
                per = idx.get(p, {}).get(valid, {}).get(pid, {})
                if not per:
                    continue
                num = den = 0.0
                for m, v in per.items():
                    wt = w_for(m, p, valid, pid)
                    num += wt * v
                    den += wt
                if den:
                    row[p] = num / den
            # total cloud from low+mid under random overlap
            lo = row.get("cc_low")
            mi = row.get("cc_mid")
            if lo is not None or mi is not None:
                lo = (lo or 0) / 100.0
                mi = (mi or 0) / 100.0
                row["cc_total"] = (1 - (1 - lo) * (1 - mi)) * 100.0
            # ceiling vote
            cbper = idx.get("cb", {}).get(valid, {}).get(pid, {})
            lclper = idx.get("cb_lcl", {}).get(valid, {}).get(pid, {})
            wtot = wceil = 0.0
            withc = []
            for m in MODELS:
                if m not in lclper:            # model reported at all?
                    continue
                wt = w_for(m, "cb", valid, pid)
                wtot += wt
                if m in cbper:
                    wceil += wt
                    withc.append((cbper[m], wt))
            if wtot and wceil / wtot >= 0.5 and withc:
                row["cb"] = sum(c * w for c, w in withc) / sum(w for _, w in withc)
            else:
                row["cb"] = None
            # fog from surface saturation of the blended moisture
            t = row.get("t2m")
            td = row.get("td2m")
            rh = row.get("rh")
            row["fog"] = bool((rh is not None and rh >= FOG_RH)
                              or (t is not None and td is not None and (t - td) <= FOG_DEP))
            per_point[pid] = row
        out[valid] = per_point
    log.info("fused %d hours", len(out))
    return out


# --- interpolate the fused points onto the 1 km grid ------------------------
NO_CEIL = 6000.0    # metres — "no ceiling" sentinel so gaps between ceilinged
#                     points blend high, not into a spurious low ceiling.


def _grid_xy():
    xs = P.X0 + (np.arange(P.W) + 0.5) / P.W * (P.X1 - P.X0)
    ys = P.Y1 - (np.arange(P.H) + 0.5) / P.H * (P.Y1 - P.Y0)
    return np.meshgrid(xs, ys)


def interp_fields(fused, points):
    """Fused point values -> {valid: {field: (H,W) array}} on the 1 km grid.

    One Delaunay triangulation of the points is reused across every hour and
    field; gaps outside the hull are filled nearest so the sea edges are not
    holes."""
    from scipy.spatial import Delaunay
    from scipy.interpolate import LinearNDInterpolator, NearestNDInterpolator

    gx, gy = _grid_xy()
    out: dict = {}
    for valid, per_point in fused.items():
        pids = list(per_point.keys())
        if len(pids) < 4:
            continue
        la = np.array([points[p][0] for p in pids])
        lo = np.array([points[p][1] for p in pids])
        px, py = P.to_xy(lo, la)
        pxy = np.column_stack([np.asarray(px), np.asarray(py)])
        try:
            tri = Delaunay(pxy)
        except Exception:
            continue

        def grid_of(field, default):
            v = np.array([per_point[p].get(field, default) for p in pids], float)
            v = np.where(np.isfinite(v), v, default)
            g = LinearNDInterpolator(tri, v)(gx, gy)
            if np.isnan(g).any():
                g = np.where(np.isnan(g), NearestNDInterpolator(pxy, v)(gx, gy), g)
            return g

        cbv = np.array([(per_point[p].get("cb") if per_point[p].get("cb") is not None
                         else NO_CEIL) for p in pids], float)
        cbg = LinearNDInterpolator(tri, cbv)(gx, gy)
        if np.isnan(cbg).any():
            cbg = np.where(np.isnan(cbg), NearestNDInterpolator(pxy, cbv)(gx, gy), cbg)

        out[valid] = {
            "t2m": grid_of("t2m", 0.0),
            "td2m": grid_of("td2m", 0.0),
            "prcp": grid_of("prcp_1h", 0.0),
            "tcc": grid_of("cc_total", 0.0),
            "cb": cbg,
            "fog": grid_of("fog", 0.0) >= 0.5,
        }
    return out


# --- downscale with the static geography ------------------------------------
LAPSE_K_PER_M = -6.5e-3       # environmental lapse for the elevation fine-structure


def load_static(s3):
    try:
        body = s3.get_object(Bucket=config.R2_BUCKET, Key=STATIC_NPZ)["Body"].read()
    except Exception as e:
        log.warning("static grid unreadable (%s) — no downscaling", type(e).__name__)
        return None
    z = np.load(io.BytesIO(body))
    return {k: z[k] for k in z.files}


def downscale(fields, static):
    """Sharpen the interpolated blend with geography.

    v1: elevation fine-structure. The interpolated blend carries the coarse
    (~15-25 km) terrain the models resolve; TPI is height above that local mean,
    so a lapse applied to TPI cools ridges and warms hollows at 1 km, and — via
    the same shifted temperature — moves the rain/snow line and the fog line to
    follow the ground. Dew point is shifted with temperature so the saturation
    gap is preserved. Coastal/SST and marine-layer rules come next."""
    if static is None:
        return fields
    tpi = static["tpi_m"].astype(np.float32)
    dt2 = (LAPSE_K_PER_M * tpi).astype(np.float32)   # <0 on hills, >0 in hollows
    land = static["land"]
    for valid, f in fields.items():
        t = f["t2m"] + np.where(land, dt2, 0.0)
        td = f["td2m"] + np.where(land, dt2, 0.0)
        f["t2m"] = t
        f["td2m"] = td
        # fog line follows the sharpened saturation
        f["fog"] = f["fog"] | (land & ((f["t2m"] - f["td2m"]) <= FOG_DEP))
    return fields


# --- render (mirrors maps_meps.render_run) ----------------------------------
def render_run() -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap
    from PIL import Image as PILImage
    from . import db

    conn = db.connect()
    weights = load_weights(conn)
    srows, spts = _station_rows(conn)
    conn.close()
    s3 = r2_client()
    grows, gpts = _grid_rows(s3)
    rows = srows + grows
    points = {**gpts, **spts}
    if not rows:
        log.error("fused map: no forecast rows — nothing to render")
        return

    run_tag = max(r[1] for r in rows).astimezone(dt.timezone.utc).strftime("%Y%m%dT%H")
    fused = fuse(rows, weights)
    fields = interp_fields(fused, points)
    fields = downscale(fields, load_static(s3))

    greens, blues, rain_norm = rain_cmaps()
    pink = LinearSegmentedColormap.from_list("p", ["#f3b8b4", "#f3b8b4"])
    orange = LinearSegmentedColormap.from_list("o", ["#e8a33d", "#e8a33d"])
    grey = _cloud_cmap()
    gx, gy = _grid_xy()
    borders_xy = [[P.to_xy([px for px, _ in line], [py for _, py in line])
                   for line in c["lines"]]
                  for c in json.load(open(BORDERS_FILE))]

    def _draw_borders(ax):
        for country in borders_xy:
            for bx, by in country:
                ax.plot(bx, by, color="#f7f1e2", lw=2.2, zorder=9, solid_capstyle="round")
                ax.plot(bx, by, color="#55503f", lw=0.9, zorder=10, solid_capstyle="round")

    def blank():
        fig = plt.figure(figsize=(P.W / 100, P.H / 100), dpi=100)
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_facecolor("#fbf3de")
        ax.set_xlim(P.X0, P.X1); ax.set_ylim(P.Y0, P.Y1)
        ax.set_aspect("equal"); ax.axis("off")
        return fig, ax

    def render_frame(f, thr):
        fig, ax = blank()
        _draw_clouds(ax, gx, gy, f["tcc"] / 100.0, grey)
        below = np.where((f["cb"] < thr) & ~f["fog"], 1.0, np.nan)
        ax.pcolormesh(gx, gy, below, cmap=pink, vmin=0, vmax=1, alpha=0.75, shading="auto")
        ax.pcolormesh(gx, gy, np.where(f["fog"], 1.0, np.nan), cmap=orange,
                      vmin=0, vmax=1, alpha=0.8, shading="auto")
        prm = np.where(f["prcp"] >= 0.1, f["prcp"], np.nan)
        _draw_rain(ax, gx, gy, prm, f["t2m"], greens, blues, rain_norm)
        _draw_borders(ax)
        return fig

    # Fast path for eyeballing: render one representative frame, print it as
    # base64, and stop — no 400-frame render, no upload.
    if os.environ.get("FUSED_PREVIEW"):
        import base64
        keys = sorted(fields.keys())
        valid = keys[len(keys) // 4]                  # a near-term hour
        fig = render_frame(fields[valid], 700)
        fp = os.path.join(tempfile.mkdtemp(), "preview.png")
        fig.savefig(fp, dpi=100, facecolor="#fbf3de")
        import matplotlib.pyplot as _plt
        _plt.close(fig)
        b = base64.b64encode(open(fp, "rb").read()).decode()
        print(f"@@SNAP_BEGIN FUSED {len(b)}")
        print(b)
        print("@@SNAP_END FUSED")
        log.info("fused preview: %s", valid.isoformat())
        return

    tmp = tempfile.mkdtemp()
    hours_out = []
    for valid in sorted(fields.keys()):
        f = fields[valid]
        vtag = valid.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H")
        prm = np.where(f["prcp"] >= 0.1, f["prcp"], np.nan)
        for thr in THRESHOLDS:
            fig, ax = blank()
            _draw_clouds(ax, gx, gy, f["tcc"] / 100.0, grey)
            below = np.where((f["cb"] < thr) & ~f["fog"], 1.0, np.nan)
            ax.pcolormesh(gx, gy, below, cmap=pink, vmin=0, vmax=1,
                          alpha=0.75, shading="auto")
            ax.pcolormesh(gx, gy, np.where(f["fog"], 1.0, np.nan), cmap=orange,
                          vmin=0, vmax=1, alpha=0.8, shading="auto")
            _draw_rain(ax, gx, gy, prm, f["t2m"], greens, blues, rain_norm)
            _draw_borders(ax)
            fp = os.path.join(tmp, f"{vtag}_alt{thr}.png")
            fig.savefig(fp, dpi=100, facecolor="#fbf3de")
            plt.close(fig)
            PILImage.open(fp).convert("RGB").quantize(
                colors=192, dither=PILImage.Dither.NONE).save(fp, optimize=True)
            s3.upload_file(fp, config.R2_BUCKET, f"maps/fused/{run_tag}/{vtag}_alt{thr}.png",
                           ExtraArgs={"ContentType": "image/png",
                                      "CacheControl": "public, max-age=86400"})
            os.unlink(fp)
        hours_out.append(valid.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:00:00Z"))

    manifest = {"run": run_tag, "path": f"maps/fused/{run_tag}", "hours": hours_out,
                "proj": "epsg3059", "thresholds": THRESHOLDS,
                "credit": "TTAero fused: ICON+MEPS+IFS+GFS+UKMO via Open-Meteo, downscaled",
                "generated_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    s3.put_object(Bucket=config.R2_BUCKET, Key="maps/fused/latest.json",
                  Body=json.dumps(manifest).encode(), ContentType="application/json",
                  CacheControl="public, max-age=300")
    try:
        arch = json.loads(s3.get_object(Bucket=config.R2_BUCKET,
                                        Key="maps/fused/archive.json")["Body"].read())
    except Exception:
        arch = {"hours": {}, "thresholds": THRESHOLDS}
    for h in hours_out:
        arch["hours"][h.replace("-", "").replace(":", "")[:11]] = run_tag
    s3.put_object(Bucket=config.R2_BUCKET, Key="maps/fused/archive.json",
                  Body=json.dumps(arch).encode(), ContentType="application/json",
                  CacheControl="public, max-age=300")
    log.info("fused map: %d hours, run %s", len(hours_out), run_tag)
