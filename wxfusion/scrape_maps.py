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
import functools
import io
import json
import logging
import math
import os
import pathlib
import re
import tempfile
import time

import boto3
import numpy as np
from PIL import Image

from . import config
from .http import session
from .maps_meps import (RAIN_LEVELS, RAIN_ANCHORS, SNOW_ANCHORS,
                        pos_rain, rain_pos as _ramp_pos, ramp_rgb)

log = logging.getLogger(__name__)

BBOX = (53.8, 59.9, 20.0, 28.6)  # lat_min, lat_max, lon_min, lon_max
# How many CONSECUTIVE empty leads end a UM run. One is not enough: ICM
# serves isolated blank leads mid-forecast (3, 6 and 12 as of 03.09.2026),
# and treating the first as the horizon truncated the scrape to two frames.
UM_HORIZON_BLANKS = 4
# Re-publish the UM index every this many stored frames, so a scrape
# killed by the job timeout still leaves its frames reachable.
UM_FLUSH_EVERY = 12
GWC = "https://mapy.meteo.pl/geoserver/gwc/service/wms"
ZOOM = 6
TILE_SLEEP = 0.3
# DIM_FORECAST counts hours FROM the TIME we pass, starting at zero. Straight
# from maps.meteo.pl's own code (dist/geo.min.js):
#
#     TIME: t.utc().format(...) + "Z",
#     DIM_FORECAST = a.diff(t, "h")
#
# where t is the simulation start and a the moment being displayed. So
# valid = TIME + DIM_FORECAST, full stop.
#
# We had this as 1-based on the strength of cross-correlating UM precipitation
# against the radar archive, which put the peak at -1 h. That was five or six
# usable pairs of very small rain areas; it was noise, and it made every UM
# frame an hour early. The site's own arithmetic settles it.
#
# DIM_FORECAST=0 does come back transparent, which is what led me astray — but
# the app never asks for it either (initSelectedDay carries firstStep: 1), so
# the layer simply has nothing at the simulation's own hour.
UM_LEAD_OFFSET = 0

RADAR_PAGE = "https://www.meteolapa.lv/radars"
RADAR_BASE = "https://www.meteolapa.lv"

# Lithuania: LHMT composite (Laukuva + Vilnius), 5-min cadence, UTC stamps.
# Leaflet imageOverlay bounds from meteo.lt InteractiveMaps config.
LT_URL = ("https://new.meteo.lt/meteo_jobs/radaru_informacija/"
          "Header_Radar-composite-{ts}.png")
LT_SW = (49.876389, 15.618611)
LT_NE = (59.701667, 34.313611)
LT_MAXSIZE = 1400  # downscale the 4000px source before classification

# Rain colours, measured off a meteo.pl sample rather than guessed. Counting
# the saturated pixels in their UM frame, the ramp is:
#
#   #fcffad  pale cream-yellow   the lightest rain
#   #e6ff78  yellow-green
#   #00e300  bright green
#   #00a800  green
#   #006709  dark green
#   #ff7800  orange              the heavy cores  (833 px, the modal colour)
#   #cc2222  red                 rare, 112 px in the whole frame
#
# The thing I had wrong: there is no yellow in the MIDDLE. Their yellow tones
# are the weakest rain and the scale darkens through green before jumping to
# orange, so a heavy core reads as orange against dark green. Ours put yellow
# at step 4 of 6, which is why the middle of a rain area came out yellow.
RAIN_STEPS = [(252, 255, 173, 170), (230, 255, 120, 200), (0, 227, 0, 225),
              (0, 168, 0, 240), (0, 103, 9, 250), (255, 120, 0, 255)]

# Sub-class resolution. Everything downstream of classification — despeckling,
# beam removal, the archive, the verifier — is written in terms of the six
# intensity classes and stays that way. What changed is that a pixel now
# carries where inside its class it sits, so the recolour can draw the ramp
# continuously instead of six flat bands. Six bands was the reason a radar
# frame and the MEPS frame beside it did not look like the same quantity even
# when they agreed: the model map was a smooth field and the radar was a
# contour chart of it.
#
# 16 sub-steps per class. Finer than anything the feeds themselves resolve —
# their own ramps are 10 to 15 steps for the whole range — so this is about
# the drawing, not about pretending to precision. 96 levels leaves the rain
# and snow ramps together at 192 colours, still inside the 256 a palette PNG
# can hold, which is what the archive re-encodes to.
SUB = 16
LEV_MAX = 6 * SUB
lev_class = lambda lev: (np.asarray(lev).astype(np.int16) + SUB - 1) // SUB


def rate_to_lev(mm):
    """mm/h -> level 1..48 on the shared ramp.

    Levels are half-open like the classes were: a rate sitting exactly on a
    class edge is the first level of the class ABOVE it, which is why this
    floors and adds one rather than rounding. Level k*SUB is then the last
    level of class k, and lev_class() recovers the class exactly."""
    pos = np.asarray(_ramp_pos(mm), dtype=np.float64)
    return np.clip(np.floor(pos * LEV_MAX) + 1, 1, LEV_MAX).astype(np.uint8)


def class_to_lev(cls):
    """Class -> the middle of that class, for pixels placed by the hue rules
    rather than by a measured rate. The middle, not an edge: the hue rules
    know which band a colour is in and nothing finer, and drawing it at a band
    edge would fake a precision they do not have."""
    c = np.asarray(cls).astype(np.int16)
    return np.where(c > 0, np.clip(c * SUB - SUB // 2, 1, LEV_MAX), 0).astype(np.uint8)
# Palettes archived frames were recoloured with before this. radar_verify has
# to recognise all of them, or history stops being scoreable the moment the
# colours change.
LEGACY_RAIN_STEPS = [(200, 230, 176, 150), (137, 199, 116, 190), (87, 182, 71, 220),
                     (28, 122, 28, 235), (11, 77, 11, 255), (5, 45, 5, 255)]
LEGACY_RAIN_STEPS_2 = [(205, 234, 176, 160), (143, 208, 106, 195), (70, 177, 60, 225),
                       (242, 212, 58, 240), (243, 148, 31, 250), (226, 86, 15, 255)]
GREEN_STEPS = RAIN_STEPS          # name kept for callers


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
            params = {
                "service": "WMS", "version": "1.1.1", "request": "GetMap",
                "layers": layer, "styles": "",
                "bbox": ",".join(f"{v:.9f}" for v in bb),
                "width": 256, "height": 256, "srs": "EPSG:900913",
                "format": "image/png", "transparent": "true", "tiled": "true",
                "TIME": run_midnight.strftime("%Y-%m-%dT%H:%M:%S") + "Z",
                "DIM_FORECAST": lead_h,
            }
            # meteo.pl's GWC renders uncached tiles on demand, so a busy tile can
            # come back non-200 (or an error page) and then succeed moments later
            # — "loads eventually". Retry a few times with a short backoff rather
            # than dropping the tile, which would punch a hole in the frame.
            r = None
            for attempt in range(3):
                try:
                    resp = s.get(GWC, params=params, timeout=60)
                except Exception:
                    resp = None
                if resp is not None and resp.status_code == 200 \
                        and resp.headers.get("content-type", "").startswith("image"):
                    r = resp
                    break
                time.sleep(1.0 * (attempt + 1))     # 1s, 2s: let GWC finish rendering
            time.sleep(TILE_SLEEP)
            if r is None:
                continue
            out.paste(Image.open(io.BytesIO(r.content)).convert("RGBA"),
                      ((xi - x0) * 256, (yi - y0) * 256))
            got += 1
    if got == 0:
        return None
    return out


# meteo.pl serves EVERY hour, not just the first day. We had been asking for
# 1..24 hourly and then 3-hourly, which was our assumption, never theirs:
# probed on the 30.07 12Z run, leads 25, 26, 27, 28, 30 and 31 all return
# distinct non-empty frames. That self-imposed coarseness is what left the
# afternoon with two empty hours after every frame.
#
# Hourly to +96 h, then 3-hourly to the +120 h horizon. meteo.pl publishes
# every hour across the whole range; +96 h covers the four-day planning window
# hour-by-hour (past the old +48 h cutoff the afternoons showed 3-hourly gaps
# the viewer had to carry across), and it fits comfortably now that the scrape
# has its own workflow (um.yml) instead of the shared 45-min models job. Each
# lead is nine tile requests against someone else's cache, so the far tail stays
# 3-hourly.
UM_LEADS = list(range(1, 97)) + list(range(99, 121, 3))
# What earlier runs were fetched at. The purge below has to treat frames from
# those runs as legitimate too, or changing this list would delete them.
UM_LEADS_LEGACY = (list(range(1, 49)) + list(range(51, 121, 3))
                   + list(range(1, 25)) + list(range(27, 73, 3))
                   + list(range(78, 121, 6)) + list(range(1, 98)))
# Runs scraped before this one were labelled with the old 1-based reading, so
# every frame in them is an hour early. There is no way to tell a mislabelled
# frame from a correct one by its name, so the purge drops those runs whole and
# lets fresh scrapes rebuild. UM is an advisory layer; a day of its history is
# worth less than knowing the hour on it is right.
UM_OFFSET_FIXED_RUN = "20260730T18"


def um_purge_stale(name: str = "um4") -> int:
    """Delete frames left behind by the pre-UM_LEAD_OFFSET labelling.

    Before we knew DIM_FORECAST was 1-based, every frame was written one hour
    late. Correcting it did not remove the old keys, so the bucket now holds
    the same picture twice — 20260730T02.png and 20260730T03.png are
    byte-identical — and the archive index, which is built by listing the
    bucket, offers both. That is harmless while the viewer only carries frames
    forward from the current run's manifest, but the moment it prefers an
    exact hour from the index it would start showing the copy that is an hour
    wrong. So clear them out.

    Deterministic, not a guess: for a given run the correct valid hours are
    run + (lead - 1) over the standard lead list. Anything else in that run's
    directory is a leftover.
    """
    s3 = r2_client()
    stale, token = [], None
    while True:
        kw = {"Bucket": config.R2_BUCKET, "Prefix": f"maps/{name}/"}
        if token:
            kw["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kw)
        for obj in resp.get("Contents", []):
            m = re.match(rf"maps/{name}/(\d{{8}}T\d{{2}})/(\d{{8}}T\d{{2}})\.png$", obj["Key"])
            if not m:
                continue
            run_tag = m.group(1)
            if run_tag < UM_OFFSET_FIXED_RUN:
                stale.append(obj["Key"])       # written an hour early
                continue
            run = dt.datetime.strptime(run_tag, "%Y%m%dT%H").replace(
                tzinfo=dt.timezone.utc)
            good = {(run + dt.timedelta(hours=h - UM_LEAD_OFFSET)).strftime("%Y%m%dT%H")
                    for h in (*UM_LEADS, *UM_LEADS_LEGACY)}
            if m.group(2) not in good:
                stale.append(obj["Key"])
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")

    for i in range(0, len(stale), 1000):
        batch = stale[i:i + 1000]
        s3.delete_objects(Bucket=config.R2_BUCKET,
                          Delete={"Objects": [{"Key": k} for k in batch]})
    log.info("%s purge: %d mislabelled frames deleted", name, len(stale))
    return len(stale)


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
    hours = hours or list(UM_LEADS)
    now = dt.datetime.now(dt.timezone.utc)
    s3 = r2_client()
    for name, layer in [("um4", "um:UM4_CLOUD")]:
        # Which run to ask for. This used to be hard-coded to today's 00Z,
        # which was wrong twice over:
        #
        #   * At the 04:20 pass today's 00Z is not out yet (ICM publishes it
        #     mid-morning), so every frame came back transparent, the loop
        #     broke on the first one and nothing was stored. The morning pass
        #     was doing nothing at all.
        #   * The 12Z cycle was never fetched. UM output is hourly only for
        #     the run's first 24 h and 3-hourly after that, so using 00Z alone
        #     left every afternoon and evening on 3-hourly frames even when a
        #     12Z run covering them hourly existed.
        #
        # So walk back through the 12-hourly cycles until one answers with
        # actual pixels.
        #
        # Probe leads that are reliably populated. This has now bitten twice,
        # and the second time was self-inflicted: the set was (6, 3, 12), and
        # a lead sweep on 03.09.2026 found those three blank in EVERY
        # published run while their neighbours were full —
        #
        #   03.09 00Z  +0:0% +1:92% +2:93% +3:0% +4:97% +6:0% +9:94%
        #              +12:0% +18:92% +24:96% +36:98% +48:99%
        #
        # so the scraper judged four healthy runs "not published" and the UM
        # column sat on a three-day-old one. (The first time, mid-Aug 2026,
        # ICM began leaving the analysis and +1h transparent, which is why
        # the set had moved off +1h in the first place.)
        #
        # The lesson is to ask MORE leads, not to chase whichever one works
        # this month: a run counts as published if any of them has data, so a
        # wider set is strictly safer. These are spread across the forecast
        # so no single upstream gap can take all of them out.
        run = None
        cand = now.replace(minute=0, second=0, microsecond=0,
                           hour=0 if now.hour < 12 else 12)
        for _ in range(4):
            published = False
            for pl in (4, 9, 18, 2, 24, 36):
                p = fetch_um_frame(layer, cand, pl)
                if p is not None and np.array(p)[..., 3].any():
                    published = True
                    break
            if published:
                run = cand
                break
            log.info("%s: run %s not published yet", name,
                     cand.strftime("%Y%m%dT%H"))
            cand -= dt.timedelta(hours=12)
        if run is None:
            log.warning("%s: no published run found in the last 4 cycles", name)
            # Loud on purpose: the step is continue-on-error so the job stays
            # green, but a stalled scrape (meteo.pl changed something, or is
            # refusing us) must not pass unseen — surface it as a GitHub Actions
            # warning annotation on the run summary.
            print(f"::warning title=UM scrape stalled::{name}: no published "
                  "meteo.pl run in the last 4 cycles (48 h) — the page is "
                  "serving the last run that returned frames", flush=True)
            continue
        log.info("%s: run %s", name, run.strftime("%Y%m%dT%H"))

        run_tag = run.strftime("%Y%m%dT%H")
        done = []
        holes = []             # leads that answered blank mid-forecast
        from . import proj3059 as P
        sw, ne = _tilegrid_bounds_latlon()

        def store(h, img):
            """Reproject one lead onto the common grid and put it in R2."""
            # EPSG:3059 (transparent overlay; the client stacks it on
            # maps/bg_3059.png), with borders baked ON TOP — the UM cloud
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

        def publish():
            """Point the viewer at what we have stored so far.

            Written repeatedly, not once at the end. A full scrape is ~100
            leads at nine slow tiles each, all against someone else's cache,
            so its runtime is not ours to control; if a slow day upstream
            ever pushes it past the job timeout, the frames would be sitting
            in the bucket with nothing indexing them and the whole scrape
            would be unreachable. Cheap insurance, and the same shape of bug
            as the radar quarters pass.
            """
            if not done:
                return
            # Persistent hour -> run index, same idea as maps/meps/archive.json.
            # Frames are never deleted from the bucket, but without this only
            # the newest run's hours were reachable, so yesterday's UM simply
            # vanished from the viewer. An hour is only overwritten by a newer
            # run.
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
            s3.put_object(Bucket=config.R2_BUCKET,
                          Key=f"maps/{name}/archive.json",
                          Body=json.dumps(arch).encode(),
                          ContentType="application/json",
                          CacheControl="public, max-age=300")
            s3.put_object(Bucket=config.R2_BUCKET,
                          Key=f"maps/{name}/latest.json",
                          Body=json.dumps({
                              "run": run_tag, "path": f"maps/{name}/{run_tag}",
                              "hours": done, "thresholds": None,
                              "proj": "epsg3059", "overlay": True,
                              "credit": "ICM UW meteo.pl UM (advisory, scraped tiles)",
                              "generated_at": now.isoformat()}).encode(),
                          ContentType="application/json",
                          CacheControl="public, max-age=300")

        seen = False           # stored any real frame in this run yet?
        blanks = 0             # consecutive empty leads since the last frame
        for h in hours:
            img = fetch_um_frame(layer, run, h)
            if img is None:
                log.warning("%s: no tiles for +%dh", name, h)
                continue
            # A blank frame means two different things depending on where it
            # is. ICM leaves the first forecast hours empty, so a blank BEFORE
            # any data is a leading gap to skip. Past the model horizon the
            # tile server also answers transparent, so a blank AFTER data
            # means the run has ended.
            #
            # But a blank after data is NOT proof of the horizon. A published
            # run keeps filling in behind us: leads that read 0% at the top of
            # the scrape were 92-100% covered twenty minutes later, same run,
            # same tiles. So a lone blank is a lead we arrived at too early —
            # step over it, note it, and come back at the end. Only a RUN of
            # empties is the horizon.
            probe = np.array(img)
            if probe.shape[-1] == 4 and not probe[..., 3].any():
                if not seen:
                    log.info("%s: +%dh is empty — leading blank, skipping",
                             name, h)
                    continue
                blanks += 1
                if blanks >= UM_HORIZON_BLANKS:
                    log.info("%s: +%dh empty, %d in a row — horizon reached",
                             name, h, blanks)
                    # The earlier empties in this run were the horizon too,
                    # not holes — don't spend a retry on them.
                    del holes[len(holes) - (blanks - 1):]
                    break
                log.info("%s: +%dh is empty — hole, will retry at the end",
                         name, h)
                holes.append(h)
                continue
            seen = True
            blanks = 0
            store(h, img)
            if len(done) % UM_FLUSH_EVERY == 0:
                publish()

        # Second pass over the holes. The scrape takes 8-15 min, which is
        # about the lag measured between a lead reading blank and the same
        # lead reading full, so by now the ones we were early for have
        # landed. Whatever is still blank is genuinely absent upstream: this
        # run only gets the two passes, and the next scheduled scrape is
        # against a different run, so it will not fill these in.
        if holes:
            log.info("%s: retrying %d hole(s): %s", name, len(holes),
                     ", ".join(f"+{h}h" for h in holes))
            filled = 0
            for h in holes:
                img = fetch_um_frame(layer, run, h)
                if img is None:
                    continue
                probe = np.array(img)
                if probe.shape[-1] == 4 and not probe[..., 3].any():
                    continue
                store(h, img)
                filled += 1
            log.info("%s: %d of %d hole(s) filled on the second pass",
                     name, filled, len(holes))

        publish()
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


# Colour -> mm/h, measured against the national rain gauges.
#
# This is the thing that makes three radars into one map. Each feed paints its
# own ramp and nothing in the imagery says what any step is worth, so the
# classifier used to guess from hue: blues weak, greens middling, yellow up,
# red top. That guess had two failures, both measured on a week of archive:
#
#   * WITHIN a feed it threw the gradation away. Every greenish pixel became
#     one class, so 68% of Lithuania's echo — six distinct steps of its ramp —
#     came out as one slab of #00e300. Latvia was worse than flat: its six
#     blues landed in classes 1 and 2 in an order set by where the hue
#     thresholds happened to fall, not by the ramp.
#   * ACROSS feeds it made the same rain a different colour either side of a
#     border. Latvian light rain read class 1, Lithuanian light rain class 3.
#
# The fix is to stop reading hue and read the measurement. Each feed's palette
# is small and exact (LV 15 steps, EE 10, LT 12), and its ORDER is not a guess
# either: ranking the steps by which colours border which, over 141 archived
# frames, produces one unambiguous chain per feed, and the gauge means rise
# monotonically along it. The rate on each step is the mean hourly total
# recorded by gauges under that colour, shrunk towards the feed's own fitted
# ramp where the sample is thin and forced monotone along the chain.
#
# Bundled in wxfusion/assets so it works with no network and no credentials;
# the radar-calibrate job writes a refreshed table to R2 and that one wins.
# A colour we cannot place still falls through to the hue rules.
FEED_SCALE = {"LV": "LV", "EE": "EE", "LT": "LT2", "LT2": "LT2"}
CAL_KEY = "maps/radar_cal/scales.json"
RATES_ASSET = pathlib.Path(__file__).with_name("assets") / "radar_rates.json"
_CAL_CACHE: dict = {}


def _load_rates(path_or_body) -> dict:
    d = json.loads(path_or_body)
    out = {}
    for feed, rows in (d.get("scales") or {}).items():
        if rows:
            out[feed] = (np.array([r["rgb"] for r in rows], dtype=np.float32),
                         np.array([r["mmh"] for r in rows], dtype=np.float32))
    return out


def fitted_scales(force: bool = False) -> dict:
    """{feed: (palette Nx3, rate N)}. The bundled table first so there is
    always one, then R2 on top of it if the calibrate job has run."""
    if _CAL_CACHE and not force:
        return _CAL_CACHE
    _CAL_CACHE.clear()
    try:
        _CAL_CACHE.update(_load_rates(RATES_ASSET.read_text()))
        log.info("bundled radar rates: %s",
                 {k: len(v[1]) for k, v in _CAL_CACHE.items()})
    except Exception:
        log.warning("bundled radar rate table unreadable", exc_info=True)
    try:
        body = r2_client().get_object(Bucket=config.R2_BUCKET,
                                      Key=CAL_KEY)["Body"].read()
        fresh = _load_rates(body)
        if fresh:
            _CAL_CACHE.update(fresh)
            log.info("R2 radar rates override: %s",
                     {k: len(v[1]) for k, v in fresh.items()})
    except Exception:
        log.info("no refreshed calibration table in R2; using the bundled one")
    return _CAL_CACHE


# How close a pixel has to be to a table colour to inherit its rate. LV and EE
# are PNG and land on their palette exactly; the Lithuanian composite is JPEG
# and spreads twelve colours over seventeen thousand, so the radius has to be
# wide enough to pull the ringing back onto the step it came from.
#
# It is deliberately larger than the tightest gaps on the ramps (32 between
# Latvia's two maroons, 38 between Lithuania's two yellows) because assignment
# is nearest-colour, not a ball test: a pixel landing between two steps goes to
# the nearer one, and those two steps are always ADJACENT on the ramp, so the
# worst case is one step of error. What the radius actually decides is whether
# a colour belongs to this feed's scale at all — everything beyond it is
# something we have never measured, and falls through to the hue rules.
CAL_MAX_DIST = 40.0


def _classify_calibrated(arr: np.ndarray, feed: str):
    """Levels from the measured table, plus a mask of what it could not place."""
    tab = fitted_scales().get(FEED_SCALE.get(feed or "", ""))
    if tab is None:
        return None, None
    pal, rate = tab
    flat = arr[..., :3].reshape(-1, 3).astype(np.float32)
    best_i = np.empty(flat.shape[0], np.int32)
    best_d = np.empty(flat.shape[0], np.float32)
    for a0 in range(0, flat.shape[0], 200_000):
        chunk = flat[a0:a0 + 200_000]
        d = np.linalg.norm(chunk[:, None, :] - pal[None, :, :], axis=2)
        best_i[a0:a0 + 200_000] = d.argmin(1)
        best_d[a0:a0 + 200_000] = d.min(1)
    mm = rate[best_i].reshape(arr.shape[:2])
    unknown = (best_d > CAL_MAX_DIST).reshape(arr.shape[:2])
    lev = rate_to_lev(mm)
    lev[mm < RAIN_LEVELS[0]] = 0
    return lev, unknown


def _classify_intensity(arr: np.ndarray, feed: str | None = None) -> np.ndarray:
    """Source palette RGBA -> intensity level 0..48 (0 = no echo).

    A level is a class times SUB, give or take where inside the class the rate
    sits; lev_class() recovers the class wherever one is what is wanted.

    LVGMC/EE/LT palettes all run blue -> green -> yellow -> orange -> red with
    increasing intensity, so most of this is done by hue and value rather than
    by exact colours. The tops of the three scales are where they diverge, and
    that is where this used to fail: on the 20:01 frame of 31 July the pixels
    it could not place were white (254,254,254) and dark maroon (128,0,0) from
    Latvia, magenta (255,69,255) from Estonia, and amber (254,188,0) from
    Lithuania — every one of them the most intense end of its own scale. They
    were dropped, so the heaviest cores came out as holes in the middle of the
    rain. Exactly backwards for deciding whether to fly.
    """
    r = arr[..., 0].astype(int)
    g = arr[..., 1].astype(int)
    b = arr[..., 2].astype(int)
    a = arr[..., 3]
    opaque = a > 60
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    # Near-neutral pixels are basemap tints or a feed's no-data grey, EXCEPT
    # near-white, which is the top of the Latvian scale.
    white = opaque & (mn >= 240)
    echo = opaque & (((mx - mn) > 25) | white)
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
    # Yellow through amber. The old rule also demanded g >= r - 60, which threw
    # away Lithuania's amber (254,188,0) — 194 > 188 — for no reason.
    yellow = echo & (r > 150) & (g > 120) & (b < 120)
    cls[yellow] = 4
    pale = echo & (r > 200) & (g > 180) & (b >= 120) & (b <= 220)
    cls[pale] = 4
    orange = echo & (r > 180) & (g > 60) & (g <= 160) & (b < 100)
    cls[orange] = 5
    red = echo & (r > 150) & (g <= 60) & (b < 100)
    cls[red] = 6
    dark_red = echo & (r >= 90) & (g < 60) & (b < 60)      # maroon top of scale
    cls[dark_red] = 6
    magenta = echo & (r > 150) & (b > 150) & (g < 130)     # EE top of scale
    cls[magenta] = 6
    # White is Latvia's, and it is NOT the top of its scale. On meteolapa's own
    # legend it sits in the gap between the blue family and the yellow one, at
    # about 3 mm/h — and Latvia's colours matched that legend row at a
    # nearest-colour distance of zero, with the gauge work putting its ordering
    # at Spearman +0.77, so the row is sound even though the Lithuanian one is
    # not. Read as class 6 it was a fivefold overstatement, and it was the
    # whole story: over three live frames, 100% of Latvia's class-6 pixels were
    # white, while its real maroon top step appeared twice in total.
    cls[white] = 4 if (feed or "").upper().startswith("LV") else 6
    # Hue-placed pixels know their class and nothing finer, so they sit at the
    # middle of it. Measured rates win wherever a colour is on the feed's own
    # scale, and carry the sub-class detail with them. `echo` has to be part of
    # the condition — a feed's no-data grey is opaque and would otherwise be
    # snapped to whichever palette step it happens to sit nearest.
    lev = class_to_lev(cls)
    by_cal, unknown = _classify_calibrated(arr, feed)
    if by_cal is not None:
        lev = np.where(unknown | ~echo, lev, by_cal).astype(np.uint8)
        lev[~opaque] = 0
    return _fill_enclosed(lev, opaque)


def _fill_enclosed(cls: np.ndarray, opaque: np.ndarray, rounds: int = 3) -> np.ndarray:
    """Give enclosed unplaced pixels the strongest class touching them.

    Whatever a feed paints inside a rain area — a colour we do not know, or its
    own no-data grey where the beam was blocked — a hole in the middle of an
    echo is the one reading we can be sure is wrong. Filling from the
    neighbours errs towards more rain, which is the right way to be wrong here.
    """
    try:
        from scipy import ndimage
    except ImportError:
        return cls
    out = cls
    for _ in range(rounds):
        gap = opaque & (out == 0)
        if not gap.any():
            break
        nb = ndimage.maximum_filter(out, size=3)
        # only fill where the pixel is genuinely enclosed, not at an edge
        enclosed = gap & (ndimage.minimum_filter((out > 0).astype(np.uint8), size=3) == 0) \
                       & (ndimage.uniform_filter((out > 0).astype(float), size=5) > 0.55)
        if not enclosed.any():
            break
        out = np.where(enclosed, nb, out)
    return out


# Baltic weather radar sites. Interference beams are always radial from an
# antenna, which is a far stronger discriminator than "looks like a line" —
# real squall lines have no reason to point at a radar. Surgavere was
# confirmed empirically: extending every thin streak found across 40 archived
# frames and clustering the pairwise intersections put the densest cluster
# 5 km from it, and it accounts for nearly all the beams we see. The rest are
# the published site positions; an inaccurate one can only cause a missed
# beam, never removal of real echo, because a candidate must already be thin,
# straight and outside thick echo before the radial test is even applied.
# How far each dish's product actually reaches, for the range rings the debug
# page draws. SMHI's volumes stop at 480 bins x 500 m; FMI's per-radar cappi
# is a 500 km box, so its circle is the inscribed 250 km. The Baltic
# products publish no range at all — 250 km is their nominal reach and the
# Estonian composite is painted well past it (echo out to 370 km), so those
# rings say "about here", not "the data stops here".
SITE_RANGE_KM_DEFAULT = 250.0
SITE_RANGE_KM = {"SE Ase": 240.0, "SE Balsta": 240.0, "SE Vilebo": 240.0,
                 "SE Karlskrona": 240.0, "SE Hudiksvall": 240.0}


def site_range_km(name: str) -> float:
    return SITE_RANGE_KM.get(name, SITE_RANGE_KM_DEFAULT)


RADAR_SITES = [
    ("EE Surgavere", 58.482, 25.519),
    ("EE Harku", 59.398, 24.603),
    ("LV Riga", 56.918, 24.062),
    ("LT Laukuva", 55.613, 22.228),
    ("LT Vilnius", 54.652, 25.284),
    # Neighbour feeds (radar_nordic): the Swedish and Finnish antennas whose
    # range reaches the map. They join the coverage backstop and the per-feed
    # weak-echo fade; the nordic grids skip the beam passes (their composites
    # arrive quality-controlled), but the sites still matter for attribution.
    #
    # SMHI's comp is a TWELVE-radar national composite, not two radars. Fitting
    # circles to its coverage footprint (jobs/probe_disc_centres.py, 23.08.2026)
    # recovers the network as a union of ~240 km discs — Kiruna, Lulea,
    # Ornskoldsvik, Ostersund, Hudiksvall, Arlanda, Vara, Vilebo, Angelholm,
    # Karlskrona, Ase. Listed here are the ones whose range actually touches
    # our canvas; without them everything SMHI draws over the northern Baltic
    # was attributed to Ase, hundreds of km from the antenna that saw it.
    # Positions now come from the radars themselves — SMHI's per-site volumes
    # carry /where and name the dish in /what/source, which is also how "Ase"
    # and "Hemse" turned out to be one antenna and how Balsta replaced the
    # Arlanda guess (22 km out) that the arc fit had suggested.
    ("SE Ase", 57.3035, 18.4001),        # PLC:Hemse(Ase), Gotland
    ("SE Karlskrona", 56.2955, 15.6102),
    ("SE Balsta", 59.6110, 17.5833),
    # Hudiksvall reaches 345 px of our raster — a corner sliver — and
    # Kesalahti 1632; both are listed for range only, never fetched.
    ("SE Hudiksvall", 61.5771, 16.7144),
    ("SE Vilebo", 58.1059, 15.9365),     # PLC:Atvidaberg(Vilebo)
    # Read off FMI's per-radar grids (their centre pixel IS the antenna), not
    # from a published list — which is how "FI Vantaa" turned out not to be a
    # radar at all: the Helsinki-area antenna is Vihti, 35 km north-west of
    # where we had it.
    ("FI Korppoo", 60.129, 21.639),
    ("FI Vihti", 60.556, 24.494),
    ("FI Anjalankoski", 60.904, 27.108),
    ("FI Kankaanpaa", 61.811, 22.500),
    ("FI Kesalahti", 61.907, 29.799),
]
# Feeds that ARE a single radar. The national products mix several antennas,
# so "which sites belong to this feed" has to be matched by country prefix —
# but FMI publishes one product per radar, and for those the feed IS the
# antenna. Everything that reasons about range (the weak-echo fade, the beam
# passes, the range-graded sensitivity) asks _feed_site_names, so a site feed
# measures distance from its own dish and nothing else.
SITE_FEEDS = {
    "SEHEM": "SE Ase",
    "SEBAL": "SE Balsta",
    "SEATV": "SE Vilebo",
    "FIKOR": "FI Korppoo",
    "FIVIH": "FI Vihti",
    "FIANJ": "FI Anjalankoski",
    "FIKAN": "FI Kankaanpaa",
}


def _feed_site_names(feed: str | None) -> tuple[str, ...]:
    """Which RADAR_SITES entries belong to this feed."""
    if feed in SITE_FEEDS:
        return (SITE_FEEDS[feed],)
    return tuple(n for n, _, _ in RADAR_SITES if n.startswith((feed or "")[:2]))


# Draw order IS weight. The canvas is alpha-composited in this sequence, so a
# feed listed later covers every feed before it wherever both have echo — the
# last one to paint a pixel owns it.
#
# Estonia is deliberately at the bottom, measured 22.08.2026 over 16 slots:
# SE, FI and LT2 agree with each other within 3-6% on hundreds of thousands
# of co-wet pixels, and Estonia reads 1.18x against SE, 1.73x against FI and
# 2.08x against LT2 — a bias that is not even constant, because its intensity
# arrives as ~6 palette steps pinned to class midpoints. So the Estonian
# mosaic now shows only where nothing better covers the pixel.
DRAW_ORDER = ("EE", "SE", "FI", "LT2", "LT", "LV")

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


# A beam is a wedge from an antenna: long in range, a fixed few degrees wide,
# and therefore many times longer than it is broad. These four numbers are the
# whole test. 300 km used to bound the search, on the assumption that a radar's
# own range bounds where its artefacts appear. It does not: the composite spans
# the whole Baltic and the Latgale streak of 29 July ran from 180 km out to
# 380 km from Riga.
N_AZ = 720              # 0.5 deg azimuth bins
R_MAX_KM = 600          # a site further than this cannot be the source
BEAM_MIN_PX = 150       # too little echo to judge anything by
BEAM_GAP_KM = 15        # dashes this far apart along a ray count as one beam
BEAM_MIN_LEN_KM = 80    # radial extent before a blob can be a beam
BEAM_MAX_DEG = 16.0     # azimuthal width of a beam, in degrees
BEAM_ELONG = 4.5        # ... and how many times longer than wide it must be
# A second, stricter shape that needs no origin test. Something 6 deg wide and
# eight times longer than wide is a beam wherever along the ray it happens to
# start — beams are often blanked over the first hundred kilometres, and the
# 05:00 UTC fan of 29 July had one at 3 deg, elongation 9, that the origin
# test alone threw out because it began 164 km from Harku.
BEAM_NARROW_DEG = 6.0
BEAM_NARROW_ELONG = 8.0
# And it has to start at the antenna. This is what separates a beam from a rain
# band that happens to lie radially: measured over a week of frames, every
# beam-shaped blob began within 150 km of its site, while the elongated blobs
# that were real weather all sat 270 km or more out — radial by coincidence,
# seen from a distant radar, not emanating from one.
BEAM_START_KM = 150
# A third shape, for the isolated beam in a near-empty frame. When a radar's
# own picture is almost all beam and little rain, the ray arrives dashed and
# wobbling across a bin or two, so binary_closing leaves it as a string of
# short, pencil-thin, wildly elongated fragments — none reaching the 80 km a
# wedge or needle needs. The reach/hot detectors downstream can't help either:
# they flag a bearing by how far it overshoots its neighbours, and an isolated
# beam has none. But nothing in real weather is 2 deg wide and fifteen-times
# elongated: a fragment that thin and that stretched, starting near the
# antenna, is a beam whatever its length. Measured on the 11.08 17:01 LV frame,
# whose only Riga echo was the beam, the trunk fragments ran elong 22-55 at
# 0.5-2.5 deg; the tightest real cell in the 27-frame sample was under 5.
BEAM_HAIR_DEG = 3.0
BEAM_HAIR_ELONG = 15.0
BEAM_HAIR_LEN = 20
# The dashed ray that near-misses everything. On the 21.08 20:56 LV frame the
# Riga beam arrived as five fragments on one bearing corridor — spans 10-25 km,
# elongations 12-26 — each ONE number short of the hair gate (18<20 km, or
# 13.3<15x), with dash gaps beyond BEAM_GAP_KM so the closing never joined
# them; and the ridge test upstream was blind to it because the surrounding sky
# was empty (flank reach 0, under REACH_MIN_FLANK_KM, the isolated-shower
# guard). So a second detection pass re-closes with this much wider bridge —
# along the ray and across the one-bin wobble — and judges the merged shape by
# the NARROW gates only. The dashes above merge to rays 30-55 km long at
# 1-3 deg: unmistakable. Real weather cannot ride in on the wide bridge because
# nothing meteorological is 3 deg wide over that length — the wedge gate, which
# scattered convection could reach when bridged this far, deliberately does not
# apply on this pass.
#
# The dash pass carries its own elongation bar, below hair's 15: the (3, gap)
# closing that joins the wobbling dashes also inflates the merged component's
# azimuthal bounding box by up to a bin each side, which halves the measured
# elongation — on the 21.08 frame the joined ray read aspan 2-3 deg, elong
# 8-11, and hair's 15 missed every one. At <=3 deg over 30+ km the shape is
# still nothing weather makes (the tightest real cell in the 27-frame sample
# was under 5), so 8 with the width cap is as safe as 15 without the closing.
BEAM_DASH_GAP_KM = 45
BEAM_DASH_ELONG = 8.0
BEAM_DASH_LEN = 30
# Smallest removal worth writing. Was BEAM_MIN_PX // 2 (75) inline. An isolated
# beam in a low-res source frame is tiny — the 11.08 Riga ray was 28 px all
# told — so the floor has to sit below that. The shape gates (a 20 km ray, 3
# deg wide, 15x+ elongated, from the antenna) are what vouch it is a beam; this
# is only a speckle guard, and 15 px of that shape is not speckle.
BEAM_MIN_REMOVE_PX = 15

# Isolation gate. A real interference beam has clear sky on at least one side.
# A ray-shaped run of echo with rain on BOTH azimuthal flanks along its whole
# range is either rain itself or a beam buried in rain — and zeroing it just
# punches a white radial gash through the storm (the "star" of white lines that
# appears near an antenna sitting under heavy rain). So a component is only
# removed when a flank is substantially clear; an over-reach that pokes out past
# the rain still has an empty flank and is still caught.
BEAM_FLANK_ISO_BINS = 6      # azimuth bins tested on each side (±3°); wide enough
#                              to see past a beam's own narrow clearance margin
BEAM_FLANK_ISO_FRAC = 0.4    # both flanks this occupied over a range = embedded there


def _embedded_range_mask(closed, a0, a1) -> np.ndarray:
    """Per-range boolean over [0..R_MAX]: True where a component at azimuth
    [a0,a1) has echo on BOTH flanks AT THAT RANGE — i.e. buried in rain there.

    Per-range, not whole-component, because an over-reaching beam is in the rain
    near the antenna and in clear air further out: averaging its flanks over the
    whole length lands in the middle and the whole ray gets zeroed, gouging the
    rain it started in. Sparing only the ranges that are actually embedded
    removes the part poking out of the storm and leaves the part inside it."""
    la = [(a0 - 1 - k) % N_AZ for k in range(BEAM_FLANK_ISO_BINS)]
    ra = [(a1 + k) % N_AZ for k in range(BEAM_FLANK_ISO_BINS)]
    lf = closed[la].mean(0)          # fraction of left flank bins lit, per range
    rf = closed[ra].mean(0)
    return (lf >= BEAM_FLANK_ISO_FRAC) & (rf >= BEAM_FLANK_ISO_FRAC)


def _sweep_dash_neighbours(occ, kill, ndimage):
    """Once a beam is confirmed, collect the thin fragments right beside it.

    A dashed ray never dies whole: on the 21.08 20:56 LV frame the dash pass
    cleared three corridors and left fragments one number short of every gate —
    spans 28 and 29 km against the 30 the dash bar asks, a 15 km hair at
    elongation 20. Chasing them by lowering the bars would eventually reach
    real weather; instead, use what is already known. These fragments sit
    within a couple of bins of a corridor that WAS condemned, and a thin
    ray-ish component that close to a confirmed beam is the same beam. The
    shape bar here is deliberately low (elong >= 4) because the neighbourhood
    test is doing the vouching."""
    grown = ndimage.binary_dilation(kill, structure=np.ones((5, 41), bool))
    lab, n = ndimage.label(occ, structure=np.ones((3, 3)))
    extra = np.zeros_like(kill)
    for i, sl in enumerate(ndimage.find_objects(lab), start=1):
        a0, a1 = sl[0].start, sl[0].stop
        r0, r1 = sl[1].start, sl[1].stop
        a_span = (a1 - a0) * 360.0 / N_AZ
        width_km = (r0 + r1) / 2.0 * np.radians(a_span)
        elong = (r1 - r0) / width_km if width_km > 0 else 0.0
        if a_span > BEAM_HAIR_DEG or elong < 4.0 or r0 > BEAM_START_KM:
            continue
        comp = (lab == i)
        if (comp & kill).any() or not (comp & grown).any():
            continue
        extra |= comp
    return extra


def _remove_beams_polar(cls: np.ndarray) -> np.ndarray:
    """Remove interference cones by the shape that defines them.

    An interference ray is a wedge: it starts at an antenna, runs outward a
    long way, and stays a fixed few degrees wide the whole time — so it grows
    broader in kilometres the further out it goes, which is exactly what the
    eye reads as a cone radiating from the radar. Nothing meteorological has
    that shape.

    The work is done in polar coordinates around each known antenna, where a
    beam is simply a long thin horizontal bar. Four numbers decide: where the
    echo starts in range, how far it runs, how many degrees of azimuth it
    covers, and how many times longer than wide that makes it.

    Two earlier attempts failed on the Latgale streak of 29 July, both for the
    same reason — they measured statistics instead of shape:

      * the thinness test never saw it, because the wedge renders 3+ px wide
        and survived the 3x3 opening;
      * the per-azimuth cover test compared each ray against a 13-bin median
        window, and the streak was 10 bins wide, so it set its own baseline:
        144 km of range lit against a "background" of 108. Widening that
        window started eating real rain instead, because a long ray through a
        big rain area also stands above its neighbours. Length and prominence
        do not separate the two. Shape does.

    Working in polar space also closes gaps along the ray first, which matters
    because these beams are often dashed — as separate Cartesian blobs each
    dash is too short to judge, and every one of them slipped through.

    Brightness is deliberately not consulted. The old rule spared any ray
    carrying a bright core, and this streak had one down its axis, so it was
    protected by the very test meant to protect weather.
    """
    try:
        from scipy import ndimage
    except ImportError:
        log.warning("scipy missing - beam removal skipped")
        return cls

    echo = cls > 0
    if echo.sum() < BEAM_MIN_PX:
        return cls
    h, w = cls.shape
    yy, xx = np.mgrid[0:h, 0:w]
    out = cls.copy()
    removed = []

    for name, sx, sy in _site_pixels():
        lit = out > 0
        if lit.sum() < BEAM_MIN_PX:
            break
        rng = np.hypot(xx - sx, yy - sy)
        keep = lit & (rng <= R_MAX_KM)
        if keep.sum() < BEAM_MIN_PX:
            continue
        az = ((np.arctan2(yy - sy, xx - sx) + np.pi) / (2 * np.pi) * N_AZ).astype(int) % N_AZ
        rb = np.clip(rng.astype(int), 0, R_MAX_KM)

        occ = np.zeros((N_AZ, R_MAX_KM + 1), bool)
        occ[az[keep], rb[keep]] = True
        kill_bins = np.zeros((N_AZ, R_MAX_KM + 1), bool)
        # Two detection passes: the ordinary close (all three gates), then the
        # wide dash-bridging close (narrow gates only — see BEAM_DASH_GAP_KM).
        for gap_struct, all_gates in (
                (np.ones((1, BEAM_GAP_KM), bool), True),
                (np.ones((3, BEAM_DASH_GAP_KM), bool), False)):
            closed = ndimage.binary_closing(occ, structure=gap_struct)
            lab, n = ndimage.label(closed, structure=np.ones((3, 3)))
            if n == 0:
                continue
            for i, sl in enumerate(ndimage.find_objects(lab), start=1):
                a0, a1 = sl[0].start, sl[0].stop
                r0, r1 = sl[1].start, sl[1].stop
                r_span = r1 - r0
                a_span = (a1 - a0) * 360.0 / N_AZ
                width_km = (r0 + r1) / 2.0 * np.radians(a_span)
                elong = r_span / width_km if width_km > 0 else 0.0
                long_enough = r_span >= BEAM_MIN_LEN_KM
                wedge = (all_gates and long_enough and a_span <= BEAM_MAX_DEG
                         and elong >= BEAM_ELONG and r0 <= BEAM_START_KM)
                needle = (long_enough and a_span <= BEAM_NARROW_DEG
                          and elong >= BEAM_NARROW_ELONG)
                hair = (r_span >= BEAM_HAIR_LEN and a_span <= BEAM_HAIR_DEG
                        and elong >= BEAM_HAIR_ELONG and r0 <= BEAM_START_KM)
                dash = (not all_gates and r_span >= BEAM_DASH_LEN
                        and a_span <= BEAM_HAIR_DEG and elong >= BEAM_DASH_ELONG
                        and r0 <= BEAM_START_KM)
                if os.environ.get("RADAR_BEAM_DEBUG") and (r_span >= 40 or elong >= 3):
                    log.info("beam_diag polar %s%s: comp r%d-%d span%d aspan%.1f "
                             "elong%.1f start%d -> wedge=%s needle=%s hair=%s dash=%s",
                             name, "" if all_gates else " (dash)",
                             r0, r1, r_span, a_span, elong, r0, wedge, needle, hair, dash)
                if not (wedge or needle or hair or dash):
                    continue
                # Zero the ray only where a flank is clear; spare the ranges where
                # it is buried in rain, so an over-reach loses its cone without a
                # white gash being punched through the storm it started in.
                comp = (lab == i)
                comp &= ~_embedded_range_mask(closed, a0, a1)[None, :]
                if not comp.any():
                    if os.environ.get("RADAR_BEAM_DEBUG"):
                        log.info("beam_diag polar %s: comp r%d-%d fully embedded — "
                                 "kept, not gouged", name, r0, r1)
                    continue
                kill_bins |= comp
                removed.append(f"{name} {r0}-{r1} km {a_span:.1f} deg "
                               f"elong {elong:.1f}")

        if not kill_bins.any():
            continue
        kill_bins |= _sweep_dash_neighbours(occ, kill_bins, ndimage)
        # Only ever clear pixels that were lit; the closing was for detection.
        gone = keep & kill_bins[az, rb]
        if os.environ.get("RADAR_BEAM_DEBUG"):
            log.info("beam_diag polar %s: would clear %d px (floor %d)",
                     name, int(gone.sum()), BEAM_MIN_REMOVE_PX)
        if gone.sum() < BEAM_MIN_REMOVE_PX:
            continue
        out[gone] = 0

    if removed:
        log.info("beam removal: %s", "; ".join(removed))
    return out


# The line-based pass. A beam is a straight corridor of echo that points at an
# antenna and stays narrow relative to its length. Unlike the polar pass this
# does not assume the ray passes exactly through the antenna pixel — the fitted
# line only has to point within RADIAL_TOL_KM of one. That matters: rays are
# often a few kilometres off, either because the composite's georeferencing is
# approximate or because the published antenna coordinate is, and at 100 km a
# 12 km miss smears the ray across enough azimuth bins to hide it from the
# polar test entirely. That is what was left of the 05:00 fan on 29 July.
LINE_MIN_KM = 40        # shortest line worth considering
# Beams are dashed, and the faint ones are barely more than a dotted line: the
# Harku ray that survived the first version put 127 px on the map over 56 km.
# A 12 km gap tolerance did not join it into anything Hough would report.
LINE_GAP_KM = 18
LINE_MAX_WIDTH_KM = 18  # a corridor wider than this is weather
LINE_ELONG = 10.0       # ... and it must be this many times longer than wide
# And it must stand alone. Light stratiform rain draws long narrow filaments
# that satisfy every shape test going, and enough of them point roughly at a
# radar to matter — the first version of this pass ate 15% of a widespread
# light-rain field. The difference is context: a beam sits in empty sky, a
# filament sits inside a broader field of echo. So the flanks either side of
# the corridor have to be nearly empty.
LINE_FLANK_KM = 20      # how far out to look either side
LINE_FLANK_MAX = 0.12   # and how much echo is allowed there
LINE_KEEP_AREA = 400    # protect big blobs that are not themselves elongated
LINE_KEEP_ELONG = 3.0


def _remove_beam_lines(cls: np.ndarray) -> np.ndarray:
    try:
        from scipy import ndimage
        from skimage.transform import probabilistic_hough_line
    except ImportError:
        log.warning("scipy/skimage missing - line beam removal skipped")
        return cls

    echo = cls > 0
    if echo.sum() < BEAM_MIN_PX:
        return cls
    segs = probabilistic_hough_line(echo, threshold=6,
                                    line_length=LINE_MIN_KM, line_gap=LINE_GAP_KM,
                                    rng=0)
    if not segs:
        return cls

    # Compact, chunky blobs are weather whatever lies across them.
    lab, n = ndimage.label(echo, structure=np.ones((3, 3)))
    protect = np.zeros(n + 1, bool)
    for i, sl in enumerate(ndimage.find_objects(lab), start=1):
        m = (lab[sl] == i)
        area = int(m.sum())
        if area < LINE_KEEP_AREA:
            continue
        ys, xs = np.nonzero(m)
        pts = np.c_[xs, ys].astype(float)
        sv = np.linalg.svd(pts - pts.mean(0), full_matrices=False)[1]
        if sv[1] > 1e-6 and sv[0] / sv[1] < LINE_KEEP_ELONG:
            protect[i] = True

    ys, xs = np.nonzero(echo)
    pts = np.c_[xs, ys].astype(float)
    sites = _site_pixels()
    out = cls.copy()
    killed = {}
    for (x0, y0), (x1, y1) in segs:
        p0 = np.array([x0, y0], float)
        d = np.array([x1 - x0, y1 - y0], float)
        length = float(np.hypot(*d))
        if length < LINE_MIN_KM:
            continue
        d /= length
        nrm = np.array([-d[1], d[0]])
        src = _radial_source(x0, y0, x1, y1, sites)
        if src is None:
            continue                        # straight, but aimed at no antenna
        t = (pts - p0) @ d
        w = (pts - p0) @ nrm
        near = (t >= -10) & (t <= length + 10) & (np.abs(w) <= 40)
        if near.sum() < BEAM_MIN_PX // 2:
            continue
        # How wide the echo actually is around the line. A wedge is allowed to
        # broaden with range, so the limit is relative to its length.
        w85 = float(np.percentile(np.abs(w[near]), 85))
        if w85 > min(LINE_MAX_WIDTH_KM, length / LINE_ELONG):
            continue
        half = w85 * 1.4 + 2
        # Isolation: a beam has empty sky beside it.
        flank = near & (np.abs(w) > half + 4) & (np.abs(w) <= half + 4 + LINE_FLANK_KM)
        if flank.sum() / (2.0 * length * LINE_FLANK_KM) > LINE_FLANK_MAX:
            continue
        corridor = near & (np.abs(w) <= half)
        idx = np.nonzero(corridor)[0]
        yy, xx = ys[idx], xs[idx]
        keep = protect[lab[yy, xx]]
        yy, xx = yy[~keep], xx[~keep]
        if len(yy) < BEAM_MIN_PX // 3:
            continue
        out[yy, xx] = 0
        killed[src] = killed.get(src, 0) + len(yy)

    if killed:
        log.info("line beam removal: %s",
                 ", ".join(f"{k} {v} px" for k, v in sorted(killed.items())))
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


# De-spoking. A radar overlay carries its own no-data spokes: a single azimuth
# ray the source dropped reads as a thin white line radiating straight from the
# antenna, right through whatever rain surrounds it. It is not our beam remover
# — the gap is in the raw frame — and despeckle cannot touch it, because every
# pixel bordering the slot is lit. The white "star" of hairline gashes the eye
# picks out around a radar sitting under heavy rain is these dropped rays, one
# per direction.
#
# The cure is the one every operational radar product applies: azimuthal
# interpolation across the gap. It is gated so it can only ever close a slot
# that is BOTH narrow (a couple of tenths of a degree) AND long (a genuine
# radial spoke, not the everyday gap between two rain cells), and only where
# rain brackets it on both azimuthal sides. The fill takes the LIGHTER of the
# two flanking rays, so it can round a hole shut but never invent a core.
SPOKE_MAX_GAP_BINS = 4     # close an azimuth slot up to ~2 deg wide
SPOKE_MIN_LEN_KM = 40      # ... only if the slot runs at least this far in range
SPOKE_INNER_KM = 20        # ... skip the sub-pixel azimuth fan hugging the antenna
SPOKE_MIN_FILL_PX = 12     # ... and yields at least this much fill (speckle guard)


def _close_radial_spokes(cls: np.ndarray) -> np.ndarray:
    """Fill narrow no-data rays that are buried in rain on both sides."""
    try:
        from scipy import ndimage
    except ImportError:
        return cls
    if (cls > 0).sum() < BEAM_MIN_PX:
        return cls
    h, w = cls.shape
    yy, xx = np.mgrid[0:h, 0:w]
    out = cls.copy()
    filled = 0
    az_close = np.ones((SPOKE_MAX_GAP_BINS + 1, 1), bool)   # bridges azimuth slots
    rng_close = np.ones((1, BEAM_GAP_KM), bool)             # bridges range holes

    for name, sx, sy in _site_pixels():
        rng = np.hypot(xx - sx, yy - sy)
        within = rng <= R_MAX_KM
        lit = (out > 0) & within
        if lit.sum() < BEAM_MIN_PX:
            continue
        az = ((np.arctan2(yy - sy, xx - sx) + np.pi)
              / (2 * np.pi) * N_AZ).astype(int) % N_AZ
        rb = np.clip(rng.astype(int), 0, R_MAX_KM)

        occ = np.zeros((N_AZ, R_MAX_KM + 1), bool)
        occ[az[lit], rb[lit]] = True
        # A 0.5-deg azimuth bin does not hit every 1 km range ring on the pixel
        # grid, so a solid ray still reads as a dotted column. Bridge those holes
        # along range first (as the beam pass does for dashes), so the only
        # columns still empty are genuinely dropped rays.
        occ = ndimage.binary_closing(occ, structure=rng_close)

        # Embedded slots: empty bins that an azimuthal close bridges — i.e. with
        # a ray within a couple of bins on BOTH sides at that range.
        emb = ndimage.binary_closing(occ, structure=az_close) & ~occ
        if not emb.any():
            continue

        # A dropped ray is a single bearing empty for a long unbroken run of
        # range; the fan of sub-pixel bins near the antenna is empty only close
        # in, and every bearing through real rain is lit again further out. So
        # score each bearing by how much of it beyond the inner zone is embedded
        # gap, and keep the bearings that run at least a spoke's length.
        col = emb[:, SPOKE_INNER_KM:].sum(1)
        spoke_az = col >= SPOKE_MIN_LEN_KM
        if not spoke_az.any():
            continue
        # ... and only where that band of bearings stays narrow. A wide contiguous
        # run of dead bearings is a blanked sector, not a ray.
        albl, _ = ndimage.label(spoke_az)
        for j in range(1, albl.max() + 1):
            if int((albl == j).sum()) > SPOKE_MAX_GAP_BINS:
                spoke_az[albl == j] = False
        if not spoke_az.any():
            continue
        spoke = emb & spoke_az[:, None]

        # The empty pixels lying in a dropped ray. Fill them in Cartesian space
        # from their lit neighbours — a couple of rounds close a 1-2 px slot,
        # each pixel taking the LIGHTER neighbour so a hole is rounded shut but
        # a core is never invented. Bounded to the spoke pixels, so it can only
        # ever touch a dropped ray, never a real gap.
        cand = within & (out == 0) & spoke[az, rb]
        n0 = int(cand.sum())
        if n0 < SPOKE_MIN_FILL_PX:
            continue
        for _ in range(SPOKE_MAX_GAP_BINS + 1):
            if not cand.any():
                break
            best = np.zeros((h, w), np.int16)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if dy == 0 and dx == 0:
                        continue
                    sh = np.roll(np.roll(out, dy, 0), dx, 1)
                    take = cand & (sh > 0) & ((best == 0) | (sh < best))
                    best[take] = sh[take]
            done = cand & (best > 0)
            out[done] = best[done]
            cand &= ~done
        filled += n0 - int(cand.sum())

    if filled:
        log.info("radial de-spoke: filled %d px", filled)
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


# Smallest a heavy core is allowed to be, in pixels — and a pixel is a
# kilometre. Measured off a live composite: the orange class appeared in 115
# separate blobs of which the median was ONE pixel and 58% were three pixels
# or fewer, scattered through an otherwise uniform green field. The MEPS and
# UM panels of the same weather had their heaviest class in 3 and 18 blobs,
# median 40 and 11 px, with nothing tiny at all. Convection does not produce
# isolated square kilometres of 15 mm/h inside light rain; a source with 6,561
# colours of compression noise in it does.
#
# Demoted rather than deleted: the pixel is still raining, it is just not
# raining that hard, so it takes the strongest class its neighbours can
# vouch for.
MIN_CORE_PX = 4
CORE_CLASS = 5
CORE_LEV = (CORE_CLASS - 1) * SUB + 1     # first level inside class 5


def _despeckle_intensity(cls: np.ndarray, min_px: int = MIN_CORE_PX) -> np.ndarray:
    """Demote heavy-class blobs too small to be real cores."""
    try:
        from scipy import ndimage
    except Exception:
        return cls
    out = cls.copy()
    heavy = cls >= CORE_LEV
    if not heavy.any():
        return out
    lab, n = ndimage.label(heavy, np.ones((3, 3)))
    if n == 0:
        return out
    sizes = np.bincount(lab.ravel())
    small = np.isin(lab, np.flatnonzero(sizes < min_px)) & heavy
    if not small.any():
        return out
    # what the neighbourhood says, ignoring the speck itself
    nb = np.zeros_like(cls)
    body = np.where(heavy, 0, cls)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy == 0 and dx == 0:
                continue
            nb = np.maximum(nb, np.roll(np.roll(body, dy, 0), dx, 1))
    out[small] = np.maximum(nb[small], 1)
    return out


# The same six steps for air below freezing, matching maps_meps.SNOW_ANCHORS
# so the model column and the radar column say the same thing in the same
# colour. Top step stays orange: at that rate the rate is the point.
SNOW_STEPS = [(223, 238, 255, 170), (181, 213, 251, 200), (127, 178, 238, 225),
              (69, 135, 214, 240), (31, 90, 168, 250), (255, 120, 0, 255)]
# Opacity along the same ramp: light rain half-transparent so the coastline
# under it stays legible, heavy rain solid. One value per anchor, interpolated
# with the colour.
RAIN_ALPHA = [170, 200, 225, 240, 250, 255, 255]

# Colour and alpha for every level 1..48, built once. Forty-eight lookups is
# cheaper than interpolating 393 000 pixels, and it means the exact colours
# that end up in the archive are enumerable — which the verifier needs.
def _ramp_lut(anchors):
    # Each level is a band, so it is drawn at the middle of its own band.
    pos = (np.arange(LEV_MAX + 1, dtype=np.float64) - 0.5) / LEV_MAX
    lut = np.zeros((LEV_MAX + 1, 4), np.uint8)
    lut[:, :3] = ramp_rgb(pos, anchors)
    from .maps_meps import ANCHOR_POS
    lut[:, 3] = np.rint(np.interp(pos, ANCHOR_POS, RAIN_ALPHA)).astype(np.uint8)
    lut[0] = 0
    return lut


_LUT_RAIN = None
_LUT_SNOW = None


def ramp_lut(cold: bool = False) -> np.ndarray:
    global _LUT_RAIN, _LUT_SNOW
    if cold:
        if _LUT_SNOW is None:
            _LUT_SNOW = _ramp_lut(SNOW_ANCHORS)
        return _LUT_SNOW
    if _LUT_RAIN is None:
        _LUT_RAIN = _ramp_lut(RAIN_ANCHORS)
    return _LUT_RAIN


# Sensitivity by range: the same rain reads brighter the further out it is
# seen. At 200+ km the beam is a kilometre wide and a kilometre up, so it
# fills with hydrometeors aloft and with whatever the ground clutter and the
# melting layer add, while the drops that reach the ground are the same. The
# palette products show it as bright arcs at the rim with clear air inside.
#
# The fades elsewhere only touch WEAK echo (_weak_alpha) or the last few km of
# the rim (_rim_alpha); this grades the INTENSITY itself, over the whole disc,
# by distance from the antenna that saw the pixel. Quadratic, so the near
# field is left alone: at half range a feed keeps ~90% of its rate, at the
# rim it keeps 1 - RANGE_DAMP of it.
#
# Composites are excluded on purpose: for SE and FI a pixel's distance to the
# antennas we know about says nothing about how far away the radar that saw
# it actually is.
RANGE_DAMP = {"EE": 0.45, "LT2": 0.35, "LT": 0.35, "LV": 0.35}
RANGE_DAMP_REF_KM = 250.0


def _range_damp(lev: np.ndarray, feed: str | None) -> np.ndarray:
    """Level grid with the far half of each disc read down (see RANGE_DAMP)."""
    k = RANGE_DAMP.get(feed or "", 0.0)
    if os.environ.get("RADAR_RANGE_DAMP"):        # test renders sweep this
        k = float(os.environ["RADAR_RANGE_DAMP"]) if k else 0.0
    if not k:
        return lev
    d = _feed_range_km(feed)
    if d is None or d.shape != lev.shape:
        return lev
    f = 1.0 - k * np.clip(d / RANGE_DAMP_REF_KM, 0.0, 1.0) ** 2
    out = lev.copy()
    wet = lev > 0
    out[wet] = rate_to_lev(
        pos_rain((lev[wet].astype(np.float64) - 0.5) / LEV_MAX) * f[wet])
    return out


def _recolor(cls: np.ndarray, cold: np.ndarray | None = None,
             feed: str | None = None,
             active: frozenset | None = None) -> Image.Image:
    """Intensity levels -> RGBA, in green or, where the air is below freezing,
    in blue. Recoloured from the level array rather than from finished pixels:
    once the sources are composited the edges are blended and no longer sit on
    a ramp colour. With `active` (the frame's awake feeds, from active_feeds)
    the weak-echo fade is coverage-aware; without it, the static per-feed one."""
    lev = np.clip(cls, 0, LEV_MAX).astype(np.uint8)
    if feed:
        lev = _range_damp(lev, feed)
    out = ramp_lut(False)[lev]
    if cold is not None:
        out = np.where(cold[..., None], ramp_lut(True)[lev], out)
    if feed:
        out = out.copy()
        w = (_weak_alpha_aware(feed, active)
             if active is not None and ADAPTIVE_FADE else _weak_alpha(feed))
        weak = (lev > 0) & (lev <= WEAK_CLASSES * SUB)
        out[..., 3] = np.where(weak, out[..., 3] * w, out[..., 3]).astype(np.uint8)
        # Fade out any coverage circle this frame is showing, here rather than
        # once over the finished composite — by then the layers are merged and
        # there is no way to tell whose edge is whose.
        rim = _rim_alpha(lev, feed)
        if rim is not None:
            out[..., 3] = (out[..., 3].astype(np.float32) * rim).astype(np.uint8)
    return Image.fromarray(np.ascontiguousarray(out, dtype=np.uint8), "RGBA")


def _freezing_mask(when: dt.datetime):
    """True where MEPS puts the air below freezing, on the 3059 canvas.

    Radar says where the echo is; it cannot say what is falling. The model can,
    and it is already stored on this grid for the hour. Best effort by design:
    anything missing — no grid for the hour, no reprojection index, an hour
    stored before temperature was kept — returns None and the frame draws
    green, which is what every frame did before.
    """
    from .maps_meps import FREEZE_C
    try:
        from . import meps_grid as MG
        from . import proj3059 as P

        hour = when.astimezone(dt.timezone.utc).replace(
            minute=0, second=0, microsecond=0)
        s3 = r2_client()
        blob = s3.get_object(Bucket=config.R2_BUCKET,
                             Key=MG._key(hour))["Body"].read()
        z, _ = MG.unpack(blob)
        if "t2m" not in z:
            return None
        ix, _ = MG.unpack(s3.get_object(
            Bucket=config.R2_BUCKET,
            Key="maps/meps_grid/index.wxg")["Body"].read())
        idx = ix["idx"].ravel()
        t = (z["t2m"].astype(np.float32) / 100.0 - 273.15).ravel()
        miss = idx == 0xFFFFFFFF
        sampled = t[np.where(miss, 0, idx)]
        return np.where(miss, False, sampled < FREEZE_C).reshape(P.H, P.W)
    except Exception:
        log.info("no freezing mask for %s; drawing rain green", when)
        return None


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


# One tick fetches the same overlay more than once: the composite wants the
# three newest frames per feed for its temporal vote, the source archive wants
# the one nearest the hour, and those overlap. They run as separate processes
# in the same job, so an in-memory memo would not see across them — but they
# share a runner and its /tmp, which is thrown away with it.
#
# Measured on a routine tick: 12 image fetches become 9. Small, but it is
# somebody else's bandwidth and the fix is fifteen lines.
CACHE_DIR = pathlib.Path(os.environ.get("WX_OVERLAY_CACHE")
                         or pathlib.Path(tempfile.gettempdir()) / "wx-overlays")
CACHE_TTL_S = 3 * 3600


def overlay_bytes(url: str, s=None) -> bytes | None:
    """The overlay PNG, from the runner's cache if this tick already got it."""
    import hashlib
    f = CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".png")
    try:
        if f.is_file() and time.time() - f.stat().st_mtime < CACHE_TTL_S:
            return f.read_bytes()
    except Exception:
        pass
    r = (s or session()).get(url, allow_redirects=True, timeout=90)
    time.sleep(0.2)
    if r.status_code != 200:
        return None
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".part")
        tmp.write_bytes(r.content)
        tmp.replace(f)          # atomic, so a second process never sees a half file
        _sweep_cache()
    except Exception:
        pass                    # a cache that cannot be written is still a fetch
    return r.content


def _sweep_cache(limit: int = 400) -> None:
    """A runner throws the whole directory away, but a backfill on a laptop
    does not, and this is somebody's disk either way."""
    files = list(CACHE_DIR.glob("*.png"))
    if len(files) <= limit:
        return
    for p in sorted(files, key=lambda p: p.stat().st_mtime)[:len(files) - limit]:
        p.unlink(missing_ok=True)


def _source_polar(shape, sw, ne, lat0, lon0):
    """Range in km and azimuth in degrees from one antenna, for every pixel of
    a source overlay — in the overlay's own geometry, before anything has been
    reprojected."""
    h, w = shape[:2]

    def merc_y(la):
        r = np.radians(la)
        return np.log(np.tan(r) + 1.0 / np.cos(r))

    # the inverse of what resample_mercator_image applies
    my = merc_y(ne[0]) + ((np.arange(h) + 0.5) / h) * (merc_y(sw[0]) - merc_y(ne[0]))
    lat = np.degrees(np.arctan(np.sinh(my)))[:, None]
    lon = (sw[1] + ((np.arange(w) + 0.5) / w) * (ne[1] - sw[1]))[None, :]
    dx = (lon - lon0) * 111.32 * np.cos(np.radians((lat + lat0) / 2.0))
    dy = (lat - lat0) * 110.57
    return np.hypot(dx, dy), (np.degrees(np.arctan2(dx, dy)) + 360.0) % 360.0


def _remove_beams_source(arr: np.ndarray, feed: str | None, sw, ne) -> np.ndarray:
    """Beam removal on the radar's OWN image, before merging or classifying.

    The passes further down work on the shared 3059 grid, which is the right
    place for anything that has to compare feeds. A beam is not that. It
    belongs to one antenna, it is radial in that antenna's own geometry, and by
    the time a frame has been reprojected onto somebody else's grid, quantised
    into levels and had its enclosed holes filled, a dashed one-degree ray has
    been smeared into something the shape tests no longer recognise. Catching
    it here means the classifier never sees it.

    Same wedge test as _remove_beams_polar — starts near the antenna, runs a
    long way, holds a fixed few degrees of azimuth — but applied to the
    source's own echo mask, and only for that source's own antennas.
    """
    try:
        from scipy import ndimage
    except ImportError:
        return arr
    a = arr[..., 3].astype(int)
    rgb = arr[..., :3].astype(int)
    echo = (a > 60) & ((rgb.max(2) - rgb.min(2)) > 25)
    if echo.sum() < BEAM_MIN_PX:
        if os.environ.get("RADAR_BEAM_DEBUG"):
            log.info("beam_diag %s: source-shape echo %d px (< %d) — shape pass "
                     "skipped", feed, int(echo.sum()), BEAM_MIN_PX)
        return arr
    out, removed = arr, []
    names = _feed_site_names(feed)
    for name, la, lo in RADAR_SITES:
        if name not in names:
            continue
        rng, azdeg = _source_polar(arr.shape, sw, ne, la, lo)
        keep = echo & (rng <= R_MAX_KM)
        if keep.sum() < BEAM_MIN_PX:
            if os.environ.get("RADAR_BEAM_DEBUG"):
                log.info("beam_diag %s %s: source-shape keep %d px (< %d) — site "
                         "skipped", feed, name, int(keep.sum()), BEAM_MIN_PX)
            continue
        az = (azdeg / 360.0 * N_AZ).astype(int) % N_AZ
        rb = np.clip(rng.astype(int), 0, R_MAX_KM)
        occ = np.zeros((N_AZ, R_MAX_KM + 1), bool)
        occ[az[keep], rb[keep]] = True
        kill = np.zeros((N_AZ, R_MAX_KM + 1), bool)
        # Two passes, as in _remove_beams_polar: ordinary close with all three
        # gates, then the wide dash-bridge with the narrow gates only.
        for gap_struct, all_gates in (
                (np.ones((1, BEAM_GAP_KM), bool), True),
                (np.ones((3, BEAM_DASH_GAP_KM), bool), False)):
            closed = ndimage.binary_closing(occ, structure=gap_struct)
            lab, n = ndimage.label(closed, structure=np.ones((3, 3)))
            if n == 0:
                continue
            for i, sl in enumerate(ndimage.find_objects(lab), start=1):
                a0, a1 = sl[0].start, sl[0].stop
                r0, r1 = sl[1].start, sl[1].stop
                r_span = r1 - r0
                a_span = (a1 - a0) * 360.0 / N_AZ
                width_km = (r0 + r1) / 2.0 * np.radians(a_span)
                elong = r_span / width_km if width_km > 0 else 0.0
                long_enough = r_span >= BEAM_MIN_LEN_KM
                wedge = (all_gates and long_enough and a_span <= BEAM_MAX_DEG
                         and elong >= BEAM_ELONG and r0 <= BEAM_START_KM)
                needle = (long_enough and a_span <= BEAM_NARROW_DEG
                          and elong >= BEAM_NARROW_ELONG)
                hair = (r_span >= BEAM_HAIR_LEN and a_span <= BEAM_HAIR_DEG
                        and elong >= BEAM_HAIR_ELONG and r0 <= BEAM_START_KM)
                dash = (not all_gates and r_span >= BEAM_DASH_LEN
                        and a_span <= BEAM_HAIR_DEG and elong >= BEAM_DASH_ELONG
                        and r0 <= BEAM_START_KM)
                # Log any component that even looks ray-ish, flagged or not, so a
                # beam that fell just outside the shape gates is visible.
                if os.environ.get("RADAR_BEAM_DEBUG") and (r_span >= 40 or elong >= 3):
                    log.info("beam_diag %s %s%s: comp r%d-%d span%d aspan%.1f elong%.1f "
                             "start%d -> wedge=%s needle=%s hair=%s dash=%s", feed, name,
                             "" if all_gates else " (dash)", r0,
                             r1, r_span, a_span, elong, r0, wedge, needle, hair, dash)
                if not (wedge or needle or hair or dash):
                    continue
                comp = (lab == i)
                comp &= ~_embedded_range_mask(closed, a0, a1)[None, :]
                if not comp.any():
                    if os.environ.get("RADAR_BEAM_DEBUG"):
                        log.info("beam_diag %s %s: comp r%d-%d fully embedded — kept",
                                 feed, name, r0, r1)
                    continue
                kill |= comp
                removed.append(f"{name} {r0}-{r1} km {a_span:.1f} deg elong {elong:.1f}")
        if not kill.any():
            continue
        kill |= _sweep_dash_neighbours(occ, kill, ndimage)
        gone = keep & kill[az, rb]
        # An isolated beam lives in a near-empty frame, so its whole removal can
        # be well under the old 75 px "don't bother" floor and still be the only
        # thing worth removing. 30 px is a real feature, not speckle, and the
        # shape gates above already vouch that it is a ray.
        if os.environ.get("RADAR_BEAM_DEBUG"):
            log.info("beam_diag %s %s: would clear %d px (floor %d)",
                     feed, name, int(gone.sum()), BEAM_MIN_REMOVE_PX)
        if gone.sum() < BEAM_MIN_REMOVE_PX:
            continue
        if out is arr:
            out = arr.copy()
        out[gone] = 0                     # cleared outright, alpha and all
    if removed:
        log.info("source beams (%s): %s", feed, "; ".join(removed))
    return out


# The beam a shape test cannot see, and what to do about it.
#
# Every earlier pass asks where echo STOPS — thin residue after an opening,
# connected blobs in polar space, corridors with empty flanks. A ray lying
# inside rain stops nowhere. Measured on the 02.08 01:00 Latvian frame: in
# polar coordinates round Riga the rain and the ray are one connected blob
# 125 degrees wide and 202 km long, elongation 0.6. There is no shape there.
#
# What the ray always is, though, is a BEARING that does not belong with its
# neighbours — either reaching much further than they do, or running two whole
# intensity classes above them. So the bearings are judged, not the blobs.
#
# And once a bearing is condemned it is not deleted, it is REBUILT: every range
# along it takes the value interpolated across the wedge from the clean
# bearings either side. Where those are empty the ray disappears, which is what
# should happen to a ray in clear air. Where they carry rain, the rain crosses
# the ray unbroken, which is what should happen to a shower the beam was drawn
# on top of. Deleting the whole bearing would leave a wedge-shaped hole through
# real weather, and that is just a different artefact.
RIDGE_AZ_HALF = 24          # bins either side used for the flank baseline
RIDGE_AZ_GAP = 6            # ... skipping these, next to the ray itself
RIDGE_LIFT_CLASSES = 2.0    # how far above its flanks a ray has to sit
RIDGE_MIN_LEN_KM = 60       # ... and for how much range
RIDGE_MIN_KM = 30           # ignore the clutter-dominated close range
# How far past its flanks a bearing has to reach. 60 was set from one clear
# example and it turned out to be a bar the ordinary case does not clear: the
# beam reported on 31.07 22:01 runs 97.0-98.5deg out to 244 km against flanks
# at 194, a lift of 48-50 km on four contiguous bins with every neighbour at
# -3 or below. As clean a spike as the 109deg one on the same frame, which
# lifts 63. Dropping the bar to 45 catches it; the wedge cap still throws out
# anything wider than 5deg, so what this admits is narrow by construction.
REACH_LIFT_KM = 45
# The flanks have to HAVE a reach to be overshot. Without this an isolated
# shower on a bearing whose neighbours are empty reads as a hundred-kilometre
# spike, and on one frame that alone accounted for most of the detections.
REACH_MIN_FLANK_KM = 40
REACH_MAX_KM = 300          # past this we are outside every product anyway
BEAM_MAX_WEDGE_DEG = 5.0    # a group of bad bearings wider than this is weather
BEAM_FLANK_BINS = 3         # clean bearings offset to rebuild from
BEAM_GROW_BINS = 0
# What ONE wedge may take, as a fraction of the feed's echo in that frame.
# The frame ceiling below is all-or-nothing, so a single greedy detection used
# The old per-wedge veto (WEDGE_MAX_REMOVE = 4%) is gone: it spared exactly the
# beams that sit on rain, which is what we were asked to delete. The flank fill
# is the safeguard instead — a beam over rain is bridged, not lost.
#
# One disaster backstop remains, over the whole stage: if the NET removal
# (echo that ended up empty, after the flank fill had its chance to bridge)
# is more than this fraction of the feed's echo, none of it is applied. A net
# loss that large means a band of rain was condemned as a fan of rays with
# nothing beside it to bridge from — a misdiagnosis, and the frame is better
# left alone than blanked. Set well above any legitimate case: a real beam
# nets almost nothing (bridged), and even an isolated beam that is most of a
# sparse frame stays under it.
BEAM_MAX_REMOVE_FRAME = 0.55
# How far a condemned wedge may grow into its shoulders. ZERO, and that is a
# measurement rather than a default. Growing while a neighbouring bearing still
# half-failed the test looked right on one frame, and across 27 it took 8.24%
# of all echo instead of 1.09% — the soft criterion is satisfied by most
# bearings beside a real one, so nearly every wedge grew to its cap. The one
# shoulder bin either side stays; it is cheap and bounded.


def _source_site_xy(shape, sw, ne, lat0, lon0):
    """Where an antenna sits in a source overlay, in that image's pixels."""
    def merc_y(la):
        r = np.radians(la)
        return np.log(np.tan(r) + 1.0 / np.cos(r))
    h, w = shape[:2]
    u = (lon0 - sw[1]) / (ne[1] - sw[1])
    v = (merc_y(ne[0]) - merc_y(lat0)) / (merc_y(ne[0]) - merc_y(sw[0]))
    return u * w, v * h


def _repair_beams_source(lev: np.ndarray, feed: str | None, sw, ne):
    """Find bearings that do not belong, and rebuild them from their
    neighbours. Returns (levels, how many wedges were rebuilt)."""
    if (lev > 0).sum() < BEAM_MIN_PX:
        if os.environ.get("RADAR_BEAM_DEBUG"):
            log.info("beam_diag %s: only %d echo px (< BEAM_MIN_PX=%d) — beam "
                     "repair skipped for the whole frame", feed,
                     int((lev > 0).sum()), BEAM_MIN_PX)
        return lev, 0
    out, notes, kept = lev, [], 0
    names = _feed_site_names(feed)
    mine = [(n, la, lo) for n, la, lo in RADAR_SITES if n in names]
    if not mine:
        if os.environ.get("RADAR_BEAM_DEBUG"):
            log.info("beam_diag %s: no radar sites match this feed prefix", feed)
        return lev, 0        # every caller unpacks two; this path returned one
    # Each antenna judges only the pixels it is the nearest to. Without this,
    # Lithuania's Vilnius sees echo over Laukuva's half of the country as a
    # 300 km spike on its own bearings, and the same for Estonia's pair — on
    # one frame that was most of the detections.
    polar = [_source_polar(lev.shape, sw, ne, la, lo) for _, la, lo in mine]
    nearest = np.argmin(np.stack([p[0] for p in polar]), axis=0)
    for si, (name, la, lo) in enumerate(mine):
        rng, azdeg = polar[si]
        az = (azdeg / 360.0 * N_AZ).astype(int) % N_AZ
        rb = rng.astype(int)
        ok = (rb >= RIDGE_MIN_KM) & (rb <= R_MAX_KM) & (nearest == si)
        if (ok & (out > 0)).sum() < BEAM_MIN_PX:
            if os.environ.get("RADAR_BEAM_DEBUG"):
                log.info("beam_diag %s %s: only %d echo px in this site's own "
                         "sector (< %d) — site skipped", feed, name,
                         int((ok & (out > 0)).sum()), BEAM_MIN_PX)
            continue
        pol = np.zeros((N_AZ, R_MAX_KM + 1), np.float32)
        np.maximum.at(pol, (az[ok], rb[ok]), out[ok].astype(np.float32))

        ring = [k for k in range(-RIDGE_AZ_HALF, RIDGE_AZ_HALF + 1)
                if abs(k) > RIDGE_AZ_GAP]
        flank2d = np.median(np.stack([np.roll(pol, k, axis=0) for k in ring]), axis=0)

        # (a) reaches much further than the bearings around it
        reach = np.zeros(N_AZ, np.float32)
        lit = ok & (out > 0)
        np.maximum.at(reach, az[lit], rng[lit].astype(np.float32))
        fr = np.median(np.stack([np.roll(reach, k) for k in ring]), axis=0)
        bad = ((reach - fr) >= REACH_LIFT_KM) & (fr >= REACH_MIN_FLANK_KM) \
            & (reach <= REACH_MAX_KM)
        # (b) or runs far above them for a long stretch of range
        hot = ((pol - flank2d) >= RIDGE_LIFT_CLASSES * SUB) & (pol > 0) & (flank2d > 0)
        hotlen = hot.sum(axis=1)
        bad |= hotlen >= RIDGE_MIN_LEN_KM
        # Read-only diagnosis (RADAR_BEAM_DEBUG=1): the strongest reach-lift and
        # hot-run bearings for this site, flagged or not, so a beam that slipped
        # under the thresholds shows up as a near-miss rather than as silence.
        if os.environ.get("RADAR_BEAM_DEBUG"):
            reach_lift = reach - fr
            ro = np.argsort(-reach_lift)[:8]
            ho = np.argsort(-hotlen)[:8]
            log.info("beam_diag %s %s: %d bad bearings (thr reach>=%d flank>=%d, "
                     "hot>=%d km)", feed, name, int(bad.sum()), REACH_LIFT_KM,
                     REACH_MIN_FLANK_KM, RIDGE_MIN_LEN_KM)
            log.info("  top reach-lift: %s", "; ".join(
                f"{k*360.0/N_AZ:.0f}deg lift{reach_lift[k]:.0f} "
                f"(reach{reach[k]:.0f}/flank{fr[k]:.0f}) hot{int(hotlen[k])}"
                for k in ro))
            log.info("  top hot-run:    %s", "; ".join(
                f"{k*360.0/N_AZ:.0f}deg hot{int(hotlen[k])} lift{reach_lift[k]:.0f}"
                for k in ho))
        if not bad.any():
            continue

        # contiguous groups of condemned bearings, wrapping round the circle
        idx = np.flatnonzero(bad)
        groups, cur = [], [int(idx[0])]
        for k in idx[1:]:
            if k == cur[-1] + 1:
                cur.append(int(k))
            else:
                groups.append(cur); cur = [int(k)]
        groups.append(cur)
        if len(groups) > 1 and groups[0][0] == 0 and groups[-1][-1] == N_AZ - 1:
            groups[0] = groups[-1] + groups[0]; groups.pop()

        touched = np.zeros(N_AZ, bool)
        offs, gsel, gnotes = [], [], []
        # Rebuild by rotating the picture, not by reading the polar grid.
        #
        # The grid is far too sparse to rebuild from: 720 azimuths by 600
        # ranges is 432,000 cells and a 500 px source frame has about eleven
        # thousand echo pixels in it, so most cells are empty for want of a
        # pixel rather than for want of rain. Averaging three such bins gave
        # nearly zero, and the "repaired" bearing came out as a pale gash
        # through the rain instead of a beam removed from it.
        #
        # So each condemned pixel is sampled from where it would be if the
        # picture were turned about the antenna by the width of the wedge,
        # once each way, and takes the mean of the two. Mercator is conformal,
        # so turning in pixels is turning on the ground: same range, azimuth
        # moved off the ray. Those are real pixels with real values in them.
        ax, ay = _source_site_xy(lev.shape, sw, ne, la, lo)
        for g in groups:
            deg = len(g) * 360.0 / N_AZ
            if deg > BEAM_MAX_WEDGE_DEG:
                continue
            # Grow the wedge outward while the bearing still looks unlike its
            # surroundings. The tests condemn the core of a ray; its shoulders
            # fail them by a little and, left in place, are exactly what is
            # still visible as a line after the core has been rebuilt.
            # Two bins at most either side, and only for a bearing that still
            # half-fails the test. Letting this run to the wedge cap on a soft
            # criterion grew almost every wedge to its limit and took 9.9% of
            # all echo across 27 frames instead of 1.1%.
            soft_r = REACH_LIFT_KM / 2.0
            soft_h = RIDGE_MIN_LEN_KM / 2.0
            lo_i, hi_i, cap = g[0], g[-1], BEAM_GROW_BINS
            for _ in range(cap):
                nxt = (lo_i - 1) % N_AZ
                if (reach[nxt] - fr[nxt]) >= soft_r or hotlen[nxt] >= soft_h:
                    lo_i = nxt
                else:
                    break
            for _ in range(cap):
                nxt = (hi_i + 1) % N_AZ
                if (reach[nxt] - fr[nxt]) >= soft_r or hotlen[nxt] >= soft_h:
                    hi_i = nxt
                else:
                    break
            n_g = (hi_i - lo_i) % N_AZ + 1
            g = [(lo_i + k) % N_AZ for k in range(n_g)]
            g = [(g[0] - 1) % N_AZ] + g + [(g[-1] + 1) % N_AZ]   # shoulders too
            deg = len(g) * 360.0 / N_AZ
            touched[g] = True
            gnotes.append(f"{name} bearing {g[0] * 360.0 / N_AZ:.1f}+{deg:.1f}deg, "
                          f"reach {reach[g].max():.0f} vs {fr[g].max():.0f} km")
            offs.append(np.radians(deg + BEAM_FLANK_BINS * 360.0 / N_AZ))
            gsel.append(np.zeros(N_AZ, bool)); gsel[-1][g] = True
        if not touched.any():
            continue
        for off, gs, note in zip(offs, gsel, gnotes):
            sel = ok & gs[az]
            if not sel.any():
                continue
            sy, sx = np.nonzero(sel)
            dx, dy = sx - ax, sy - ay
            vals = []
            for sign in (+1.0, -1.0):
                c, s_ = math.cos(sign * off), math.sin(sign * off)
                rx = np.rint(ax + c * dx - s_ * dy).astype(int)
                ry = np.rint(ay + s_ * dx + c * dy).astype(int)
                inb = ((rx >= 0) & (rx < lev.shape[1])
                       & (ry >= 0) & (ry < lev.shape[0]))
                v = np.zeros(len(sx), np.float32)
                v[inb] = lev[ry[inb], rx[inb]]
                vals.append(v)
            # Both flanks have to agree that there is rain here, or the pixel
            # goes dry. Filling from whichever single side happens to hold echo
            # is what left the beam visible: outside a storm one flank holds
            # scattered echo and the other holds nothing, and half of something
            # is still something — a pale class-1/2 streak running the whole
            # length of the condemned bearing. That one-sided fill is exactly
            # the residual "cone" that survived cleaning.
            #
            # Requiring both flanks keeps the one case that should be kept — a
            # real rain band crossing the ray, where both neighbours are wet, is
            # bridged at full strength — and deletes everything else along the
            # ray: a beam grazing scattered cells, or standing in clear air, is
            # erased rather than half-rebuilt into a line. The whole-frame
            # BEAM_MAX_REMOVE_FRAME backstop still guards against condemning an
            # actual narrow band that has nothing beside it to bridge from.
            both = (vals[0] > 0) & (vals[1] > 0)
            newv = np.where(both,
                            np.clip(np.rint((vals[0] + vals[1]) / 2.0),
                                    0, LEV_MAX), 0).astype(np.uint8)
            if out is lev:
                out = lev.copy()
            out[sy, sx] = newv
            notes.append(note)
            kept += 1
    if notes:
        log.info("source beams rebuilt (%s): %s", feed, "; ".join(notes))
    return out, kept


# Cross-feed calibration onto the shared ramp (probe 22.08.2026, middle
# anchor chosen by the user from test renders = FMI's rain-rate product / 2).
# Co-wet-pixel medians said the meteolapa palettes under-read against it:
# FI/EE x4.5 and, chained through SE's overlaps, FI/LT2 x3.33. Halved to the
# middle anchor. The nordic feeds carry their calibration in their decode
# (radar_nordic: SMHI_DBZ_ADJ / FMI_RATE_DIV) — never scale them here.
CAL_LEV_FACTOR = {"EE": 2.25, "LT2": 1.67, "LT": 1.67, "LV": 1.67}
# NOT a source of calibration: the Surgavere crescent. Cutting the Estonian
# composite at 250 km does not separate its two radars — the product's echo
# reaches 307-374 km from Harku (measured 22.08.2026 on every wet sweep), so
# Harku fills most of the crescent that carries Surgavere's name, and the
# region pulsates frame to frame. Numbers taken there describe a patch of the
# merged mosaic. Calibrate Estonia against neighbours over the WHOLE product,
# or wait for per-radar volumes.
#
# Where the ring does agree: SE, FI and LT2 sit within 3-6% of each other on
# 22.08 (FI/SE 1.05x near field, LT2/SE 1.03x), and against that trio Latvia
# reads about 2x high (LT2/LV 0.48x, 0.52x near field) — its 1.67 above is
# the next thing to revisit.
# Estonia's x2.25 is the crudest of these, and it is crude because the source
# is a picture: a meteolapa EE frame holds EIGHT distinct RGBA values, so its
# intensity arrives as ~6 colour steps and everything between them is our
# invention (hue-placed pixels sit at the middle of their class).
#
# There IS a numeric Estonian source, found 25.08.2026 and deliberately not
# wired up — recorded here so the search does not have to happen twice:
#
#   https://ilmgs.envir.ee/geoserver/ows?service=WCS&version=2.0.1
#     &request=GetCoverage&coverageId=ilm__cmp_cap&format=image/tiff
#     &subset=X(<northing lo>,<hi>)&subset=Y(<easting lo>,<hi>)
#     &subset=time("2026-08-25T02:50:00.000Z")
#
# float32 on EPSG:3301 at 360 m, 5-min TIME axis reaching ~2 days back,
# ~2,700 distinct values per frame against the palette's 8. Note the axis
# labels are reversed from the obvious reading — their X is the northing —
# which is why a sane-looking bbox returns "empty intersection".
#
# Why it was left alone: it is the same Harku+Surgavere COMPOSITE we already
# have, so it separates nothing, and at 360 m against the palette's ~480 m it
# is no more detailed than what we draw at 1 km. The only real gain is honest
# intensity in place of this factor — worth doing the day EE's calibration
# matters more than the work, and not before.


def _calibrate_lev(lev, feed):
    k = CAL_LEV_FACTOR.get(feed or "", 1.0)
    if k == 1.0:
        return lev
    out = lev.copy()
    wet = lev > 0
    out[wet] = rate_to_lev(
        pos_rain((lev[wet].astype(np.float64) - 0.5) / LEV_MAX) * k)
    return out


def _prepare_source(arr: np.ndarray, feed: str | None, o: dict | None):
    """The raw prepare + the per-feed level calibration every caller needs."""
    return _calibrate_lev(_prepare_source_raw(arr, feed, o), feed)


def _prepare_source_raw(arr: np.ndarray, feed: str | None, o: dict | None):
    """Everything that belongs in the radar's own frame, then classify.

    With one guard over the whole stage: if the beam work would take more than
    BEAM_MAX_REMOVE_FRAC of the frame's echo, none of it is applied. Any
    removal that large is a misdiagnosis by definition — a narrow band of rain
    read as a fan of rays — and the frame is better left alone than blanked.
    """
    plain = _classify_intensity(arr, feed)
    if o is None:
        return plain
    cleaned = _remove_beams_source(arr, feed, o["sw"], o["ne"])
    lev = plain if cleaned is arr else _classify_intensity(cleaned, feed)
    lev, wedges = _repair_beams_source(lev, feed, o["sw"], o["ne"])
    if lev is plain:
        return plain
    # Only a disaster backstop now, not a per-beam veto. With the flanks doing
    # the fill, a real beam over rain nets almost no loss (it is bridged), so a
    # large net removal means the detector condemned a fan of bearings that was
    # actually a band of rain with nothing beside it to bridge from. Above this
    # the frame is left alone; below it, confident beams — including an isolated
    # one that is most of a sparse frame — are deleted as asked.
    was = int((plain > 0).sum())
    gone = int(((plain > 0) & (lev == 0)).sum())
    if was and gone / was > BEAM_MAX_REMOVE_FRAME:
        log.warning("source beams (%s): %d wedges would net %.0f%% of the "
                    "echo (%d of %d px) — misdiagnosis, leaving the frame alone",
                    feed, wedges, 100 * gone / was, gone, was)
        return plain
    return lev


def beam_diag(ts_iso: str) -> None:
    """Read-only beam-detector diagnosis for the frame nearest `ts_iso`
    (e.g. "2026-08-11T17:00"). Fetches the source overlays around that time and
    runs the same source-space classify + beam repair each feed gets in the
    live composite, with RADAR_BEAM_DEBUG logging on, but writes nothing. Use it
    to see whether a beam was never flagged (near-miss on the thresholds) or
    flagged and then vetoed by a removal cap, before touching any threshold."""
    os.environ.setdefault("RADAR_BEAM_DEBUG", "1")
    s = session()
    target = dt.datetime.fromisoformat(ts_iso)
    if target.tzinfo is None:
        target = target.replace(tzinfo=dt.timezone.utc)
    overlays = fetch_overlays_window(target - dt.timedelta(minutes=40),
                                     target + dt.timedelta(minutes=40))
    log.info("beam_diag %s: %d overlays in +-40 min window", ts_iso, len(overlays))
    for code in ("EE", "LT2", "LT", "LV"):
        cands = [o for o in overlays if o["code"] == code]
        if not cands:
            log.info("beam_diag %s: no overlay in window", code)
            continue
        best = min(cands, key=lambda o: abs((o["time"] - target).total_seconds()))
        log.info("beam_diag %s: nearest sweep %s (%+d min)", code,
                 best["time"].strftime("%H:%M"),
                 round((best["time"] - target).total_seconds() / 60))
        # Runs _prepare_source -> _repair_beams_source, which logs the notes and
        # (with the debug env) the per-bearing near-miss table. Nothing stored.
        lev = _load_and_classify(best["url"], s, code, best)
        if lev is None:
            log.info("beam_diag %s: overlay did not load (fetch/decoded None)", code)
        else:
            log.info("beam_diag %s: classified echo = %d px (BEAM_MIN_PX=%d)",
                     code, int((lev > 0).sum()), BEAM_MIN_PX)


def _load_and_classify(url: str, s, feed: str | None = None,
                       o: dict | None = None) -> np.ndarray | None:
    body = overlay_bytes(url, s)
    if body is None:
        return None
    img = Image.open(io.BytesIO(body)).convert("RGBA")
    if max(img.size) > LT_MAXSIZE:
        sc = LT_MAXSIZE / max(img.size)
        img = img.resize((int(img.width * sc), int(img.height * sc)),
                         Image.NEAREST)
    return _prepare_source(np.array(img), feed, o)


# Coverage is geometry, not opacity. Estonia paints its whole disc — 76,061 px
# against 18,475 px of echo on the 31.07 20:01 frame — but Latvia and Lithuania
# paint only where it rains, so their opaque area IS the echo and fading by it
# would eat the weather. Measured on that frame, the furthest covered pixel is
# 255 km from the nearest antenna for Estonia, 242 for Lithuania, 233 for
# Latvia, so 250 km is the working range for all of them.
RADAR_RANGE_KM_COVER = 250
EDGE_FADE_KM = 25
# What _fade_edges is left doing once the real fade is per feed: a radius no
# product reaches, so it cannot second-guess a rim that has already been
# handled and can still catch a layer drawn with no feed name.
BACKSTOP_RANGE_KM = 320
# The coverage-aware fade makes a feed's rendered weight depend on WHICH
# OTHER feeds are in the frame. On days with mixed sweep cadence the active
# set fluctuates between adjacent frames, so the fade pattern pulses when
# seeking — the user turned it off 23.08 in favour of the deterministic
# per-feed static fades (_weak_alpha; SE uses its product-edge solo ramp).
# The aware machinery stays for a possible re-enable.
ADAPTIVE_FADE = False
BACKSTOP_FADE_KM = 30
# Weak echo fades much earlier than the coverage edge, and this is why.
#
# Estonia paints faint rings at long range — you can see them as arcs over the
# gulf with clear air inside. I tried five ways to cut them and could not find
# one that is safe:
#
#   a flat range cut         removed 73% of Latvia's echo, which is real: LV
#                            has one radar, so most of its country IS long range
#   isolated weak blobs      removed 89% of Estonia on a day its whole field
#                            was genuinely light rain
#   per-frame ring detector  the ring in the reported frame is 110 km wide, not
#                            a narrow spike, and the test false-fired elsewhere
#   a persistent clutter map 60 frames over four days: median echo frequency
#                            beyond 200 km is 0.00, so the rings are episodic,
#                            not fixed. Nothing to subtract.
#   a beam-height rule       physically right, and at the 4 km height that
#                            catches 72% of the ring it also takes 60% of Latvia
#
# On one frame, an artefact ring and real drizzle at 220 km are the same
# picture. So this does not delete anything — it makes weak returns fade with
# range, which is honest about how much a 0.2 mm/h echo from five kilometres up
# is worth. A ring at 230 km renders at a sixth of its weight and stops
# competing for attention; a heavy core at the same range is untouched, because
# a strong return from high up is real weather.
# What settles it is how many radars each country has. Measured against the
# borders file: 94% of Estonia's land is within 150 km of an Estonian antenna
# and 98% within 180, and Lithuania is 96% and 100% — both have two radars for
# a small country. Latvia has one, at Riga, and only 64% of Latvia is inside
# 150 km, 78% inside 180. So Estonian echo beyond 180 km is almost never over
# Estonia; it is over the sea, or over Latvia where Latvia's own radar is
# looking at the same sky from closer. Latvia's long range is not spare.
#
# Hence a per-feed limit on WEAK echo only, faded in over the last 40 km so
# there is no rim. Strong returns are left alone everywhere: a heavy core at
# 230 km is real weather even if the beam is five kilometres up.
#
# Not the cause of the arc reported on 02.08, which was tested rather than
# assumed: the hard-edge pixels in that frame cluster at 244-248 km from Harku,
# 236-240 from Riga and 224-232 from the Lithuanian pair — the rims of the
# products themselves — and not at 180. See FEED_RIM_KM.
WEAK_RANGE_KM = {"EE": 180.0, "LT": 180.0, "LT2": 180.0, "LV": 250.0,
                 "SE": 180.0, "FI": 180.0}
WEAK_FADE_KM = 40.0
# Sole-witness weight cap for rescued far echo, per feed (see the a_far note
# in _weak_alpha_aware).
FAR_WEIGHT = {"EE": 0.5}
WEAK_CLASSES = 2


@functools.lru_cache(maxsize=4)
def _coverage_alpha(range_km: int = RADAR_RANGE_KM_COVER,
                    fade_km: int = EDGE_FADE_KM) -> np.ndarray:
    """1 inside the radar discs, ramping to 0 over the last fade_km of range.

    A hard rim reads as a weather boundary — a straight edge or a clean arc is
    the one shape rain never has. It is also where the data is weakest: at
    maximum range the beam is kilometres up and kilometres wide, so what it
    reports is barely a surface observation. Fading it says both things at once.
    """
    from . import proj3059 as P

    yy, xx = np.mgrid[0:P.H, 0:P.W]
    d = np.full((P.H, P.W), np.inf, dtype=np.float32)
    for _, sx, sy in _site_pixels():
        d = np.minimum(d, np.hypot(xx - sx, yy - sy).astype(np.float32))
    return np.clip((range_km - d) / float(fade_km), 0.0, 1.0).astype(np.float32)


@functools.lru_cache(maxsize=1)
def _range_km() -> np.ndarray:
    """Distance to the nearest antenna, in kilometres (1 px = 1 km)."""
    from . import proj3059 as P
    yy, xx = np.mgrid[0:P.H, 0:P.W]
    d = np.full((P.H, P.W), np.inf, dtype=np.float32)
    for _, sx, sy in _site_pixels():
        d = np.minimum(d, np.hypot(xx - sx, yy - sy).astype(np.float32))
    return d


@functools.lru_cache(maxsize=8)
def _feed_range_km(feed: str) -> np.ndarray | None:
    """Distance to the nearest antenna OF THIS FEED, in km. None if unknown."""
    from . import proj3059 as P
    mine = _feed_site_names(feed)
    sites = [(sx, sy) for n, sx, sy in _site_pixels() if n in mine]
    if not sites:
        return None
    yy, xx = np.mgrid[0:P.H, 0:P.W]
    d = np.full((P.H, P.W), np.inf, np.float32)
    for sx, sy in sites:
        d = np.minimum(d, np.hypot(xx - sx, yy - sy).astype(np.float32))
    return d


@functools.lru_cache(maxsize=8)
def _weak_alpha(feed: str) -> np.ndarray:
    """Multiplier for this feed's weak echo: 1 in close, 0 past its limit."""
    from . import proj3059 as P
    if feed == "SE":                # product-edge geometry, see below
        e = _se_edge_km()
        if e is None:
            return np.ones((P.H, P.W), np.float32)
        return np.clip(e / SE_EDGE_SOLO_KM, 0.0, 1.0).astype(np.float32)
    d = _feed_range_km(feed)
    if d is None:
        return np.ones((P.H, P.W), np.float32)
    lim = WEAK_RANGE_KM.get(feed or "", 250.0)
    return np.clip((lim - d) / WEAK_FADE_KM, 0.0, 1.0).astype(np.float32)


# The per-feed limit assumes the neighbour whose territory the far echo sits
# over is watching the same sky from closer. Usually true — but when that
# radar is quiet, the far echo is the only witness there, and fading it erases
# real rain. Seen 17.08 15-17 UTC: the LV frames held 403 / 1 / 37 echo px in
# TOTAL, a cell in east Latvia lived only in Vilnius's sweep at ~240 km, and
# the 180 km LT limit hid it (case book: jobs/check_beam_cases.py). So the
# limit is coverage-AWARE: weak echo beyond a feed's own limit is faded only
# where another feed that is demonstrably awake this frame covers the pixel
# from inside its own limit; where nobody closer is awake, the limit relaxes
# to the coverage range and the far echo survives.
#
# "Awake" is judged from the frame itself: a feed claims coverage only if it
# painted a meaningful amount of echo — an absolute floor, and a fraction of
# the busiest feed so a 1-px LV frame next to a 15,000-px LT2 storm does not
# pass. A quiet-but-healthy radar on a locally dry day can be judged asleep,
# which errs toward SHOWING a neighbour's far echo — the safe direction: real
# rain kept beats an occasional far-range smear.
ACTIVE_MIN_PX = 200
ACTIVE_MIN_FRAC = 0.02


def active_feeds(layers) -> frozenset:
    """Which of this frame's feeds are awake enough to claim coverage.

    layers: [(code, level grid), ...] as collected by a composite builder."""
    counts = {code: int((g > 0).sum()) for code, g in layers}
    busiest = max(counts.values(), default=0)
    return frozenset(c for c, n in counts.items()
                     if n >= ACTIVE_MIN_PX and n >= busiest * ACTIVE_MIN_FRAC)


# SE is a ~12-radar national composite, not the two antennas we list for it —
# judging it by antenna distance zeroed real echo over the Baltic wherever LV
# claimed coverage (user report 22.08, hole west of the Estonian islands).
# Its honest geometry is distance to the PRODUCT's coverage edge, bundled as
# a km map generated from the footprint itself (0 = outside coverage).
SE_EDGE_FADE_KM = 70.0     # full fade depth where another awake feed covers
SE_EDGE_SOLO_KM = 20.0     # sole witness: only a soft melt at the very edge


SE_EDGE_KEY = "maps/radar_src/se_edge_km.png"


@functools.lru_cache(maxsize=1)
def _se_edge_km() -> np.ndarray | None:
    """km-to-product-edge map, written to R2 by the SE decode itself
    (radar_nordic.store_edge_map) so the footprint stays current."""
    try:
        body = r2_client().get_object(Bucket=config.R2_BUCKET,
                                      Key=SE_EDGE_KEY)["Body"].read()
        return np.array(Image.open(io.BytesIO(body)), np.float32)
    except Exception:
        log.warning("SE edge map missing — SE gets no weak fade")
        return None


def _feed_cover_alpha(feed: str) -> np.ndarray | None:
    """How firmly this feed covers each pixel from within its own limit."""
    if feed == "SE":
        e = _se_edge_km()
        return None if e is None else np.clip(e / WEAK_FADE_KM, 0.0, 1.0)
    d = _feed_range_km(feed)
    if d is None:
        return None
    lim = WEAK_RANGE_KM.get(feed or "", 250.0)
    return np.clip((lim - d) / WEAK_FADE_KM, 0.0, 1.0)


@functools.lru_cache(maxsize=32)
def _weak_alpha_aware(feed: str, active: frozenset) -> np.ndarray:
    """Coverage-aware weak-echo fade for one feed, given who is awake."""
    from . import proj3059 as P
    # How firmly some OTHER awake feed covers each pixel, 0..1 (smooth over
    # the same 40 km the fade itself uses, so the handover has no seam).
    cover = np.zeros((P.H, P.W), np.float32)
    for other in active:
        if (other or "")[:2] == (feed or "")[:2]:
            continue                      # LT and LT2 share antennas
        co = _feed_cover_alpha(other)
        if co is not None:
            cover = np.maximum(cover, co)
    if feed == "SE":
        e = _se_edge_km()
        if e is None:
            return np.ones((P.H, P.W), np.float32)
        # Where another awake feed covers: fade out over the last
        # SE_EDGE_FADE_KM inside the product edge. Where SE is the only
        # witness the echo is real and stays — with just a soft melt over the
        # final SE_EDGE_SOLO_KM so the product wall never shows sharp.
        a_covered = np.clip(e / SE_EDGE_FADE_KM, 0.0, 1.0)
        a_solo = np.clip(e / SE_EDGE_SOLO_KM, 0.0, 1.0)
        return np.maximum(a_covered, (1.0 - cover) * a_solo).astype(np.float32)
    d = _feed_range_km(feed)
    if d is None:
        return np.ones((P.H, P.W), np.float32)
    own = WEAK_RANGE_KM.get(feed or "", 250.0)
    # Where somebody closer is awake: the original fade, unchanged. Where
    # nobody is: the far echo is still shown but ALWAYS fades with range — a
    # slow ramp from full weight at the feed's own limit down to nothing at
    # the 320 km backstop, so rescued echo never presents a sharp edge and its
    # weight is honest about how far away the radar was. The 17.08 cell at
    # ~245 km from Vilnius renders at roughly half weight: visible, and
    # visibly long-range.
    a_near = np.clip((own - d) / WEAK_FADE_KM, 0.0, 1.0)
    a_far = np.clip((BACKSTOP_RANGE_KM - d) / (BACKSTOP_RANGE_KM - own), 0.0, 1.0)
    # Per-feed cap on the rescued far echo. EE's rim paints a broad pale wash
    # at its range border (user report 22.08 09:00, arc over the gulf) —
    # bright-band overshoot, not surface rain, so its sole-witness weight is
    # halved on top of the range ramp.
    a_far = a_far * FAR_WEIGHT.get(feed or "", 1.0)
    return np.maximum(a_near, (1.0 - cover) * a_far).astype(np.float32)


# A coverage edge is a circle centred on an antenna. Nothing else is.
#
# The reported arc on 02.08 01:00 turned out to be neither the product rim nor
# the weak-echo limit. Estonia's source paints its whole coverage grey and
# colours only where it rains — verified on a dry frame, where grey is 98% of
# the painted area and 100% of the inner half of the disc — so the boundary
# between colour and grey is USUALLY a rain edge. On that frame it was not:
# fitting a circle to it gave a radius of 248 km centred 10 km from Harku, rms
# 1.2 km over 420 boundary pixels. Surgavere was contributing nothing, so the
# picture was Harku's disc alone and the rain filled it to the edge.
#
# Rain does not do that. So rather than assume a radius — which cannot work,
# because which radars are up changes hour to hour — each frame is asked
# whether its echo boundary lies on a circle around one of this feed's own
# antennas, over a wide enough arc that weather could not have arranged it.
# Where it does, the last RING_FEATHER_KM inside that circle are faded out.
#
# The same geometric prior the beam removal uses, and for the same reason: an
# antenna is the only thing in the picture that can draw a circle.
# The test is not "are there boundary pixels at this radius" — a big blob of
# rain has boundary pixels at every radius, and asking that way finds a ring in
# almost every frame. It is "does the echo STOP at the same radius across many
# consecutive bearings", which is a statement rain cannot satisfy: an edge that
# holds to within a couple of kilometres over seventy degrees of arc was drawn
# by an antenna.
RING_SECTOR_DEG = 5
RING_MIN_SECTORS = 10        # 50 degrees of arc; at 250 km that is 220 km of it
RING_TOL_KM = 5.0            # how far a bearing may sit off the common radius
RING_MAX_SD_KM = 3.5         # and how much they may scatter as a group
RING_GAP_SECTORS = 1         # one bearing may disagree without ending the run
RING_MIN_PX_SECTOR = 12      # echo pixels before a sector has an opinion
# Only look where a coverage edge can plausibly be. These radars rim out at
# 240-260 km; the two candidates the search found at 134 km from Surgavere were
# almost certainly the edge of a rain area that happened to curve, and there is
# no evidence of a blockage ring there to justify keeping them.
RING_SEARCH_KM = (180, 300)
# How far inside the arc the taper runs. Estonia gets twice as much as the
# rest: its coverage edge is where its wide, pale, light-rain fields end, and a
# 25 km taper across one of those is still legible as a line. The Latvian and
# Lithuanian rims usually cut a smaller, brighter echo where a shorter taper is
# enough and a longer one would eat weather.
RING_FEATHER_KM = {"EE": 50.0}
RING_FEATHER_DEFAULT = 25.0
RING_EDGE_DEG = 12.0         # soften the ends of the arc, or we swap one edge


def _coverage_rings(echo: np.ndarray, feed: str) -> list[tuple]:
    """[(cx, cy, radius, from_deg, to_deg)] where the echo stops on a circle."""
    if not echo.any():
        return []
    ys, xs = np.nonzero(echo)
    out = []
    nsec = 360 // RING_SECTOR_DEG
    for name, sx, sy in _site_pixels():
        if not name.startswith((feed or "")[:2]):
            continue
        d = np.hypot(xs - sx, ys - sy)
        sec = ((np.degrees(np.arctan2(ys - sy, xs - sx)) + 360) % 360
               // RING_SECTOR_DEG).astype(int)
        # how far the echo reaches on each bearing
        far = np.full(nsec, np.nan)
        cnt = np.bincount(sec, minlength=nsec)
        lo, hi = RING_SEARCH_KM
        for s in range(nsec):
            if cnt[s] < RING_MIN_PX_SECTOR:
                continue
            r = float(np.percentile(d[sec == s], 99.0))
            if lo <= r <= hi:
                far[s] = r
        # For each radius the frame actually offers, how long a run of bearings
        # agrees with it. Not "how tight is this run" — the echo only reaches
        # the coverage edge where there is rain to reach it, so the run has to
        # survive a bearing or two falling short.
        best = None
        for cand in np.unique(far[~np.isnan(far)]):
            ok = np.abs(far - cand) <= RING_TOL_KM
            ok[np.isnan(far)] = False
            # walk every start, allowing RING_GAP_SECTORS misses in a row
            for start in range(nsec):
                if not ok[start]:
                    continue
                hits, gap, k = [], 0, 0
                while k < nsec:
                    s = (start + k) % nsec
                    if ok[s]:
                        hits.append(far[s]); gap = 0
                    else:
                        gap += 1
                        if gap > RING_GAP_SECTORS:
                            break
                    k += 1
                span = k - gap
                if (len(hits) >= RING_MIN_SECTORS
                        and float(np.std(hits)) <= RING_MAX_SD_KM
                        and (best is None or len(hits) > best[0])):
                    best = (len(hits), start, float(np.median(hits)), span)
        if best is None:
            continue
        n, start, r, span = best
        a0 = start * RING_SECTOR_DEG
        a1 = a0 + span * RING_SECTOR_DEG
        log.info("coverage ring: %s at %.0f km over %d deg of arc (%d bearings)",
                 name, r, span * RING_SECTOR_DEG, n)
        out.append((sx, sy, r, float(a0), float(a1)))
    return out


def _rim_alpha(lev: np.ndarray, feed: str) -> np.ndarray | None:
    """Fade the last kilometres inside any coverage arc this frame shows."""
    rings = _coverage_rings(lev > 0, feed)
    if not rings:
        return None
    from . import proj3059 as P
    yy, xx = np.mgrid[0:P.H, 0:P.W]
    a = np.ones((P.H, P.W), np.float32)
    feather = RING_FEATHER_KM.get((feed or "").upper(), RING_FEATHER_DEFAULT)
    for cx, cy, r, a0, a1 in rings:
        d = np.hypot(xx - cx, yy - cy)
        ang = (np.degrees(np.arctan2(yy - cy, xx - cx)) + 360) % 360
        # distance in degrees into the arc, so the ends taper instead of
        # replacing a curved edge with two radial ones
        off = np.minimum((ang - a0) % 360, (a1 - ang) % 360)
        inside = ((ang - a0) % 360) <= ((a1 - a0) % 360 or 360)
        w = np.where(inside, np.clip(off / RING_EDGE_DEG, 0.0, 1.0), 0.0)
        fade = np.clip((r - d) / feather, 0.0, 1.0)
        a = np.minimum(a, 1.0 - w * (1.0 - fade))
    return a.astype(np.float32)


def draw_borders(canvas: Image.Image) -> None:
    """Coast and frontier onto the overlay, cased so it reads without a
    background underneath.

    Module level because two paths drew it and they drew it differently — the
    live composite a single hairline in one grey, the backfill a pale halo
    under a dark core. Re-rendering an hour therefore changed how it looked
    even when nothing about the weather had. One implementation now.
    """
    from PIL import ImageDraw

    from . import proj3059 as P
    borders = json.load(open(os.path.join(
        os.path.dirname(__file__), "assets", "borders_baltic.json")))
    d = ImageDraw.Draw(canvas)
    for c in borders:
        for line in c["lines"]:
            px, py = P.to_px([la for _, la in line], [lo for lo, _ in line])
            pts = list(zip(px.tolist(), py.tolist()))
            d.line(pts, fill=(247, 241, 226, 230), width=3)
            d.line(pts, fill=(85, 80, 63, 255), width=1)


def _fade_edges(canvas: Image.Image) -> Image.Image:
    """A backstop, and only that.

    The real fading is per feed and happens in _recolor, against each product's
    own antennas — see _rim_alpha. This still runs, but at a radius no feed
    reaches, so it can only catch a layer drawn without a feed name and never
    bites inside a rim that has already been faded properly.
    """
    a = np.array(canvas)
    a[..., 3] = (a[..., 3].astype(np.float32)
                 * _coverage_alpha(BACKSTOP_RANGE_KM, BACKSTOP_FADE_KM)).astype(np.uint8)
    return Image.fromarray(a, "RGBA")


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
    # One lookup for the whole frame: every source is being drawn for the same
    # hour, so they share an isotherm.
    cold = _freezing_mask(dt.datetime.now(dt.timezone.utc))
    # The strongest class any feed put on each pixel, so the fade can tell weak
    # echo from heavy. Taken from the classes, not read back off the canvas:
    # once four feeds are composited the pixels are blended.
    newest = None
    layers = []
    # Best-effort: a neighbour outage never touches the composite. Where they
    # land in the stack is DRAW_ORDER's business, not fetch order's — the
    # layers are sorted into it before anything is drawn.
    try:
        from . import radar_nordic
        layers += radar_nordic.live_layers(s3)
    except Exception:
        log.exception("nordic feeds skipped")
    for code in DRAW_ORDER:                  # trust order, see DRAW_ORDER
        frames = by_code.get(code)
        if not frames:
            continue
        use = frames[:frames_back]
        if not use:
            continue
        newest = max(newest or use[0]["time"], use[0]["time"])
        cls_stack = []
        for f in use:
            cls = _load_and_classify(f["url"], s, code, f)
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
        # Warp to the shared grid BEFORE cleaning. The geometric filters place
        # the antennas with proj3059 and assume 1 px = 1 km; run on the source
        # overlay's own pixels those coordinates point at nothing, which is why
        # beams kept surviving however the test was tuned.
        cur = P.resample_mercator_image(cur, f0["sw"], f0["ne"])
        cur = _close_radial_spokes(_despeckle_intensity(
            _remove_beam_lines(_remove_beams_polar(_despeckle(cur)))))
        layers.append((code, cur))
    # Recolor once every feed is in, so the weak-echo fade knows which radars
    # are awake this frame (coverage-aware — see _weak_alpha_aware).
    act = active_feeds(layers)
    # Sort the whole stack — nordic feeds arrive first from live_layers, the
    # meteolapa ones are appended after, and neither order is the one that
    # should decide who owns a shared pixel.
    layers.sort(key=lambda cg: DRAW_ORDER.index(cg[0])
                if cg[0] in DRAW_ORDER else len(DRAW_ORDER))
    for code, cur in layers:
        canvas.alpha_composite(_recolor(cur, cold, code, act))

    if newest is None:
        raise RuntimeError("no radar frames fetched")
    canvas = _fade_edges(canvas)
    draw_borders(canvas)
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


# How close to the top of the hour a stored frame has to be before an hour
# counts as covered — as a function of how many times the backfill has already
# tried to improve it. The first pass insists on a near-perfect frame (≤4 min)
# and revisits everything else; the second relaxes to ≤6, and the third and
# every pass after it to ≤9. So an hour whose upstream simply never published a
# sweep nearer than, say, 8 min keeps getting retried a couple of times, then
# settles instead of being re-fetched forever. Index is the attempt count,
# clamped to the last entry.
RADAR_TOL_SCHEDULE_MIN = (4, 6, 9)


def _hour_tol_min(tries: int) -> int:
    """Coverage tolerance in minutes for an hour tried `tries` times."""
    return RADAR_TOL_SCHEDULE_MIN[min(tries, len(RADAR_TOL_SCHEDULE_MIN) - 1)]


def _frame_offset_s(tag: str | None, hour: dt.datetime) -> float:
    """Seconds between a stored frame's sweep time and the top of its hour;
    inf when there is no frame or the tag will not parse."""
    if not tag:
        return float("inf")
    try:
        t = dt.datetime.strptime(tag[:13], "%Y%m%dT%H%M").replace(
            tzinfo=dt.timezone.utc)
    except (ValueError, TypeError):
        return float("inf")
    return abs((t - hour).total_seconds())


def _archive_note(s3, vtag: str) -> None:
    """Permanent hourly index: for each hour keep the frame NEAREST the top of
    it. A sweep closer to :00 replaces a farther one already stored — so an
    early tick that grabbed :35 before meteolapa published the on-hour sweep is
    upgraded the moment the :05 arrives. Frames on R2 are never deleted, so the
    superseded bytes simply stop being referenced."""
    hour = vtag[:11]  # YYYYMMDDTHH
    try:
        arch = json.loads(s3.get_object(
            Bucket=config.R2_BUCKET, Key="maps/radar/archive.json"
        )["Body"].read())
    except Exception:
        arch = {"path": "maps/radar/frames3059", "hours": {}}
    hour_dt = dt.datetime.strptime(hour, "%Y%m%dT%H").replace(tzinfo=dt.timezone.utc)
    if _frame_offset_s(vtag, hour_dt) < _frame_offset_s(arch["hours"].get(hour), hour_dt):
        arch["hours"][hour] = vtag
        # Bump the per-hour revision so the stable frame URL busts its cache the
        # moment the hour is redrawn (same mechanism the backfill uses).
        arch.setdefault("rev", {})[hour] = dt.datetime.now(
            dt.timezone.utc).strftime("%y%m%d%H%M")
        s3.put_object(Bucket=config.R2_BUCKET, Key="maps/radar/archive.json",
                      Body=json.dumps(arch).encode(),
                      ContentType="application/json",
                      CacheControl="public, max-age=300")


def _hour_covered(arch: dict, hour: dt.datetime) -> bool:
    """Covered only if the hour's frame is within the current tolerance of :00,
    and the tolerance widens with each attempt (see RADAR_TOL_SCHEDULE_MIN). A
    frame the live job grabbed at :35 — or one a pass stored before the on-hour
    sweep was published — starts out not covered, so the hour is revisited and
    upgraded to a closer sweep when one appears; after a few tries the bar
    relaxes so a genuinely-unbeatable frame stops being re-fetched."""
    hk = hour.strftime("%Y%m%dT%H")
    tag = arch.get("hours", {}).get(hk)
    tries = arch.get("tries", {}).get(hk, 0)
    return _frame_offset_s(tag, hour) <= _hour_tol_min(tries) * 60


def _radar_stats(arch: dict) -> str:
    """One-line histogram of how close each archived hour's frame sits to :00,
    plus how many are settled at their current tolerance and the deepest retry
    count. Logged either side of a backfill so a run's effect is visible."""
    hours = arch.get("hours", {})
    tries = arch.get("tries", {})
    edges = (4, 6, 9, 12)
    counts = [0] * (len(edges) + 1)
    settled = 0
    for hk, tag in hours.items():
        try:
            h = dt.datetime.strptime(hk, "%Y%m%dT%H").replace(
                tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        off_m = _frame_offset_s(tag, h) / 60.0
        for i, e in enumerate(edges):
            if off_m <= e:
                counts[i] += 1
                break
        else:
            counts[-1] += 1
        if off_m <= _hour_tol_min(tries.get(hk, 0)):
            settled += 1
    return ("radar archive %d h  |  <=4m:%d  <=6m:%d  <=9m:%d  <=12m:%d  "
            ">12m:%d  |  settled:%d  max-tries:%d" % (
                len(hours), counts[0], counts[1], counts[2], counts[3],
                counts[4], settled, max(tries.values(), default=0)))


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

    log.info("before: %s", _radar_stats(arch))
    rev = dt.datetime.now(dt.timezone.utc).strftime("%y%m%d%H%M")
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
            # This hour is not yet covered at its current tolerance, so this
            # pass is an attempt on it — count it whether or not a closer sweep
            # turns out to exist. Enough attempts widen the tolerance (see
            # _hour_tol_min), which is what lets an unbeatable frame settle
            # instead of being re-fetched every pass.
            if not force:
                tmap = arch.setdefault("tries", {})
                tmap[hour_key] = tmap.get(hour_key, 0) + 1
            # Never trade a good frame for a worse one. Whatever is stored for
            # this hour, only overwrite it with a sweep strictly closer to :00 —
            # so a pass that finds nothing nearer than the :35 already on file
            # leaves it alone, and the :05 that turns up next pass replaces it.
            cur_off = _frame_offset_s(arch.get("hours", {}).get(hour_key), target)
            avail = [abs((o["time"] - target).total_seconds()) for o in overlays
                     if abs((o["time"] - target).total_seconds()) <= 40 * 60]
            if not force and (not avail or min(avail) >= cur_off):
                continue
            canvas = Image.new("RGBA", (P.W, P.H), (0, 0, 0, 0))
            cold = _freezing_mask(target)
            used = []
            layers = []
            for code in ("EE", "LT2", "LT", "LV"):  # LV drawn last, on top
                cands = [o for o in overlays if o["code"] == code]
                if not cands:
                    continue
                best = min(cands,
                           key=lambda o: abs((o["time"] - target).total_seconds()))
                if abs((best["time"] - target).total_seconds()) > 40 * 60:
                    continue
                cls = _load_and_classify(best["url"], s, code, best)
                if cls is None:
                    continue
                # Warp first, clean second — see the note in radar_composite.
                grid = P.resample_mercator_image(cls, best["sw"], best["ne"])
                grid = _close_radial_spokes(_despeckle_intensity(
                    _remove_beam_lines(_remove_beams_polar(_despeckle(grid)))))
                layers.append((code, grid))
                used.append(best["time"])
            if not layers:
                log.info("backfill %s: no frames in archive", hour_key)
                continue
            act = active_feeds(layers)
            for code, grid in layers:
                canvas.alpha_composite(_recolor(grid, cold, code, act))
            canvas = _fade_edges(canvas)
            draw_borders(canvas)
            # Name the frame after the sweep it actually came from, not after
            # the hour we asked for. Sources arrive on their own ~10 min
            # cadence, so the nearest one to 19:00 can be 18:12 — and stamping
            # it 19:00 made the radar look up to 40 min later than it was,
            # which is the wrong direction to be wrong when the whole point is
            # deciding when rain arrives. The live path has always done this;
            # only the backfill rounded.
            stamp = max(used)
            if abs((stamp - target).total_seconds()) > 60:
                log.info("backfill %s: nearest sweep %s (%+d min)", hour_key,
                         stamp.strftime("%H:%M"),
                         round((stamp - target).total_seconds() / 60))
            vtag = stamp.strftime("%Y%m%dT%H%M")
            # The nearest sweep this composite actually landed on (stamp is the
            # latest feed used) may still be no closer than what is already
            # stored — the pre-render check bounds the best case, this is the
            # real one. Don't downgrade or needlessly re-store an equal frame.
            if not force and _frame_offset_s(vtag, target) >= cur_off:
                continue
            buf = io.BytesIO()
            canvas.save(buf, "PNG", optimize=True)
            s3.put_object(Bucket=config.R2_BUCKET,
                          Key=f"maps/radar/frames3059/{vtag}.png",
                          Body=buf.getvalue(), ContentType="image/png",
                          CacheControl="public, max-age=604800")
            arch["hours"][hour_key] = vtag
            # A re-render leaves the FILENAME alone — it is the sweep time —
            # so nothing in the URL tells a browser the bytes changed. The page
            # used to paper over that by stamping every frame with the live
            # manifest's clock, which changes hourly and therefore threw away
            # the whole archive's cache once an hour. That is what made the
            # slider slow enough for the picture to run behind its caption.
            # A per-hour revision fixes both ends: the URL is stable until the
            # hour is actually redrawn, and changes the moment it is.
            arch.setdefault("rev", {})[hour_key] = rev
            done += 1
        # persist index incrementally so an interrupted run keeps progress
        s3.put_object(Bucket=config.R2_BUCKET, Key="maps/radar/archive.json",
                      Body=json.dumps(arch).encode(),
                      ContentType="application/json",
                      CacheControl="public, max-age=300")
    log.info("radar backfill: %d hourly composites added (archive now %d h)",
             done, len(arch["hours"]))
    log.info("after:  %s", _radar_stats(arch))


def radar_render_one(ts_iso: str) -> None:
    """Force re-render one hour's composite with the current cleaning and store
    it, updating only that hour's archive entry. For checking a fix on a single
    frame before deciding whether to re-render the rest of the archive."""
    s = session()
    s3 = r2_client()
    from . import proj3059 as P
    try:
        arch = json.loads(s3.get_object(
            Bucket=config.R2_BUCKET, Key="maps/radar/archive.json"
        )["Body"].read())
    except Exception:
        arch = {"path": "maps/radar/frames3059", "hours": {}}
    target = dt.datetime.fromisoformat(ts_iso)
    if target.tzinfo is None:
        target = target.replace(tzinfo=dt.timezone.utc)
    target = target.replace(minute=0, second=0, microsecond=0)
    hour_key = target.strftime("%Y%m%dT%H")
    rev = dt.datetime.now(dt.timezone.utc).strftime("%y%m%d%H%M")
    overlays = fetch_overlays_window(target - dt.timedelta(minutes=40),
                                     target + dt.timedelta(minutes=40))
    canvas = Image.new("RGBA", (P.W, P.H), (0, 0, 0, 0))
    cold = _freezing_mask(target)
    used = []
    layers = []
    for code in ("EE", "LT2", "LT", "LV"):  # LV drawn last, on top
        cands = [o for o in overlays if o["code"] == code]
        if not cands:
            continue
        best = min(cands, key=lambda o: abs((o["time"] - target).total_seconds()))
        if abs((best["time"] - target).total_seconds()) > 40 * 60:
            continue
        cls = _load_and_classify(best["url"], s, code, best)
        if cls is None:
            continue
        grid = P.resample_mercator_image(cls, best["sw"], best["ne"])
        grid = _close_radial_spokes(_despeckle_intensity(
            _remove_beam_lines(_remove_beams_polar(_despeckle(grid)))))
        layers.append((code, grid))
        used.append(best["time"])
    if not used:
        log.info("render-one %s: no source frames in window", hour_key)
        return
    act = active_feeds(layers)
    for code, grid in layers:
        canvas.alpha_composite(_recolor(grid, cold, code, act))
    canvas = _fade_edges(canvas)
    draw_borders(canvas)
    stamp = max(used)
    vtag = stamp.strftime("%Y%m%dT%H%M")
    buf = io.BytesIO()
    canvas.save(buf, "PNG", optimize=True)
    s3.put_object(Bucket=config.R2_BUCKET,
                  Key=f"maps/radar/frames3059/{vtag}.png",
                  Body=buf.getvalue(), ContentType="image/png",
                  CacheControl="public, max-age=604800")
    arch.setdefault("hours", {})[hour_key] = vtag
    arch.setdefault("rev", {})[hour_key] = rev
    s3.put_object(Bucket=config.R2_BUCKET, Key="maps/radar/archive.json",
                  Body=json.dumps(arch).encode(),
                  ContentType="application/json",
                  CacheControl="public, max-age=300")
    log.info("render-one %s: stored %s (nearest sweep %s, %+d min) — archive "
             "updated for this hour only", hour_key, vtag,
             stamp.strftime("%H:%M"),
             round((stamp - target).total_seconds() / 60))
