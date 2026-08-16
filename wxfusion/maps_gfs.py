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
# One import, and it is the whole point: the GFS frames have to come out of
# the same ramp as MEPS and the radar or the three columns cannot be compared.
# rain_cmaps() was missing from this list — the four names that used to be here
# (RAIN_ANCHORS, RAIN_LEVELS, SNOW_ANCHORS, THRESHOLDS) were what the old
# hand-rolled BoundaryNorm needed, and when render_run switched to rain_cmaps()
# they stayed and it did not. Nothing catches a NameError raised before the
# per-lead try block, so every GFS render since has died on line one of the
# colour setup.
from .maps_meps import _draw_rain, rain_cmaps

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


def accum_window(fcst: str) -> tuple[int, int] | None:
    """Interval an accumulation record covers, from e.g. '54-58 hour acc'.

    GFS precipitation is not a per-frame total. It is a running sum that
    resets every 6 hours: at +55 h the record reads 54-55, at +56 h it reads
    54-56, and so on to 54-60 before starting again at 60-61. So consecutive
    frames are cumulative, not independent.

    Treating each record as its own interval — which is what we did at first,
    dividing by its length to get a rate — draws the union of up to six hours
    of rain on the 6-hourly frames and one hour of rain on the next. On
    1 August that showed a band of rain over half of Latvia at 21:00 and a
    small cell 300 km away at 22:00. Nothing moved; the frame simply stopped
    including the preceding five hours.
    """
    m = re.match(r"(\d+)-(\d+) hour acc", fcst)
    return (int(m.group(1)), int(m.group(2))) if m else None


def accum_hours(fcst: str, default: int) -> int:
    """Length in hours of an accumulation record's own interval."""
    w = accum_window(fcst)
    return w[1] - w[0] if w else default


def _fetch_field(run: dt.datetime, lead: int, recs, want, with_meta: bool = False,
                 valid_max: float | None = None):
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
    # Missing/sentinel values have to go BEFORE the interpolation, not after:
    # GFS writes "no ceiling" as a huge number, and averaging that into a
    # neighbour turns a real 300 m ceiling into 2e20 and erases it.
    if valid_max is not None:
        vals = np.where(np.isfinite(vals) & (vals < valid_max), vals, np.nan)
    arr = _to_3059(vals, lats, lons)
    return (arr, fcst) if with_meta else arr


# Smoothstep leaves a slight plateau around each cell centre — visible as a
# faint quilting on a big rain core. Three pixels of blur takes it out. That
# is a fifth of a cell, so it moves nothing: measured on the 02.08 06Z run at
# +106 h it cost 3% off the peak (11.62 -> 11.25 mm/h) and left the wet area
# within half a percent. 0 disables it.
SMOOTH_PX = float(os.environ.get("GFS_SMOOTH_PX", "3"))


def _to_3059(vals: np.ndarray, lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    """Interpolate onto the shared EPSG:3059 grid.

    This used to be nearest-neighbour, on the argument that the ceiling has
    genuine discontinuities and interpolating across one invents heights no
    model produced. True as far as it goes — but a 0.25° cell painted onto a
    1 km grid is a 15×28 km rectangle of one flat colour, and that staircase is
    every bit as invented as a gradient would be. It also does not match what
    the model means: the value is a cell AVERAGE, so the field it describes is
    smooth at this scale and the sharp step sits wherever the grid happens to
    fall, not where the weather is.

    So: bilinear, but with the weights run through smoothstep first. Plain
    bilinear leaves a crease along every cell boundary — the surface is
    continuous but its slope is not — and at 25 px per cell that crease reads
    as a diamond lattice over the whole map. Smoothstep weights make the slope
    zero at each boundary, so the cells join invisibly, and because the weights
    still sum to one and stay in [0,1] the result cannot overshoot: no rain
    appears where all four surrounding cells were dry, which is exactly the
    failure a bicubic would have. Cell-centre values survive untouched.

    NaN-aware throughout: a cell with no ceiling contributes no weight rather
    than dragging its neighbours up.
    """
    from . import proj3059 as P

    lat, lon = P.pixel_latlon()
    lon = np.where(lon < 0, lon + 360, lon)          # GFS runs 0..360
    dy, dx = lats[1] - lats[0], lons[1] - lons[0]
    fy = (lat - lats[0]) / dy
    fx = (lon - lons[0]) / dx
    j0 = np.clip(np.floor(fy).astype(int), 0, len(lats) - 2)
    i0 = np.clip(np.floor(fx).astype(int), 0, len(lons) - 2)
    wy = np.clip(fy - j0, 0.0, 1.0)
    wx = np.clip(fx - i0, 0.0, 1.0)
    wy = wy * wy * (3.0 - 2.0 * wy)          # smoothstep: flat slope at the
    wx = wx * wx * (3.0 - 2.0 * wx)          # cell edges, so no crease there

    acc = np.zeros(lat.shape, dtype=float)
    wsum = np.zeros(lat.shape, dtype=float)
    for dj, wj in ((0, 1.0 - wy), (1, wy)):
        for di, wi in ((0, 1.0 - wx), (1, wx)):
            v = vals[j0 + dj, i0 + di].astype(float)
            w = wj * wi
            ok = np.isfinite(v)
            acc += np.where(ok, v * w, 0.0)
            wsum += np.where(ok, w, 0.0)
    out = np.where(wsum > 1e-9, acc / np.maximum(wsum, 1e-9), np.nan)
    return _feather(out, SMOOTH_PX)


def _feather(arr: np.ndarray, sigma: float) -> np.ndarray:
    """NaN-aware Gaussian blur. Keeps the original footprint exactly."""
    if sigma <= 0:
        return arr
    try:
        from scipy.ndimage import gaussian_filter
    except ImportError:          # bilinear alone is still a large improvement
        log.warning("gfs: scipy missing - maps stay bilinear, not feathered")
        return arr
    ok = np.isfinite(arr)
    if not ok.any():
        return arr
    num = gaussian_filter(np.where(ok, arr, 0.0), sigma, mode="nearest")
    den = gaussian_filter(ok.astype(float), sigma, mode="nearest")
    # Divide by the weight that actually landed on valid pixels, so the blur
    # does not fade a field out against its own edge.
    return np.where(ok, num / np.maximum(den, 1e-6), np.nan)


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
    from matplotlib.colors import ListedColormap
    from PIL import Image as PILImage

    from . import proj3059 as P

    log.info("gfs: %s", check_eccodes())
    run = latest_run()
    if run is None:
        raise RuntimeError("gfs: no published cycle found on the AWS mirror")
    run_tag = run.strftime("%Y%m%dT%H")
    log.info("gfs: run %s, leads %d..%d", run_tag, start_lead, end_lead)

    s3 = r2_client()
    greens, blues, rain_norm = rain_cmaps()
    pink = ListedColormap(["#f3b8b4"])
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
    # Previous rendered lead's raw accumulation, so the running 6-hour bucket
    # can be differenced back to what fell in this frame's hour alone. See
    # accum_window(). MEPS has always done this (maps_meps differences
    # precipitation_amount_acc); GFS was the odd one out.
    prev: tuple[int, int, np.ndarray] | None = None   # (lead, bucket start, acc)
    # (ceiling, valid) of the lead before this one, waiting for the rain that
    # falls after it.
    pending: tuple[np.ndarray, dt.datetime, np.ndarray | None] | None = None

    def _apcp(at_lead: int):
        recs_l = _index(run, at_lead) if at_lead != lead else recs
        got_l = _fetch_field(run, at_lead, recs_l,
                             lambda v, l: v == "APCP" and l == "surface",
                             with_meta=True)
        if not got_l:
            return None
        arr, fcst = got_l
        win = accum_window(fcst)
        return (arr, win) if win else None

    for lead in leads_for(start_lead, end_lead):
        step = lead - prev[0] if prev and prev[0] < lead else (
            1 if lead <= HOURLY_TO else 3)
        try:
            recs = _index(run, lead)
            ceil = _fetch_field(run, lead, recs,
                                lambda v, l: v == "HGT" and l.startswith("cloud ceiling"),
                                valid_max=NO_CEILING_M)
            got = _apcp(lead)
            # Kelvin, and optional: an hour with no temperature record simply
            # draws green, which is what every hour did before.
            tk = _fetch_field(run, lead, recs,
                              lambda v, l: v == "TMP" and l == "2 m above ground")
            tair = (tk - 273.15) if tk is not None else None
        except Exception as exc:
            log.error("gfs: lead +%dh failed: %s: %s", lead,
                      exc.__class__.__name__, exc)
            prev = None
            continue
        if ceil is None:
            log.warning("gfs: no ceiling field at +%dh", lead)
            continue

        # HGT is geopotential height, which is referenced to mean sea level —
        # NCEP's table calls this record "geopotential height of cloud ceiling"
        # and says no more, but that is what the quantity means. Same correction
        # as MEPS: subtract the ground so the map answers the question a pilot
        # asks. A single frame could not settle it empirically (the ceiling's
        # correlation with terrain is dominated by where the weather is), so
        # this rests on the definition rather than on a measurement.
        cb = np.where(ceil < NO_CEILING_M, ceil, np.nan)
        try:
            surf = _fetch_field(run, lead, recs,
                                lambda v, l: v == "HGT" and l == "surface")
            if surf is not None:
                cb = np.maximum(cb - surf, 0.0)
        except Exception:
            log.warning("gfs: no surface height at +%dh; ceiling stays above sea level", lead)

        pr = None
        if got:
            acc, (a0, a1) = got
            if a0 == lead - step:
                # The record's own interval is exactly this frame's: the hour
                # after a bucket reset, and every 3-hourly lead past +120 h.
                amount = np.clip(acc, 0, None)
            else:
                if prev is None or prev[1] != a0 or prev[0] != lead - step:
                    # First frame of the run, or the previous lead failed.
                    # One extra range request buys a correct frame instead of
                    # a six-hour smear.
                    try:
                        seed = _apcp(lead - step)
                    except Exception:
                        seed = None
                    prev = (lead - step, seed[1][0], seed[0]) if seed else None
                if prev is not None and prev[1] == a0:
                    amount = np.clip(acc - prev[2], 0, None)
                else:
                    log.warning("gfs: +%dh cannot difference %d-%d, drawing the "
                                "bucket mean", lead, a0, a1)
                    amount = np.clip(acc, 0, None) * step / max(a1 - a0, 1)
            prev = (lead, a0, acc)
            pr = amount / max(step, 1)          # mm/h, as on the MEPS frames
        prm = np.where(pr >= 0.1, pr, np.nan) if pr is not None else None

        # A frame stamped 01:00 carries the ceiling at 01:00 and the rain that
        # falls between 01:00 and the next step — the hour STARTING at the
        # label, matching maps_meps. The rain we just worked out fell over
        # (lead-step, lead], so it belongs to the PREVIOUS frame, whose ceiling
        # has been held back for exactly this. One frame at the far end of the
        # run has no following accumulation and is dropped.
        # The temperature is held back with the ceiling, not taken from the
        # current lead: the rain fell over (lead-step, lead], so the air it
        # fell through is the air at the START of that window.
        held, held_valid, held_t = pending if pending else (None, None, None)
        pending = (cb, run + dt.timedelta(hours=lead), tair)
        if held is None:
            continue
        cb, valid, tair = held, held_valid, held_t
        vtag = valid.strftime("%Y%m%dT%H")
        for thr in GFS_THRESHOLDS:
            fig, ax = blank_fig()
            below = np.where(np.isfinite(cb) & (cb < thr), 1.0, np.nan)
            ax.pcolormesh(xw, yw, below, cmap=pink, vmin=0, vmax=1,
                          alpha=0.75, shading="auto")
            if prm is not None:
                _draw_rain(ax, xw, yw, prm, tair, greens, blues, rain_norm)
            draw_borders(ax)
            fp = os.path.join(tmp, f"{vtag}_alt{thr}.png")
            fig.savefig(fp, dpi=100, facecolor="#fbf3de")
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
