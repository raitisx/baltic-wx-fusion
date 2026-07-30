"""Hourly Baltic flight maps from MET Norway MEPS (2.5 km) -> R2.

Why MEPS: it publishes the fields the flight decision needs *directly* —
cloud_base_altitude, precipitation_amount_acc, wind_speed_of_gust,
fog_area_fraction — on a 2.5 km grid covering the whole Baltic, via free
OPeNDAP (thredds.met.no, NLOD/CC-BY licence, credit MET Norway).

Output per run (one PNG per lead hour per cloud-base threshold):
  maps/meps/<runYYYYMMDDTHH>/<validYYYYMMDDTHH>_alt<thr>.png
  maps/meps/latest.json   manifest: {run, hours[], thresholds[], path}

Style matches the TTA-tracker weather maps: cream land, green precip scale,
pink tint where cloud base is below the threshold, hatched-free, country
borders from a bundled Natural Earth extract.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import tempfile

import boto3
import numpy as np

from . import config
from .http import session

log = logging.getLogger(__name__)

CATALOG = "https://thredds.met.no/thredds/catalog/mepslatest/catalog.xml"
DAP_BASE = "https://thredds.met.no/thredds/dodsC/mepslatest/"
BBOX = (53.8, 59.9, 20.0, 28.6)  # lat_min, lat_max, lon_min, lon_max
THRESHOLDS = [100, 300, 650, 700, 1000]
FILL = 1e30  # cloud_base_altitude fill => clear sky
# A ceiling needs broken-or-more low cloud, the same 5-okta rule the meteogram
# applies. MEPS publishes cloud_base_altitude wherever it finds any cloud base
# at all, and below 700 m that is almost never a ceiling: over the Baltic
# window of the 30.07 18Z run, the pixels with a base under 700 m had a mean
# low-cloud fraction of 0.011 and a 90th percentile of 0.00 — no low cloud
# worth the name. Meanwhile where the low cloud really was broken, the base sat
# at 1150-1550 m. Painting the first group pink is what made the map disagree
# with the meteogram, which has been gating since the LCL fix.
CEILING_LCC = 0.60
# Fog is a zero-foot ceiling however the cloud-base field reports it.
FOG_COVER = 0.60


def _gate_ceiling(cb, lcc, fog):
    """Cloud base -> aviation ceiling: only where the low cloud is broken."""
    import numpy as np
    cb = np.where(cb > FILL, np.nan, cb)
    cb = np.where(lcc >= CEILING_LCC, cb, np.nan)
    if fog is not None:
        cb = np.where(fog >= FOG_COVER, 0.0, cb)
    return cb

BORDERS_FILE = os.path.join(os.path.dirname(__file__), "assets", "borders_baltic.json")


def r2_client():
    if not (config.R2_ACCOUNT_ID and config.R2_ACCESS_KEY_ID):
        raise RuntimeError("R2 credentials not configured")
    return boto3.client(
        "s3",
        endpoint_url=f"https://{config.R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=config.R2_ACCESS_KEY_ID,
        aws_secret_access_key=config.R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )


def list_runs() -> list[str]:
    r = session().get(CATALOG, timeout=60)
    r.raise_for_status()
    runs = re.findall(r'urlPath="mepslatest/(meps_det_2_5km_\d{8}T\d{2}Z\.ncml)"', r.text)
    if not runs:
        raise RuntimeError("no meps_det datasets in catalog")
    return sorted(runs)


def latest_run() -> str:
    return list_runs()[-1]


def backfill_runs(max_runs: int = 12, leads=(1, 2, 3)) -> None:
    """Render short-lead frames from PAST runs still on thredds (the catalog
    keeps roughly the last day of runs) so today's already-passed hours get
    near-analysis maps. Registers them in maps/meps/archive.json; an hour is
    only overwritten by a NEWER run (shorter lead = better)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap
    import netCDF4 as nc
    from PIL import Image as PILImage
    from . import proj3059 as P

    s3 = r2_client()
    try:
        arch = json.loads(s3.get_object(
            Bucket=config.R2_BUCKET, Key="maps/meps/archive.json"
        )["Body"].read())
    except Exception:
        arch = {"hours": {}, "thresholds": THRESHOLDS}

    borders = json.load(open(BORDERS_FILE))
    greens = LinearSegmentedColormap.from_list(
        "g", ["#c8e6b0", "#57b647", "#1c7a1c", "#0b4d0b"])
    pink = LinearSegmentedColormap.from_list("p", ["#f3b8b4", "#f3b8b4"])
    tmp = tempfile.mkdtemp()

    runs = list_runs()[:-1][-max_runs:]  # past runs, oldest -> newest
    log.info("meps backfill: %d candidate runs", len(runs))
    for ds_name in runs:
        run_tag = re.search(r"(\d{8}T\d{2})Z", ds_name).group(1)
        run_dt = dt.datetime.strptime(run_tag, "%Y%m%dT%H").replace(
            tzinfo=dt.timezone.utc)
        hour_keys = [(run_dt + dt.timedelta(hours=h)).strftime("%Y%m%dT%H")
                     for h in leads]
        if all(arch["hours"].get(k, "") >= run_tag for k in hour_keys):
            continue  # newer or same coverage already present
        try:
            ds = nc.Dataset(DAP_BASE + ds_name)
            lat = np.array(ds.variables["latitude"][:])
            lon = np.array(ds.variables["longitude"][:])
            m = (lat >= BBOX[0]) & (lat <= BBOX[1]) & \
                (lon >= BBOX[2]) & (lon <= BBOX[3])
            ys, xs = np.where(m)
            y0, y1, x0, x1 = ys.min(), ys.max(), xs.min(), xs.max()
            latw = lat[y0:y1 + 1, x0:x1 + 1]
            lonw = lon[y0:y1 + 1, x0:x1 + 1]
            n = max(leads) + 1
            cb = np.array(ds.variables["cloud_base_altitude"][:n, 0, y0:y1 + 1, x0:x1 + 1])
            acc = np.array(ds.variables["precipitation_amount_acc"][:n, 0, y0:y1 + 1, x0:x1 + 1])
            lcc = np.array(ds.variables["low_type_cloud_area_fraction"][:n, 0, y0:y1 + 1, x0:x1 + 1])
            try:
                fog = np.array(ds.variables["fog_area_fraction"][:n, 0, y0:y1 + 1, x0:x1 + 1])
            except Exception:
                fog = None
            times = nc.num2date(ds.variables["time"][:n], ds.variables["time"].units)
        except Exception:
            log.exception("meps backfill: %s failed to open/fetch", ds_name)
            continue
        cb = _gate_ceiling(cb, lcc, fog)
        xw, yw = P.to_xy(lonw, latw)
        borders_xy = [[P.to_xy([px for px, _ in line], [py for _, py in line])
                       for line in c["lines"]] for c in borders]

        for i in leads:
            if i >= len(times):
                continue
            valid = times[i]
            vtag = valid.strftime("%Y%m%dT%H")
            if arch["hours"].get(vtag, "") >= run_tag:
                continue
            if i + 1 >= len(acc):
                continue
            # hour STARTING at the stamp — see the note in the live renderer
            pr = np.clip(acc[i + 1] - acc[i], 0, None)
            prm = np.where(pr >= 0.1, pr, np.nan)
            for thr in THRESHOLDS:
                fig = plt.figure(figsize=(P.W / 100, P.H / 100), dpi=100)
                ax = fig.add_axes([0, 0, 1, 1])
                ax.set_facecolor("#fbf3de")
                ax.set_xlim(P.X0, P.X1); ax.set_ylim(P.Y0, P.Y1)
                ax.set_aspect("equal"); ax.axis("off")
                below = np.where(np.isfinite(cb[i]) & (cb[i] < thr), 1.0, np.nan)
                ax.pcolormesh(xw, yw, below, cmap=pink, vmin=0, vmax=1,
                              alpha=0.75, shading="auto")
                ax.pcolormesh(xw, yw, prm, cmap=greens, vmin=0.1, vmax=4,
                              alpha=0.9, shading="auto")
                for country in borders_xy:
                    for bx, by in country:
                        ax.plot(bx, by, color="#f7f1e2", lw=2.2, zorder=9)
                        ax.plot(bx, by, color="#55503f", lw=0.9, zorder=10)
                fp = os.path.join(tmp, "f.png")
                fig.savefig(fp, dpi=100)
                plt.close(fig)
                PILImage.open(fp).convert("RGB").quantize(
                    colors=192, dither=PILImage.Dither.NONE).save(fp, optimize=True)
                s3.upload_file(fp, config.R2_BUCKET,
                               f"maps/meps/{run_tag}/{vtag}_alt{thr}.png",
                               ExtraArgs={"ContentType": "image/png",
                                          "CacheControl": "public, max-age=604800"})
            arch["hours"][vtag] = run_tag
            log.info("meps backfill: %s <- run %s", vtag, run_tag)
        s3.put_object(Bucket=config.R2_BUCKET, Key="maps/meps/archive.json",
                      Body=json.dumps(arch).encode(),
                      ContentType="application/json",
                      CacheControl="public, max-age=300")
    log.info("meps backfill done: archive now %d hours", len(arch["hours"]))


def render_run(max_hours: int = 66) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap
    import netCDF4 as nc

    ds_name = latest_run()
    run_tag = re.search(r"(\d{8}T\d{2})Z", ds_name).group(1)
    log.info("MEPS run %s", ds_name)
    ds = nc.Dataset(DAP_BASE + ds_name)

    lat = np.array(ds.variables["latitude"][:])
    lon = np.array(ds.variables["longitude"][:])
    m = (lat >= BBOX[0]) & (lat <= BBOX[1]) & (lon >= BBOX[2]) & (lon <= BBOX[3])
    ys, xs = np.where(m)
    y0, y1, x0, x1 = ys.min(), ys.max(), xs.min(), xs.max()
    latw, lonw = lat[y0:y1 + 1, x0:x1 + 1], lon[y0:y1 + 1, x0:x1 + 1]

    times = nc.num2date(ds.variables["time"][:], ds.variables["time"].units)
    n = min(len(times), max_hours + 1)
    log.info("fetching %d steps, window %dx%d", n, y1 - y0 + 1, x1 - x0 + 1)
    cb = np.array(ds.variables["cloud_base_altitude"][:n, 0, y0:y1 + 1, x0:x1 + 1])
    acc = np.array(ds.variables["precipitation_amount_acc"][:n, 0, y0:y1 + 1, x0:x1 + 1])
    lcc = np.array(ds.variables["low_type_cloud_area_fraction"][:n, 0, y0:y1 + 1, x0:x1 + 1])
    try:
        fog = np.array(ds.variables["fog_area_fraction"][:n, 0, y0:y1 + 1, x0:x1 + 1])
    except Exception:
        fog = None
    cb = _gate_ceiling(cb, lcc, fog)

    from . import proj3059 as P

    borders = json.load(open(BORDERS_FILE))
    greens = LinearSegmentedColormap.from_list(
        "g", ["#c8e6b0", "#57b647", "#1c7a1c", "#0b4d0b"])
    pink = LinearSegmentedColormap.from_list("p", ["#f3b8b4", "#f3b8b4"])

    # project the MEPS cell coordinates once (curvilinear mesh in LKS-92)
    xw, yw = P.to_xy(lonw, latw)
    borders_xy = [[P.to_xy([px for px, _ in line], [py for _, py in line])
                   for line in c["lines"]] for c in borders]

    def blank_fig():
        """Full-bleed axes on the fixed EPSG:3059 extent — no text, no
        margins; timestamps live in the page UI, not in pixels."""
        fig = plt.figure(figsize=(P.W / 100, P.H / 100), dpi=100)
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_facecolor("#fbf3de")
        ax.set_xlim(P.X0, P.X1); ax.set_ylim(P.Y0, P.Y1)
        ax.set_aspect("equal"); ax.axis("off")
        return fig, ax

    def draw_borders(ax):
        # cased line (light halo under dark core), forced above all data
        for country in borders_xy:
            for bx, by in country:
                ax.plot(bx, by, color="#f7f1e2", lw=2.2, zorder=9,
                        solid_capstyle="round")
                ax.plot(bx, by, color="#55503f", lw=0.9, zorder=10,
                        solid_capstyle="round")

    s3 = r2_client()
    hours_out = []
    tmp = tempfile.mkdtemp()

    # shared background plate for overlay layers (UM etc.)
    fig, ax = blank_fig()
    draw_borders(ax)
    bg = os.path.join(tmp, "bg.png")
    fig.savefig(bg, dpi=100)
    plt.close(fig)
    s3.upload_file(bg, config.R2_BUCKET, "maps/bg_3059.png", ExtraArgs={
        "ContentType": "image/png", "CacheControl": "public, max-age=604800"})

    # A frame stamped 01:00 shows the ceiling at 01:00 and the rain that falls
    # between 01:00 and 02:00 — the hour STARTING at the label. It used to show
    # the hour ending there, because precipitation_amount_acc is a running
    # total and the obvious difference is acc[i] - acc[i-1]. That reads an hour
    # late: checked against the radar archive over 49 hours with rain in both,
    # the old stamping matched best at -1 h on centroid distance (75 km, vs
    # 87 km at 0) and on overlap (0.178 vs 0.159). It also made the question
    # this map exists to answer - when does it start raining here - one the
    # reader had to shift by an hour in their head.
    for i in range(0, n - 1):
        valid = times[i]
        vtag = valid.strftime("%Y%m%dT%H")
        pr = np.clip(acc[i + 1] - acc[i], 0, None)
        prm = np.where(pr >= 0.1, pr, np.nan)
        for thr in THRESHOLDS:
            fig, ax = blank_fig()
            below = np.where(np.isfinite(cb[i]) & (cb[i] < thr), 1.0, np.nan)
            ax.pcolormesh(xw, yw, below, cmap=pink, vmin=0, vmax=1,
                          alpha=0.75, shading="auto")
            ax.pcolormesh(xw, yw, prm, cmap=greens, vmin=0.1, vmax=4,
                          alpha=0.9, shading="auto")
            draw_borders(ax)
            fp = os.path.join(tmp, f"{vtag}_alt{thr}.png")
            fig.savefig(fp, dpi=100)
            plt.close(fig)
            # palette-quantize: ~60% smaller files, visually identical at
            # these soft fills — matters because r2.dev is rate-limited
            from PIL import Image as PILImage
            PILImage.open(fp).convert("RGB").quantize(
                colors=192, dither=PILImage.Dither.NONE).save(fp, optimize=True)
            key = f"maps/meps/{run_tag}/{vtag}_alt{thr}.png"
            s3.upload_file(fp, config.R2_BUCKET, key, ExtraArgs={
                "ContentType": "image/png",
                "CacheControl": "public, max-age=86400"})
            os.unlink(fp)
        hours_out.append(valid.strftime("%Y-%m-%dT%H:00:00Z"))
        if i % 12 == 0:
            log.info("rendered %d/%d hours", i, n - 1)

    manifest = {
        "run": run_tag,
        "path": f"maps/meps/{run_tag}",
        "hours": hours_out,
        "proj": "epsg3059",
        "thresholds": THRESHOLDS,
        "credit": "MET Norway MEPS (thredds.met.no), CC-BY 4.0 / NLOD",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    s3.put_object(Bucket=config.R2_BUCKET, Key="maps/meps/latest.json",
                  Body=json.dumps(manifest).encode(),
                  ContentType="application/json",
                  CacheControl="public, max-age=300")
    # archive index: hour -> run whose frame covers it (newest run wins).
    # Keeps past hours viewable after the axis moves on — frames are never
    # deleted, so yesterday's 09:00 stays reachable forever.
    try:
        arch = json.loads(s3.get_object(
            Bucket=config.R2_BUCKET, Key="maps/meps/archive.json"
        )["Body"].read())
    except Exception:
        arch = {"hours": {}, "thresholds": THRESHOLDS}
    for h in hours_out:
        arch["hours"][h.replace("-", "").replace(":", "")[:11]] = run_tag
    s3.put_object(Bucket=config.R2_BUCKET, Key="maps/meps/archive.json",
                  Body=json.dumps(arch).encode(),
                  ContentType="application/json",
                  CacheControl="public, max-age=300")
    log.info("manifest written: %d hours, run %s", len(hours_out), run_tag)
