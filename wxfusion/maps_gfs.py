"""GFS map frames for the tail of the week that MEPS cannot reach.

MEPS stops at +66 h and meteo.pl UM at +120 h, so days 3-7 of the week view
had no map at all. GFS runs to +384 h and — usefully — publishes
`HGT:cloud ceiling` as a gridded field, which is the same quantity the
meteograms now plot: the height of the lowest cloud, not a condensation level.
So the long-range map means exactly what the short-range one does.

Fetching: NOAA retired the NOMADS OpenDAP endpoint (SCN 25-81), and the
grib_filter CGI rejects the cloud-ceiling level name. The AWS Open Data mirror
works without credentials and ships a .idx sidecar giving the byte offset of
every field, so we range-request just the two messages we need per lead —
about 1.7 MB instead of the ~500 MB full file.

Rendered with the same palette and thresholds as the MEPS frames, so the
column looks continuous as the slider crosses from one source to the other.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import logging
import os
import re
import tempfile

import boto3
import numpy as np

from . import config
from .http import session
from .maps_meps import THRESHOLDS

log = logging.getLogger(__name__)

S3_BASE = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
# GFS encodes "no ceiling" as a sentinel near the top of the atmosphere
# rather than as a missing value.
NO_CEILING_M = 15000.0


def check_eccodes() -> str:
    """Fail loudly and early if the GRIB reader is not usable.

    The eccodes wheel bundles the C library, but if it ever does not, the
    failure would otherwise surface once per lead inside a caught exception
    and the job would exit green having produced nothing.
    """
    import eccodes as ec
    return f"eccodes {ec.__version__} (library {ec.codes_get_api_version()})"


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


def _prefix(run: dt.datetime) -> str:
    return f"{S3_BASE}/gfs.{run:%Y%m%d}/{run:%H}/atmos/gfs.t{run:%H}z.pgrb2.0p25"


def latest_run(max_back: int = 5) -> dt.datetime | None:
    """Newest GFS cycle whose index for a late lead is already published."""
    now = dt.datetime.now(dt.timezone.utc)
    cycle = now.replace(minute=0, second=0, microsecond=0, hour=now.hour // 6 * 6)
    for _ in range(max_back):
        r = session().get(f"{_prefix(cycle)}.f168.idx", timeout=60)
        if r.status_code == 200:
            return cycle
        cycle -= dt.timedelta(hours=6)
    return None


def _index(run: dt.datetime, lead: int) -> list[tuple[int, str, str, str]]:
    r = session().get(f"{_prefix(run)}.f{lead:03d}.idx", timeout=60)
    r.raise_for_status()
    recs = []
    for line in r.text.splitlines():
        parts = line.split(":")
        if len(parts) > 5:
            recs.append((int(parts[1]), parts[3], parts[4], parts[5]))
    return recs


def accum_hours(fcst: str, default: int) -> int:
    """Hours covered by an accumulation record, from e.g. '90-96 hour acc'.

    Worth reading rather than assuming: GFS buckets precipitation over 3 h at
    some leads and 6 h at multiples of six, so a fixed divisor would report
    double the real rate on half the frames.
    """
    m = re.match(r"(\d+)-(\d+) hour acc", fcst)
    return int(m.group(2)) - int(m.group(1)) if m else default


def _fetch_field(run: dt.datetime, lead: int, recs, want, with_meta: bool = False):
    """Range-request one GRIB message and return it on the Baltic window."""
    import eccodes as ec  # noqa: F401 — see check_eccodes() for why this is fine

    hit = fcst = None
    for i, (off, var, lev, desc) in enumerate(recs):
        if want(var, lev):
            end = recs[i + 1][0] - 1 if i + 1 < len(recs) else ""
            hit, fcst = (off, end), desc
            break
    if hit is None:
        return None

    r = session().get(f"{_prefix(run)}.f{lead:03d}",
                      headers={"Range": f"bytes={hit[0]}-{hit[1]}"}, timeout=180)
    r.raise_for_status()
    gid = ec.codes_new_from_message(r.content)
    try:
        ni, nj = ec.codes_get(gid, "Ni"), ec.codes_get(gid, "Nj")
        lat1 = ec.codes_get(gid, "latitudeOfFirstGridPointInDegrees")
        lat2 = ec.codes_get(gid, "latitudeOfLastGridPointInDegrees")
        lon1 = ec.codes_get(gid, "longitudeOfFirstGridPointInDegrees")
        lon2 = ec.codes_get(gid, "longitudeOfLastGridPointInDegrees")
        vals = ec.codes_get_values(gid).reshape(nj, ni)
    finally:
        ec.codes_release(gid)

    lats = np.linspace(lat1, lat2, nj)
    lons = np.linspace(lon1, lon2, ni)
    arr = _to_3059(vals, lats, lons)
    return (arr, fcst) if with_meta else arr


def _to_3059(vals: np.ndarray, lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    """Nearest-neighbour sample onto the shared EPSG:3059 grid.

    Nearest rather than bilinear on purpose: ceiling is a field with genuine
    discontinuities (cloud edge), and interpolating across one invents
    intermediate heights that no model produced.
    """
    from . import proj3059 as P

    lat, lon = P.pixel_latlon()
    lon = np.where(lon < 0, lon + 360, lon)          # GFS runs 0..360
    jy = np.clip(np.rint((lat - lats[0]) / (lats[1] - lats[0])).astype(int), 0, len(lats) - 1)
    ix = np.clip(np.rint((lon - lons[0]) / (lons[1] - lons[0])).astype(int), 0, len(lons) - 1)
    return vals[jy, ix]


# Only the limits the UI can actually select. MEPS also renders 650, which no
# dropdown entry maps to, and every extra threshold is another render+upload
# on every lead.
GFS_THRESHOLDS = [100, 300, 700, 1000]
# GFS publishes hourly to +120 h and 3-hourly beyond, so follow that rather
# than settling for the coarser cadence throughout: hourly maps are the point.
HOURLY_TO = 120


def leads_for(start_lead: int, end_lead: int) -> list[int]:
    hourly = list(range(start_lead, min(end_lead, HOURLY_TO) + 1))
    coarse = [h for h in range(HOURLY_TO + 3, end_lead + 1, 3)]
    return hourly + coarse


def render_run(start_lead: int = 36, end_lead: int = 168, step: int = 6) -> None:
    """Render ceiling + precipitation frames for the far end of the week."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap
    from PIL import Image as PILImage

    from . import proj3059 as P

    log.info("gfs: %s", check_eccodes())
    run = latest_run()
    if run is None:
        raise RuntimeError("gfs: no published cycle found on the AWS mirror")
    run_tag = run.strftime("%Y%m%dT%H")
    log.info("gfs: run %s, leads %d..%d", run_tag, start_lead, end_lead)

    s3 = r2_client()
    greens = LinearSegmentedColormap.from_list(
        "g", ["#c8e6b0", "#57b647", "#1c7a1c", "#0b4d0b"])
    pink = LinearSegmentedColormap.from_list("p", ["#f3b8b4", "#f3b8b4"])
    borders = json.load(open(os.path.join(
        os.path.dirname(__file__), "assets", "borders_baltic.json")))
    tmp = tempfile.mkdtemp()
    xw, yw = np.arange(P.W), np.arange(P.H)

    def blank_fig():
        fig = plt.figure(figsize=(P.W / 100, P.H / 100), dpi=100)
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_xlim(0, P.W - 1); ax.set_ylim(P.H - 1, 0)
        ax.axis("off"); ax.set_facecolor("none")
        fig.patch.set_alpha(0)
        return fig, ax

    def draw_borders(ax):
        for c in borders:
            for line in c["lines"]:
                px, py = P.to_px([la for _, la in line], [lo for lo, _ in line])
                ax.plot(px, py, color="#f7f1e2", lw=2.4, alpha=0.9, solid_capstyle="round")
                ax.plot(px, py, color="#55503f", lw=0.9, solid_capstyle="round")

    hours_out = []
    for lead in leads_for(start_lead, end_lead):
        try:
            recs = _index(run, lead)
            ceil = _fetch_field(run, lead, recs,
                                lambda v, l: v == "HGT" and l.startswith("cloud ceiling"))
            got = _fetch_field(run, lead, recs,
                               lambda v, l: v == "APCP" and l == "surface",
                               with_meta=True)
            acc, acc_fcst = got if got else (None, "")
        except Exception as exc:
            log.error("gfs: lead +%dh failed: %s: %s", lead,
                      exc.__class__.__name__, exc)
            continue
        if ceil is None:
            log.warning("gfs: no ceiling field at +%dh", lead)
            continue

        cb = np.where(ceil < NO_CEILING_M, ceil, np.nan)
        # APCP is an accumulation over the interval since the last reset;
        # divide to an average rate so the greens mean mm/h as on MEPS.
        hrs = accum_hours(acc_fcst, 1) if acc is not None else 1
        pr = np.clip(acc, 0, None) / max(hrs, 1) if acc is not None else None
        prm = np.where(pr >= 0.1, pr, np.nan) if pr is not None else None

        valid = run + dt.timedelta(hours=lead)
        vtag = valid.strftime("%Y%m%dT%H")
        for thr in GFS_THRESHOLDS:
            fig, ax = blank_fig()
            below = np.where(np.isfinite(cb) & (cb < thr), 1.0, np.nan)
            ax.pcolormesh(xw, yw, below, cmap=pink, vmin=0, vmax=1,
                          alpha=0.75, shading="auto")
            if prm is not None:
                ax.pcolormesh(xw, yw, prm, cmap=greens, vmin=0.1, vmax=4,
                              alpha=0.9, shading="auto")
            draw_borders(ax)
            fp = os.path.join(tmp, f"{vtag}_alt{thr}.png")
            fig.savefig(fp, dpi=100)
            plt.close(fig)
            PILImage.open(fp).convert("RGB").quantize(
                colors=192, dither=PILImage.Dither.NONE).save(fp, optimize=True)
            s3.upload_file(fp, config.R2_BUCKET, f"maps/gfs/{run_tag}/{vtag}_alt{thr}.png",
                           ExtraArgs={"ContentType": "image/png",
                                      "CacheControl": "public, max-age=86400"})
            os.unlink(fp)
        hours_out.append(valid.strftime("%Y-%m-%dT%H:00:00Z"))
        log.info("gfs: +%dh -> %s", lead, vtag)

    if not hours_out:
        # Every lead failing looks identical to "no work to do" unless we say
        # so. Backfill #7 spent 2 m 18 s here, rendered nothing, and still
        # reported success — which is how this went unnoticed.
        raise RuntimeError(
            f"gfs: {len(leads_for(start_lead, end_lead))} leads attempted, "
            "none rendered — see the per-lead errors above")

    now = dt.datetime.now(dt.timezone.utc)
    s3.put_object(Bucket=config.R2_BUCKET, Key="maps/gfs/latest.json",
                  Body=json.dumps({
                      "run": run_tag, "path": f"maps/gfs/{run_tag}",
                      "hours": hours_out, "thresholds": GFS_THRESHOLDS,
                      "proj": "epsg3059", "step_h": 3,
                      "credit": "NOAA GFS 0.25 deg via AWS Open Data",
                      "generated_at": now.isoformat()}).encode(),
                  ContentType="application/json",
                  CacheControl="public, max-age=300")

    try:
        arch = json.loads(s3.get_object(
            Bucket=config.R2_BUCKET, Key="maps/gfs/archive.json")["Body"].read())
    except Exception:
        arch = {"hours": {}}
    for iso in hours_out:
        vtag = iso[0:4] + iso[5:7] + iso[8:10] + "T" + iso[11:13]
        if arch["hours"].get(vtag, "") <= run_tag:
            arch["hours"][vtag] = run_tag
    s3.put_object(Bucket=config.R2_BUCKET, Key="maps/gfs/archive.json",
                  Body=json.dumps(arch).encode(),
                  ContentType="application/json",
                  CacheControl="public, max-age=300")
    log.info("gfs: %d frames, archive now %d hours", len(hours_out), len(arch["hours"]))
