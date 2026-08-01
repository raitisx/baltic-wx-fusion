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

log = logging.getLogger(__name__)

CATALOG = "https://thredds.met.no/thredds/catalog/mepslatest/catalog.xml"
DAP_BASE = "https://thredds.met.no/thredds/dodsC/mepslatest/"
BBOX = (53.8, 59.9, 20.0, 28.6)  # lat_min, lat_max, lon_min, lon_max
THRESHOLDS = [100, 300, 650, 700, 1000]
FILL = 1e30  # cloud_base_altitude fill => clear sky
G0 = 9.80665  # m/s^2, for surface_geopotential -> terrain height


def _terrain(ds, y0, y1, x0, x1):
    """Ground height above sea level, metres, over the window.

    MEPS publishes cloud_base_altitude, and in CF and WMO terms "altitude"
    means above mean SEA level while "height" means above the ground. Drawn
    raw, the map was reporting a ceiling that included whatever hill was
    underneath — a median of 64 m across this window, 97 m over land, 177 m at
    the 90th percentile and 319 m at its highest. The meteogram's ceiling is an
    LCL, which is above ground by construction, and a METAR ceiling is above
    the aerodrome. So the map was the odd one out, by up to a third of the
    limit it was being compared against.
    """
    import numpy as np
    try:
        fis = np.array(ds.variables["surface_geopotential"][0, 0, y0:y1 + 1, x0:x1 + 1])
    except Exception:
        log.warning("no surface_geopotential; cloud base stays above sea level")
        return None
    return (fis / G0).astype("float32")


def _to_agl(cb, terrain):
    """Cloud base above SEA level -> above the ground under it."""
    import numpy as np
    if terrain is None:
        return cb
    out = np.where(cb > FILL, cb, np.maximum(cb - terrain, 0.0))
    return out.astype(cb.dtype, copy=False)


# Rain ramp measured off a meteo.pl frame (see scrape_maps.RAIN_STEPS for the
# pixel counts): pale cream-yellow for the lightest rain, darkening through
# green, then orange for the heavy cores. No yellow in the middle.
RAIN_ANCHORS = ["#fcffad", "#e6ff78", "#00e300", "#00a800", "#006709", "#ff7800"]
# Discrete, not a smooth ramp, and on the same breakpoints as the radar classes
# so the two columns can be compared directly. The old continuous scale ran
# 0.1-4 mm/h linearly, which put everything above 4 mm/h at full orange — most
# of a rain area, so the map read as a solid orange blob where meteo.pl showed
# green with small orange cores.
RAIN_LEVELS = [0.1, 0.4, 1.0, 2.5, 6.0, 15.0, 1e6]
# The same ramp again for air below freezing, in blue. Precipitation at -3 C is
# a different thing to fly a drone through than precipitation at +3, and on a
# green map the two look identical. Lightness tracks the green ramp step for
# step so the two read at the same weight side by side; the top class stays
# orange, because at fifteen millimetres an hour the rate is the whole story.
SNOW_ANCHORS = ["#dfeeff", "#b5d5fb", "#7fb2ee", "#4587d6", "#1f5aa8", "#ff7800"]
# Where the changeover sits. Not 0.0: precipitation arriving through air at
# +0.5 C at two metres has usually fallen through something colder above and
# reaches the ground as wet snow or sleet — the surface is the last part of
# the column to warm up.
FREEZE_C = 1.0


def _draw_rain(ax, xw, yw, prm, t2m, greens, blues, rain_norm, alpha=0.9):
    """Precipitation in two passes, split at freezing.

    Two masked meshes rather than one recoloured array: the boundary between
    them is a front, not a pixel edge, so each pass keeps its own ramp and they
    meet wherever the isotherm happens to run. t2m of None means an older
    stored grid with no temperature in it, which draws exactly as before.
    """
    if t2m is None:
        ax.pcolormesh(xw, yw, prm, cmap=greens, norm=rain_norm, alpha=alpha,
                      shading="auto")
        return
    ax.pcolormesh(xw, yw, np.where(t2m >= FREEZE_C, prm, np.nan),
                  cmap=greens, norm=rain_norm, alpha=alpha, shading="auto")
    ax.pcolormesh(xw, yw, np.where(t2m < FREEZE_C, prm, np.nan),
                  cmap=blues, norm=rain_norm, alpha=alpha, shading="auto")


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
# ...and so is a ceiling sitting on the deck, whether or not the fog field
# agrees. MEPS's fog_area_fraction is strict: on the 30.07 06Z run only 1.7%
# of the area with a ceiling under 700 m passed it. Without this, a cell whose
# ceiling the model puts at 20 m would be drawn as an ordinary low ceiling.
FOG_CEILING_M = 60.0


def _gate_ceiling(cb, lcc, fog):
    """Cloud base -> aviation ceiling, plus a separate fog mask.

    Fog gets its own colour on the map because it is its own decision: a
    300 m base is a low ceiling you might still work under, fog is not.
    """
    import numpy as np
    cb = np.where(cb > FILL, np.nan, cb)
    cb = np.where(lcc >= CEILING_LCC, cb, np.nan)
    fogm = np.zeros(cb.shape, bool)
    if fog is not None:
        fogm |= fog >= FOG_COVER
    fogm |= np.isfinite(cb) & (cb < FOG_CEILING_M)
    cb = np.where(fogm, 0.0, cb)
    return cb, fogm

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


ARCHIVE_CAT = "https://thredds.met.no/thredds/catalog/meps25epsarchive"
ARCHIVE_DAP = "https://thredds.met.no/thredds/dodsC/meps25epsarchive"


def archive_runs(day: dt.date) -> list[str]:
    """Full OPeNDAP URLs for a past day's deterministic runs.

    mepslatest only holds about a day, which is why re-rendering history
    looked impossible. It is not: MET Norway keeps meps25epsarchive going back
    years, one directory per day, and the archived meps_det_2_5km files carry
    everything we draw — cloud_base_altitude, precipitation_amount_acc,
    low_type_cloud_area_fraction and fog_area_fraction, 67 time steps each.
    """
    r = session().get(f"{ARCHIVE_CAT}/{day:%Y/%m/%d}/catalog.xml", timeout=60)
    if r.status_code != 200:
        return []
    names = re.findall(r'<dataset name="(meps_det_2_5km_\d{8}T\d{2}Z\.nc)"', r.text)
    return [f"{ARCHIVE_DAP}/{day:%Y/%m/%d}/{n}" for n in sorted(set(names))]


def draw_hour(cb, fogm, pr, latw, lonw, t2m=None) -> dict:
    """One hour of fields -> {threshold: PNG bytes}, the shared drawing path.

    Used by the grid renderer. The two older paths still draw inline; they
    should end up here too, but they are working and this is new code, so the
    duplication is deliberate for now.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import BoundaryNorm, LinearSegmentedColormap, ListedColormap
    from PIL import Image as PILImage

    from . import proj3059 as P

    greens = ListedColormap(RAIN_ANCHORS)
    blues = ListedColormap(SNOW_ANCHORS)
    rain_norm = BoundaryNorm(RAIN_LEVELS, greens.N)
    pink = LinearSegmentedColormap.from_list("p", ["#f3b8b4", "#f3b8b4"])
    orange = LinearSegmentedColormap.from_list("o", ["#e8a33d", "#e8a33d"])
    borders = json.load(open(BORDERS_FILE))
    xw, yw = P.to_xy(lonw, latw)
    prm = np.where(pr >= 0.1, pr, np.nan)
    out = {}
    for thr in THRESHOLDS:
        fig = plt.figure(figsize=(P.W / 100, P.H / 100), dpi=100)
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_facecolor("#fbf3de")
        ax.set_xlim(P.X0, P.X1); ax.set_ylim(P.Y0, P.Y1)
        ax.set_aspect("equal"); ax.axis("off")
        fg = fogm if fogm is not None else np.zeros(cb.shape, bool)
        below = np.where(np.isfinite(cb) & (cb < thr) & ~fg, 1.0, np.nan)
        ax.pcolormesh(xw, yw, below, cmap=pink, vmin=0, vmax=1, alpha=0.75,
                      shading="auto")
        ax.pcolormesh(xw, yw, np.where(fg, 1.0, np.nan), cmap=orange, vmin=0,
                      vmax=1, alpha=0.8, shading="auto")
        _draw_rain(ax, xw, yw, prm, t2m, greens, blues, rain_norm)
        for c in borders:
            for line in c["lines"]:
                bx, by = P.to_xy([lo for lo, _ in line], [la for _, la in line])
                ax.plot(bx, by, color="#f7f1e2", lw=2.4, alpha=0.9,
                        solid_capstyle="round")
                ax.plot(bx, by, color="#55503f", lw=0.9, solid_capstyle="round")
        buf = io.BytesIO()
        fig.savefig(buf, dpi=100, format="png")
        plt.close(fig)
        buf.seek(0)
        q = io.BytesIO()
        PILImage.open(buf).convert("RGB").quantize(
            colors=192, dither=PILImage.Dither.NONE).save(q, "PNG", optimize=True)
        out[thr] = q.getvalue()
    return out


def backfill_runs(max_runs: int = 12, leads=(1, 2, 3),
                  datasets: list[str] | None = None) -> None:
    """Render short-lead frames from PAST runs still on thredds (the catalog
    keeps roughly the last day of runs) so today's already-passed hours get
    near-analysis maps. Registers them in maps/meps/archive.json; an hour is
    only overwritten by a NEWER run (shorter lead = better).

    BACKFILL_FORCE=1 re-renders hours that are already claimed, which is what
    you want after a change to the palette or to what gets drawn — otherwise
    the archived frames keep whatever they were rendered with."""
    force = os.environ.get("BACKFILL_FORCE", "") == "1"
    if force:
        log.info("FORCE: re-rendering hours already in the archive")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import BoundaryNorm, LinearSegmentedColormap, ListedColormap
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
    greens = ListedColormap(RAIN_ANCHORS)
    blues = ListedColormap(SNOW_ANCHORS)
    rain_norm = BoundaryNorm(RAIN_LEVELS, greens.N)
    pink = LinearSegmentedColormap.from_list("p", ["#f3b8b4", "#f3b8b4"])
    orange = LinearSegmentedColormap.from_list("o", ["#e8a33d", "#e8a33d"])
    tmp = tempfile.mkdtemp()

    runs = datasets if datasets is not None else list_runs()[:-1][-max_runs:]
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
            ds = nc.Dataset(ds_name if ds_name.startswith("http")
                            else DAP_BASE + ds_name)
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
            # Kelvin here; the drawing code works in Celsius.
            try:
                tair = np.array(
                    ds.variables["air_temperature_2m"][:n, 0, y0:y1 + 1, x0:x1 + 1]) - 273.15
            except Exception:
                tair = None
            terr = _terrain(ds, y0, y1, x0, x1)
            cb = _to_agl(cb, terr)
            times = nc.num2date(ds.variables["time"][:n], ds.variables["time"].units)
        except Exception:
            log.exception("meps backfill: %s failed to open/fetch", ds_name)
            continue
        cb, fogm = _gate_ceiling(cb, lcc, fog)
        xw, yw = P.to_xy(lonw, latw)
        borders_xy = [[P.to_xy([px for px, _ in line], [py for _, py in line])
                       for line in c["lines"]] for c in borders]

        for i in leads:
            if i >= len(times):
                continue
            valid = times[i]
            vtag = valid.strftime("%Y%m%dT%H")
            if not force and arch["hours"].get(vtag, "") >= run_tag:
                continue
            if i + 1 >= len(acc):
                continue
            # hour STARTING at the stamp — see the note in the live renderer
            pr = np.clip(acc[i + 1] - acc[i], 0, None)
            prm = np.where(pr >= 0.1, pr, np.nan)
            t2m = tair[i] if tair is not None else None
            for thr in THRESHOLDS:
                fig = plt.figure(figsize=(P.W / 100, P.H / 100), dpi=100)
                ax = fig.add_axes([0, 0, 1, 1])
                ax.set_facecolor("#fbf3de")
                ax.set_xlim(P.X0, P.X1); ax.set_ylim(P.Y0, P.Y1)
                ax.set_aspect("equal"); ax.axis("off")
                fg = fogm[i] if fogm is not None else np.zeros_like(cb[i], bool)
                below = np.where(np.isfinite(cb[i]) & (cb[i] < thr) & ~fg, 1.0, np.nan)
                ax.pcolormesh(xw, yw, below, cmap=pink, vmin=0, vmax=1,
                              alpha=0.75, shading="auto")
                # Fog in orange: a different decision from a low ceiling.
                ax.pcolormesh(xw, yw, np.where(fg, 1.0, np.nan), cmap=orange,
                              vmin=0, vmax=1, alpha=0.8, shading="auto")
                _draw_rain(ax, xw, yw, prm, t2m, greens, blues, rain_norm)
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
    from matplotlib.colors import BoundaryNorm, LinearSegmentedColormap, ListedColormap
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
    # Kelvin in the file; everything downstream works in Celsius.
    try:
        tair = np.array(
            ds.variables["air_temperature_2m"][:n, 0, y0:y1 + 1, x0:x1 + 1]) - 273.15
    except Exception:
        tair = None
    cb = _to_agl(cb, _terrain(ds, y0, y1, x0, x1))
    cb, fogm = _gate_ceiling(cb, lcc, fog)

    from . import proj3059 as P

    borders = json.load(open(BORDERS_FILE))
    greens = ListedColormap(RAIN_ANCHORS)
    blues = ListedColormap(SNOW_ANCHORS)
    rain_norm = BoundaryNorm(RAIN_LEVELS, greens.N)
    pink = LinearSegmentedColormap.from_list("p", ["#f3b8b4", "#f3b8b4"])
    orange = LinearSegmentedColormap.from_list("o", ["#e8a33d", "#e8a33d"])

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
        t2m = tair[i] if tair is not None else None
        for thr in THRESHOLDS:
            fig, ax = blank_fig()
            fg = fogm[i] if fogm is not None else np.zeros_like(cb[i], bool)
            below = np.where(np.isfinite(cb[i]) & (cb[i] < thr) & ~fg, 1.0, np.nan)
            ax.pcolormesh(xw, yw, below, cmap=pink, vmin=0, vmax=1,
                          alpha=0.75, shading="auto")
            ax.pcolormesh(xw, yw, np.where(fg, 1.0, np.nan), cmap=orange,
                          vmin=0, vmax=1, alpha=0.8, shading="auto")
            _draw_rain(ax, xw, yw, prm, t2m, greens, blues, rain_norm)
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
