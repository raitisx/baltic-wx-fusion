"""Scraped map layers: meteo.pl UM WMS tiles + meteolapa radar composites.

UM (ICM UW, meteo.pl) — POLITENESS RULES (requirements doc §5):
  - their own GWC tile cache is used (cheapest possible for them)
  - honest UA with contact email, sleep between tile requests
  - runs every 3 h only; treat as ADVISORY layer, licence talks ongoing
  Endpoint (discovered from mapy.meteo.pl app):
    https://mapy.meteo.pl/geoserver/gwc/service/wms
    layers um:UM4_CLOUD (4 km) / um:UM1_CLOUD (1.5 km, PL domain edge)
    tile-aligned EPSG:900913 requests, TIME=<run midnight Z>, DIM_FORECAST=<lead h>

Radar (meteolapa.lv fuses LVGMC LV + EE composites):
  georeferenced transparent overlays at
    https://www.meteolapa.lv/radari/arhivs/YYYY/MM/DD/<YYYYMMDDHHMM><CODE>.png
  bounds delivered in the page's LRadar.init() config. ~10 min cadence.
  Processing: source palette -> intensity classes -> our green scale;
  despeckle (kills isolated pixels / thin cone-beam spikes); optional
  temporal 2-of-3 filter across consecutive frames.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import logging
import math
import os
import re
import tempfile
import time

import boto3
import numpy as np
from PIL import Image

from . import config
from .http import session

log = logging.getLogger(__name__)

BBOX = (53.8, 59.9, 20.0, 28.6)  # lat_min, lat_max, lon_min, lon_max
GWC = "https://mapy.meteo.pl/geoserver/gwc/service/wms"
ZOOM = 6
TILE_SLEEP = 0.3
# meteo.pl's DIM_FORECAST is 1-based: DIM_FORECAST=1 is the run's first
# forecast hour, i.e. valid at run+0, so labelling it run+1 put every UM frame
# an hour late. Confirmed two ways — the trailing was visible against MEPS,
# and cross-correlating UM precipitation against the radar archive over the
# frames we have peaks at -1 h (mean IoU 0.030 vs 0.019 at 0 and 0.026 at +1).
UM_LEAD_OFFSET = 1

RADAR_PAGE = "https://www.meteolapa.lv/radars"
RADAR_BASE = "https://www.meteolapa.lv"

# Lithuania: LHMT composite (Laukuva + Vilnius), 5-min cadence, UTC stamps.
# Leaflet imageOverlay bounds from meteo.lt InteractiveMaps config.
LT_URL = ("https://new.meteo.lt/meteo_jobs/radaru_informacija/"
          "Header_Radar-composite-{ts}.png")
LT_SW = (49.876389, 15.618611)
LT_NE = (59.701667, 34.313611)
LT_MAXSIZE = 1400  # downscale the 4000px source before classification

# Our precip colours (match maps_meps.py greens)
GREEN_STEPS = [(200, 230, 176, 150), (137, 199, 116, 190), (87, 182, 71, 220),
               (28, 122, 28, 235), (11, 77, 11, 255), (5, 45, 5, 255)]


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


# ---------------------------------------------------------------- UM tiles
WORLD = 20037508.342789244


def _tile_bbox_900913(z, x, y):
    n = 2 ** z
    size = 2 * WORLD / n
    minx = -WORLD + x * size
    maxy = WORLD - y * size
    return (minx, maxy - size, minx + size, maxy)


def _tile_range(z):
    n = 2 ** z
    def lon2x(lon): return int((lon + 180) / 360 * n)
    def lat2y(lat):
        r = math.radians(lat)
        return int((1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * n)
    return (lon2x(BBOX[2]), lon2x(BBOX[3]), lat2y(BBOX[1]), lat2y(BBOX[0]))


def _tilegrid_bounds_latlon():
    """(sw, ne) lat/lon of the stitched tile canvas."""
    x0, x1, y0, y1 = _tile_range(ZOOM)
    n = 2 ** ZOOM
    def x2lon(x): return x / n * 360 - 180
    def y2lat(y):
        t = math.pi * (1 - 2 * y / n)
        return math.degrees(math.atan(math.sinh(t)))
    return (y2lat(y1 + 1), x2lon(x0)), (y2lat(y0), x2lon(x1 + 1))


def fetch_um_frame(layer: str, run_midnight: dt.datetime, lead_h: int) -> Image.Image | None:
    """Stitch tile grid for one lead hour. Returns mercator-stitched image."""
    s = session()
    x0, x1, y0, y1 = _tile_range(ZOOM)
    cols, rows = x1 - x0 + 1, y1 - y0 + 1
    out = Image.new("RGBA", (cols * 256, rows * 256), (0, 0, 0, 0))
    got = 0
    for yi in range(y0, y1 + 1):
        for xi in range(x0, x1 + 1):
            bb = _tile_bbox_900913(ZOOM, xi, yi)
            r = s.get(GWC, params={
                "service": "WMS", "version": "1.1.1", "request": "GetMap",
                "layers": layer, "styles": "",
                "bbox": ",".join(f"{v:.9f}" for v in bb),
                "width": 256, "height": 256, "srs": "EPSG:900913",
                "format": "image/png", "transparent": "true", "tiled": "true",
                "TIME": run_midnight.strftime("%Y-%m-%dT%H:%M:%S") + "Z",
                "DIM_FORECAST": lead_h,
            }, timeout=60)
            time.sleep(TILE_SLEEP)
            if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image"):
                continue
            out.paste(Image.open(io.BytesIO(r.content)).convert("RGBA"),
                      ((xi - x0) * 256, (yi - y0) * 256))
            got += 1
    if got == 0:
        return None
    return out


def um_reindex(name: str = "um4") -> int:
    """Rebuild maps/<name>/archive.json from what is actually in the bucket.

    UM frames were always written to maps/<name>/<run>/<hour>.png but until the
    archive index existed only the newest run was reachable, so earlier days
    silently disappeared from the viewer even though the PNGs were there. This
    recovers them. Newer run wins for any hour covered more than once, matching
    um_run and the MEPS convention.
    """
    s3 = r2_client()
    hours: dict[str, str] = {}
    token = None
    seen = 0
    while True:
        kw = {"Bucket": config.R2_BUCKET, "Prefix": f"maps/{name}/"}
        if token:
            kw["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kw)
        for obj in resp.get("Contents", []):
            m = re.match(rf"maps/{name}/(\d{{8}}T\d{{2}})/(\d{{8}}T\d{{2}})\.png$", obj["Key"])
            if not m:
                continue
            seen += 1
            run_tag, vtag = m.group(1), m.group(2)
            if hours.get(vtag, "") <= run_tag:
                hours[vtag] = run_tag
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")

    s3.put_object(Bucket=config.R2_BUCKET, Key=f"maps/{name}/archive.json",
                  Body=json.dumps({"hours": hours}).encode(),
                  ContentType="application/json",
                  CacheControl="public, max-age=300")
    log.info("%s reindex: %d frames in the bucket -> %d hours indexed",
             name, seen, len(hours))
    return len(hours)


def um_run(hours: list[int] | None = None) -> None:
    """Fetch UM4 cloud/precip frames for the given lead hours.

    Leads measured against the live tile server (28.07.2026, 00Z run): real,
    distinct content through +120 h, fully transparent from +123 h. We were
    only asking for +60 h, so half the available range was being discarded.
    Hourly through the first day, then coarsening — the far leads are for
    planning shape, not detail.

    UM1 (the 1.5 km layer) is not fetched: its domain is Poland, so every
    frame came back completely transparent over the Baltic bbox at every lead
    tested. Re-add ("um1", "um:UM1_CLOUD") below if that ever changes.
    """
    hours = hours or (list(range(1, 25))
                      + list(range(27, 73, 3))
                      + list(range(78, 121, 6)))
    now = dt.datetime.now(dt.timezone.utc)
    run = now.replace(hour=0, minute=0, second=0, microsecond=0)
    s3 = r2_client()
    for name, layer in [("um4", "um:UM4_CLOUD")]:
        run_tag = run.strftime("%Y%m%dT%H")
        done = []
        from . import proj3059 as P
        sw, ne = _tilegrid_bounds_latlon()
        for h in hours:
            img = fetch_um_frame(layer, run, h)
            if img is None:
                log.warning("%s: no tiles for +%dh", name, h)
                continue
            # Past the model horizon the tile server keeps answering, just with
            # fully transparent tiles. Storing those would index blank frames
            # that look like a broken viewer, so stop at the first empty one.
            probe = np.array(img)
            if probe.shape[-1] == 4 and not probe[..., 3].any():
                log.info("%s: +%dh is empty — horizon reached", name, h)
                break
            # reproject the mercator-stitched canvas onto the common
            # EPSG:3059 grid (transparent overlay; client stacks it on
            # maps/bg_3059.png) and bake borders ON TOP — the UM cloud
            # layer is opaque, so background borders would be hidden.
            arr = P.resample_mercator_image(np.array(img), sw, ne)
            img = Image.fromarray(arr, "RGBA")
            from PIL import ImageDraw
            borders = json.load(open(os.path.join(
                os.path.dirname(__file__), "assets", "borders_baltic.json")))
            d = ImageDraw.Draw(img)
            for c in borders:
                for line in c["lines"]:
                    px, py = P.to_px([la for _, la in line],
                                     [lo for lo, _ in line])
                    pts = list(zip(px.tolist(), py.tolist()))
                    d.line(pts, fill=(247, 241, 226, 230), width=3)
                    d.line(pts, fill=(85, 80, 63, 255), width=1)
            valid = run + dt.timedelta(hours=h - UM_LEAD_OFFSET)
            vtag = valid.strftime("%Y%m%dT%H")
            buf = io.BytesIO()
            img.save(buf, "PNG", optimize=True)
            s3.put_object(Bucket=config.R2_BUCKET,
                          Key=f"maps/{name}/{run_tag}/{vtag}.png",
                          Body=buf.getvalue(), ContentType="image/png",
                          CacheControl="public, max-age=86400")
            done.append(valid.strftime("%Y-%m-%dT%H:00:00Z"))
        # Persistent hour -> run index, same idea as maps/meps/archive.json.
        # Frames are never deleted from the bucket, but without this only the
        # newest run's hours were reachable, so yesterday's UM simply vanished
        # from the viewer. An hour is only overwritten by a newer run.
        if done:
            try:
                arch = json.loads(s3.get_object(
                    Bucket=config.R2_BUCKET, Key=f"maps/{name}/archive.json"
                )["Body"].read())
            except Exception:
                arch = {"hours": {}}
            for iso in done:
                vtag = iso[0:4] + iso[5:7] + iso[8:10] + "T" + iso[11:13]
                if arch["hours"].get(vtag, "") <= run_tag:
                    arch["hours"][vtag] = run_tag
            s3.put_object(Bucket=config.R2_BUCKET, Key=f"maps/{name}/archive.json",
                          Body=json.dumps(arch).encode(),
                          ContentType="application/json",
                          CacheControl="public, max-age=300")
            log.info("%s: archive index now %d hours", name, len(arch["hours"]))

        if done:
            s3.put_object(Bucket=config.R2_BUCKET, Key=f"maps/{name}/latest.json",
                          Body=json.dumps({
                              "run": run_tag, "path": f"maps/{name}/{run_tag}",
                              "hours": done, "thresholds": None,
                              "proj": "epsg3059", "overlay": True,
                              "credit": "ICM UW meteo.pl UM (advisory, scraped tiles)",
                              "generated_at": now.isoformat()}).encode(),
                          ContentType="application/json",
                          CacheControl="public, max-age=300")
            log.info("%s: %d frames", name, len(done))


# ---------------------------------------------------------------- radar
def parse_radar_overlays() -> list[dict]:
    """Live page overlays (LV, EE, LT2). Frame time comes from the createdAt
    epoch — the FILENAME timestamps are Riga local time, do not parse them."""
    r = session().get(RADAR_PAGE, timeout=60)
    r.raise_for_status()
    out = []
    pat = re.compile(
        r'LMapOverlay\((\{"sw".*?\}\}),\s*"([^"]+)",[^,]+,[^,]+,[^,]+,\s*(\{.*?\})\)',
        re.S)
    for m in pat.finditer(r.text):
        bounds = json.loads(m.group(1))
        meta = json.loads(m.group(3))
        code = meta.get("code")
        created = meta.get("createdAt")
        if not code or not created:
            continue
        out.append({
            "url": m.group(2) if m.group(2).startswith("http")
                   else RADAR_BASE + m.group(2),
            "code": code,
            "time": dt.datetime.fromtimestamp(int(created), tz=dt.timezone.utc),
            "sw": (bounds["sw"]["lat"], bounds["sw"]["lng"]),
            "ne": (bounds["ne"]["lat"], bounds["ne"]["lng"]),
        })
    return out


def fetch_overlays_window(t_from: dt.datetime, t_to: dt.datetime) -> list[dict]:
    """Historical overlays via the site's own /radari/get endpoint (epoch
    range). Returns the same dict shape as parse_radar_overlays; URLs point
    at their S3 archive, which reaches back months."""
    r = session().get(
        f"{RADAR_BASE}/radari/get",
        params={"from": int(t_from.timestamp()), "to": int(t_to.timestamp())},
        headers={"X-Requested-With": "XMLHttpRequest"},
        timeout=90,
    )
    r.raise_for_status()
    d = r.json()
    out = []
    for o in d.get("overlays") or []:
        b = o.get("mapBounds") or {}
        if "sw" not in b:
            continue
        out.append({
            "url": o["url"] if o["url"].startswith("http")
                   else RADAR_BASE + o["url"],
            "code": o.get("code", "?"),
            "time": dt.datetime.fromtimestamp(int(o["createdAt"]),
                                              tz=dt.timezone.utc),
            "sw": (b["sw"]["lat"], b["sw"]["lng"]),
            "ne": (b["ne"]["lat"], b["ne"]["lng"]),
        })
    return out


def _classify_intensity(arr: np.ndarray) -> np.ndarray:
    """Source palette RGBA -> intensity class 0..6 (0 = no echo).

    LVGMC/EE palettes run blue -> green/yellow -> orange -> red with
    increasing intensity. Classify by hue+value rather than exact colours so
    both feeds work.
    """
    r = arr[..., 0].astype(int)
    g = arr[..., 1].astype(int)
    b = arr[..., 2].astype(int)
    a = arr[..., 3]
    echo = a > 60
    # exclude near-neutral pixels (LT out-of-range gray, basemap tints)
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    echo = echo & ((mx - mn) > 25)
    cls = np.zeros(arr.shape[:2], dtype=np.uint8)
    # LT teal/cyan (weak-moderate)
    teal = echo & (r < 100) & (g > 120) & (b > 120)
    cls[teal & (g >= 185)] = 1
    cls[teal & (g < 185)] = 2
    blueish = echo & ~teal & (b > r) & (b > g)
    cls[blueish & (b < 180)] = 2          # darker blue = a bit more
    cls[blueish & (b >= 180)] = 1         # light blue = weak
    greenish = echo & (g >= r) & (g > b)
    cls[greenish] = 3
    yellow = echo & (r > 150) & (g > 120) & (b < 120) & (g >= r - 60)
    cls[yellow] = 4
    orange = echo & (r > 180) & (g > 60) & (g <= 160) & (b < 100)
    cls[orange] = 5
    red = echo & (r > 150) & (g <= 60) & (b < 100)
    cls[red] = 6
    return cls


# Baltic weather radar sites. Interference beams are always radial from an
# antenna, which is a far stronger discriminator than "looks like a line" —
# real squall lines have no reason to point at a radar. Surgavere was
# confirmed empirically: extending every thin streak found across 40 archived
# frames and clustering the pairwise intersections put the densest cluster
# 5 km from it, and it accounts for nearly all the beams we see. The rest are
# the published site positions; an inaccurate one can only cause a missed
# beam, never removal of real echo, because a candidate must already be thin,
# straight and outside thick echo before the radial test is even applied.
RADAR_SITES = [
    ("EE Surgavere", 58.482, 25.519),
    ("EE Harku", 59.398, 24.603),
    ("LV Riga", 56.918, 24.062),
    ("LT Laukuva", 55.613, 22.228),
    ("LT Vilnius", 54.652, 25.284),
]
RADIAL_TOL_KM = 20      # how close the beam's line must pass to the antenna
RADAR_RANGE_KM = 320    # beyond this a site cannot be the source


def _site_pixels():
    from . import proj3059 as P
    out = []
    for name, la, lo in RADAR_SITES:
        x, y = P.to_px([la], [lo])
        out.append((name, float(x[0]), float(y[0])))
    return out


def _radial_source(x0, y0, x1, y1, sites):
    """Which radar, if any, this segment points at. 1 px = 1 km."""
    dx, dy = x1 - x0, y1 - y0
    length = (dx * dx + dy * dy) ** 0.5
    if length < 1:
        return None
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    for name, sx, sy in sites:
        # perpendicular distance from the antenna to the segment's line
        perp = abs(dx * (sy - y0) - dy * (sx - x0)) / length
        if perp > RADIAL_TOL_KM:
            continue
        if ((mx - sx) ** 2 + (my - sy) ** 2) ** 0.5 > RADAR_RANGE_KM:
            continue
        return name
    return None


N_AZ = 720          # 0.5 deg azimuth bins
R_MIN_KM, R_MAX_KM = 8, 300
BEAM_COVER = 0.40   # fraction of the ray's range bins that must hold echo
BEAM_RATIO = 3.0    # ... and how far above the neighbouring azimuths
WIDE_AZ_BINS = 12   # echo spanning >6 deg at a given range is weather, not a beam
STRONG_CLASS = 4    # interference is weak and flat; a bright core is real


def _remove_beams_polar(cls: np.ndarray) -> np.ndarray:
    """Remove interference spikes using the geometry that defines them.

    A spike is a single azimuth from one antenna lit up along most of its
    range, while its neighbouring azimuths are empty. That is very hard to
    confuse with weather, and unlike a thinness test it does not care how many
    pixels wide the ray happens to render — which is why the previous
    thin-residue approach missed these: at 3+ px they survived the opening and
    were never even considered.

    Real echo is protected two ways: an azimuth only counts as a spike if it
    stands well clear of its neighbours, and within a flagged azimuth a pixel
    is only dropped if the echo at that range does not also extend across
    several degrees either side.
    """
    try:
        from scipy import ndimage
    except ImportError:
        log.warning("scipy missing - polar beam removal skipped")
        return cls

    echo = cls > 0
    if echo.sum() < 200:
        return cls
    h, w = cls.shape
    yy, xx = np.mgrid[0:h, 0:w]
    out = cls.copy()
    removed_by = {}

    for name, sx, sy in _site_pixels():
        dx, dy = xx - sx, yy - sy
        rng = np.hypot(dx, dy)
        in_range = (rng >= R_MIN_KM) & (rng <= R_MAX_KM)
        if not in_range.any():
            continue
        az = ((np.arctan2(dy, dx) + np.pi) / (2 * np.pi) * N_AZ).astype(int) % N_AZ
        rb = rng.astype(int)

        cur = out > 0
        lit = cur & in_range
        if lit.sum() < 50:
            continue
        # how much of each ray carries echo
        counts = np.bincount(az[lit], minlength=N_AZ).astype(float)
        totals = np.bincount(az[in_range], minlength=N_AZ).astype(float)
        cover = counts / np.maximum(totals, 1)
        # neighbourhood background, wrapping at north
        bg = ndimage.median_filter(cover, size=13, mode="wrap")
        spike = (cover > BEAM_COVER) & (cover > bg * BEAM_RATIO + 0.04)
        if not spike.any():
            continue
        spike = ndimage.binary_dilation(spike, np.ones(3), border_value=0)

        # azimuthal width of echo at each range, to spare genuine weather
        occ = np.zeros((N_AZ, R_MAX_KM + 1), bool)
        occ[az[lit], rb[lit]] = True
        wide = ndimage.uniform_filter(
            occ.astype(float), size=(WIDE_AZ_BINS, 5), mode="wrap") > 0.45

        # Never take a bright core, and leave whole rays alone when they
        # carry one — a squall line can lie radially by chance, and losing
        # real convection is far worse than leaving a streak on the map.
        strong_az = np.zeros(N_AZ, bool)
        strong = lit & (out >= STRONG_CLASS)
        if strong.any():
            strong_az[np.unique(az[strong])] = True
            strong_az = ndimage.binary_dilation(strong_az, np.ones(5), border_value=0)

        cand = (lit & spike[az] & ~strong_az[az]
                & ~wide[az, np.clip(rb, 0, R_MAX_KM)]
                & (out < STRONG_CLASS))
        if cand.sum() < 20:
            continue
        out[cand] = 0
        removed_by[name] = int(cand.sum())

    if removed_by:
        log.info("polar beam removal: %s",
                 ", ".join(f"{k} {v} px" for k, v in sorted(removed_by.items())))
    return out


def _remove_spikes(cls: np.ndarray) -> np.ndarray:
    """Kill interference beams: thin straight rays radiating from a radar.

    'Thin residue' = echo minus a 3x3 opening, i.e. anything under 3 px thick.
    Probabilistic Hough finds straight lines in it, which also bridges the
    dashed beams. A candidate is removed only if it is both outside thick
    (real) echo and radially aligned with a known antenna — the geometric
    prior that makes this safe. Detection is deliberately more sensitive than
    it used to be, because the radial test now does the discriminating.
    """
    try:
        from scipy import ndimage
        from skimage.transform import probabilistic_hough_line
        from skimage.draw import line as skline
    except ImportError:
        log.warning("scipy/skimage missing - beam removal skipped")
        return cls
    echo = cls > 0
    if not echo.any():
        return cls
    opened = ndimage.binary_opening(echo, structure=np.ones((3, 3)))
    thick = ndimage.binary_dilation(opened, structure=np.ones((3, 3)))
    thin = echo & ~opened
    if thin.sum() < 30:
        return cls
    segs = probabilistic_hough_line(thin, threshold=10, line_length=25, line_gap=6)
    if not segs:
        return cls

    sites = _site_pixels()
    kill = np.zeros_like(echo)
    hits = {}
    for (x0, y0), (x1, y1) in segs:
        rr, cc = skline(int(y0), int(x0), int(y1), int(x1))
        rr = np.clip(rr, 0, echo.shape[0] - 1)
        cc = np.clip(cc, 0, echo.shape[1] - 1)
        if thick[rr, cc].mean() >= 0.25:
            continue                      # sitting inside real echo
        src = _radial_source(x0, y0, x1, y1, sites)
        if src is None:
            continue                      # straight, but not pointing at a radar
        kill[rr, cc] = True
        hits[src] = hits.get(src, 0) + 1
    if not hits:
        return cls
    kill = ndimage.binary_dilation(kill, structure=np.ones((5, 5)))
    out = cls.copy()
    out[kill & thin] = 0
    log.info("beam removal: %d px from %s",
             int((kill & thin).sum()),
             ", ".join(f"{k} x{v}" for k, v in sorted(hits.items())))
    return out

def _despeckle(cls: np.ndarray, min_neighbors: int = 2) -> np.ndarray:
    """Remove isolated pixels & thin spikes (cone-beam artifacts):
    keep an echo pixel only if enough of its 8 neighbours also have echo."""
    e = (cls > 0).astype(np.uint8)
    n = np.zeros_like(e, dtype=np.uint8)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy == 0 and dx == 0:
                continue
            n += np.roll(np.roll(e, dy, 0), dx, 1)
    keep = e.astype(bool) & (n >= min_neighbors)
    out = cls.copy()
    out[~keep] = 0
    return out


def _recolor(cls: np.ndarray) -> Image.Image:
    out = np.zeros((*cls.shape, 4), dtype=np.uint8)
    for i, rgba in enumerate(GREEN_STEPS, start=1):
        out[cls == i] = rgba
    return Image.fromarray(out, "RGBA")


def _lt_frames(newest: dt.datetime, count: int = 3, step_min: int = 5) -> list[dict]:
    """Recent LT composite frames as overlay dicts (probing 5-min stamps)."""
    s = session()
    out = []
    t = newest.replace(second=0, microsecond=0)
    t -= dt.timedelta(minutes=t.minute % step_min)
    tried = 0
    while len(out) < count and tried < count * 4:
        url = LT_URL.format(ts=t.strftime("%Y%m%d%H%M"))
        r = s.head(url, allow_redirects=True, timeout=30)
        time.sleep(0.15)
        if r.status_code == 200:
            out.append({"url": url, "code": "LT", "time": t,
                        "sw": LT_SW, "ne": LT_NE})
        t -= dt.timedelta(minutes=step_min)
        tried += 1
    return out


def _load_and_classify(url: str, s) -> np.ndarray | None:
    r = s.get(url, allow_redirects=True, timeout=90)
    time.sleep(0.2)
    if r.status_code != 200:
        return None
    img = Image.open(io.BytesIO(r.content)).convert("RGBA")
    if max(img.size) > LT_MAXSIZE:
        sc = LT_MAXSIZE / max(img.size)
        img = img.resize((int(img.width * sc), int(img.height * sc)),
                         Image.NEAREST)
    return _classify_intensity(np.array(img))


def radar_composite(frames_back: int = 3) -> None:
    """Latest LV+EE+LT frames -> cleaned, recoloured Baltic composite in R2."""
    overlays = parse_radar_overlays()
    if not overlays:
        raise RuntimeError("no radar overlays parsed from meteolapa")
    s = session()
    s3 = r2_client()
    by_code: dict[str, list[dict]] = {}
    for o in sorted(overlays, key=lambda o: o["time"], reverse=True):
        by_code.setdefault(o["code"], []).append(o)
    # meteolapa carries LT2 itself; direct meteo.lt only as fallback
    if "LT2" not in by_code:
        try:
            lt = _lt_frames(dt.datetime.now(dt.timezone.utc), count=frames_back)
            if lt:
                by_code["LT"] = lt
        except Exception:
            log.exception("LT radar fallback failed (continuing without LT)")

    # composite on the common EPSG:3059 grid (transparent overlay)
    from . import proj3059 as P
    canvas = Image.new("RGBA", (P.W, P.H), (0, 0, 0, 0))
    newest = None
    for code, frames in sorted(by_code.items()):  # EE first, LV drawn on top
        use = frames[:frames_back]
        if not use:
            continue
        newest = max(newest or use[0]["time"], use[0]["time"])
        cls_stack = []
        for f in use:
            cls = _load_and_classify(f["url"], s)
            if cls is not None:
                cls_stack.append((cls, f))
        if not cls_stack:
            continue
        cur, f0 = cls_stack[0]
        # temporal filter: echo must appear in >=2 of last frames (same feed)
        if len(cls_stack) >= 2:
            votes = sum((c[0] > 0).astype(np.uint8) for c in cls_stack
                        if c[0].shape == cur.shape)
            cur = np.where(votes >= 2, cur, 0).astype(np.uint8)
        cur = _remove_beams_polar(_remove_spikes(_despeckle(cur)))
        colored = np.array(_recolor(cur))
        warped = P.resample_mercator_image(colored, f0["sw"], f0["ne"])
        canvas.alpha_composite(Image.fromarray(warped, "RGBA"))

    if newest is None:
        raise RuntimeError("no radar frames fetched")
    # thin borders on the overlay itself (readable even without background)
    from PIL import ImageDraw
    borders_file = os.path.join(os.path.dirname(__file__), "assets",
                                "borders_baltic.json")
    draw = ImageDraw.Draw(canvas)
    for c in json.load(open(borders_file)):
        for line in c["lines"]:
            px, py = P.to_px([latv for _, latv in line],
                             [lonv for lonv, _ in line])
            draw.line(list(zip(px.tolist(), py.tolist())),
                      fill=(107, 101, 82, 255), width=1)
    vtag = newest.strftime("%Y%m%dT%H%M")
    buf = io.BytesIO()
    canvas.save(buf, "PNG", optimize=True)
    s3.put_object(Bucket=config.R2_BUCKET, Key=f"maps/radar/frames3059/{vtag}.png",
                  Body=buf.getvalue(), ContentType="image/png",
                  CacheControl="public, max-age=3600")
    # manifest: keep the trailing 12 frames
    try:
        old = json.loads(s3.get_object(
            Bucket=config.R2_BUCKET, Key="maps/radar/latest.json"
        )["Body"].read())
        # v2: frame tags derive from createdAt (UTC); older tags were built
        # from local-time filenames and are mislabeled — drop them.
        frames = old.get("frames", []) \
            if old.get("path") == "maps/radar/frames3059" and old.get("v") == 2 \
            else []
    except Exception:
        frames = []
    if vtag not in frames:
        frames.append(vtag)
    frames = sorted(frames)[-12:]
    s3.put_object(Bucket=config.R2_BUCKET, Key="maps/radar/latest.json",
                  Body=json.dumps({
                      "path": "maps/radar/frames3059", "frames": frames,
                      "v": 2, "proj": "epsg3059", "overlay": True,
                      "bbox": BBOX,
                      "credit": "LVGMC + Keskkonnaagentuur radar via meteolapa.lv, "
                                "recoloured & despeckled",
                      "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                  }).encode(), ContentType="application/json",
                  CacheControl="public, max-age=120")
    log.info("radar composite %s stored (%d frames in manifest)", vtag, len(frames))
    _archive_note(s3, vtag)


def _archive_note(s3, vtag: str) -> None:
    """Keep a permanent hourly index: first stored frame of each hour goes
    into maps/radar/archive.json (frames themselves are never deleted)."""
    hour = vtag[:11]  # YYYYMMDDTHH
    try:
        arch = json.loads(s3.get_object(
            Bucket=config.R2_BUCKET, Key="maps/radar/archive.json"
        )["Body"].read())
    except Exception:
        arch = {"path": "maps/radar/frames3059", "hours": {}}
    if hour not in arch["hours"]:
        arch["hours"][hour] = vtag
        s3.put_object(Bucket=config.R2_BUCKET, Key="maps/radar/archive.json",
                      Body=json.dumps(arch).encode(),
                      ContentType="application/json",
                      CacheControl="public, max-age=300")


def _hour_covered(arch: dict, hour: dt.datetime) -> bool:
    """An hour counts as covered only if its archived frame sits within 40 min
    of :00. A frame the live job happened to grab at :49 is better than
    nothing, but it should still be upgraded to an hour-aligned one."""
    tag = arch["hours"].get(hour.strftime("%Y%m%dT%H"))
    return bool(tag) and int(tag[11:13] or 0) <= 40


def radar_backfill(hours_back: int = 168) -> None:
    """One composite per hour from meteolapa's /radari/get archive (their S3
    store reaches back months, all three feeds). Idempotent: hours already
    covered by an hour-aligned frame are skipped."""
    s = session()
    s3 = r2_client()
    try:
        arch = json.loads(s3.get_object(
            Bucket=config.R2_BUCKET, Key="maps/radar/archive.json"
        )["Body"].read())
    except Exception:
        arch = {"path": "maps/radar/frames3059", "hours": {}}

    from . import proj3059 as P
    from PIL import ImageDraw
    borders = json.load(open(os.path.join(
        os.path.dirname(__file__), "assets", "borders_baltic.json")))

    def draw_borders(canvas):
        d = ImageDraw.Draw(canvas)
        for c in borders:
            for line in c["lines"]:
                px, py = P.to_px([la for _, la in line], [lo for lo, _ in line])
                pts = list(zip(px.tolist(), py.tolist()))
                d.line(pts, fill=(247, 241, 226, 230), width=3)
                d.line(pts, fill=(85, 80, 63, 255), width=1)

    force = os.environ.get("BACKFILL_FORCE", "") == "1"
    if force:
        log.info("FORCE mode: re-rendering hours already in the archive")
    now = dt.datetime.now(dt.timezone.utc).replace(
        minute=0, second=0, microsecond=0)
    done = 0
    # walk backwards in 6 h windows; one /radari/get call per window
    win = 6
    for w0 in range(2, hours_back + 1, win):
        t_hi = now - dt.timedelta(hours=w0 - 1)
        t_lo = now - dt.timedelta(hours=min(w0 + win - 1, hours_back))
        hours_needed = [t_lo + dt.timedelta(hours=k)
                        for k in range(int((t_hi - t_lo).total_seconds() // 3600) + 1)]
        # Skip a window only if every hour in it is *properly* covered. Testing
        # mere presence let an hour whose only frame sat at :49 keep the whole
        # window from being revisited, so the per-hour upgrade never ran.
        if not force and all(_hour_covered(arch, h) for h in hours_needed):
            continue
        try:
            overlays = fetch_overlays_window(t_lo, t_hi + dt.timedelta(minutes=30))
        except Exception:
            log.exception("window %s..%s failed", t_lo, t_hi)
            continue
        time.sleep(0.5)
        for target in hours_needed:
            hour_key = target.strftime("%Y%m%dT%H")
            if not force and _hour_covered(arch, target):
                continue
            canvas = Image.new("RGBA", (P.W, P.H), (0, 0, 0, 0))
            got = False
            for code in ("EE", "LT2", "LT", "LV"):  # LV drawn last, on top
                cands = [o for o in overlays if o["code"] == code]
                if not cands:
                    continue
                best = min(cands,
                           key=lambda o: abs((o["time"] - target).total_seconds()))
                if abs((best["time"] - target).total_seconds()) > 40 * 60:
                    continue
                cls = _load_and_classify(best["url"], s)
                if cls is None:
                    continue
                warped = P.resample_mercator_image(
                    np.array(_recolor(_remove_beams_polar(_remove_spikes(_despeckle(cls))))),
                    best["sw"], best["ne"])
                canvas.alpha_composite(Image.fromarray(warped, "RGBA"))
                got = True
            if not got:
                log.info("backfill %s: no frames in archive", hour_key)
                continue
            draw_borders(canvas)
            vtag = target.strftime("%Y%m%dT%H%M")
            buf = io.BytesIO()
            canvas.save(buf, "PNG", optimize=True)
            s3.put_object(Bucket=config.R2_BUCKET,
                          Key=f"maps/radar/frames3059/{vtag}.png",
                          Body=buf.getvalue(), ContentType="image/png",
                          CacheControl="public, max-age=604800")
            arch["hours"][hour_key] = vtag
            done += 1
        # persist index incrementally so an interrupted run keeps progress
        s3.put_object(Bucket=config.R2_BUCKET, Key="maps/radar/archive.json",
                      Body=json.dumps(arch).encode(),
                      ContentType="application/json",
                      CacheControl="public, max-age=300")
    log.info("radar backfill: %d hourly composites added (archive now %d h)",
             done, len(arch["hours"]))
