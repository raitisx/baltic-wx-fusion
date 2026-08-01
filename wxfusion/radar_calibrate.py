"""What each radar feed's colours are actually worth, in millimetres per hour.

The three national feeds are not on one scale and nothing in the imagery says
so. meteolapa publish a legend, and its ORDER is right — ranking each feed's
colours by where they sit on its own ramp correlates with measured rain at
Spearman 0.77 (LV), 0.93 (LT) and 0.89 (EE). Its RATES are not: the legend
puts Lithuanian cyan at 8.7 mm/h, and the gauges under it recorded 0.2.

So: sequence from the legend, magnitude from the gauges. This job collects the
magnitude half.

Method, and why each part of it:

  * a gauge sits under one radar at short range. The obvious alternative —
    calibrating feeds against each other where they overlap — was tried and
    measured, and it fails on geometry: the only place two of these radars see
    the same sky is a border strip where both are at 150-200 km, looking
    through the top of the weather. 49,952 pixel pairs produced a flat line.
  * an hourly gauge total against one instantaneous radar frame is mostly
    noise, so every frame in the hour is sampled and the colour that appears
    most often over the station wins.
  * 5 km around the station, not one pixel: a gauge is a point, a radar pixel
    is a kilometre, and an hour of rain does not fall on the square kilometre
    it started over.

Pairs accumulate in R2 across runs (maps/radar_cal/pairs.json). One run over a
week of weather gets the ordering and the light end; the heavy end needs heavy
rain to have fallen over a gauge, which is a matter of waiting.
"""
from __future__ import annotations

import collections
import datetime as dt
import io
import json
import logging

import numpy as np
from PIL import Image

from . import config
from .http import session
from .scrape_maps import LT_MAXSIZE, fetch_overlays_window, r2_client

log = logging.getLogger(__name__)

KEY = "maps/radar_cal/pairs.json"
# Which feed covers which network's gauges. A Latvian gauge calibrates the
# Latvian radar and nothing else — that is the whole point of using gauges.
FEED_FOR = {"lvgmc": "LV", "ee": "EE", "lt": "LT2"}
BOX = 2          # +/- pixels around the station, so 5 km across
QUANT = 16       # colour bucket; the Lithuanian source is heavily compressed


def _fetch(url: str, s) -> np.ndarray | None:
    r = s.get(url, allow_redirects=True, timeout=90)
    if r.status_code != 200:
        return None
    img = Image.open(io.BytesIO(r.content)).convert("RGBA")
    if max(img.size) > LT_MAXSIZE:
        sc = LT_MAXSIZE / max(img.size)
        img = img.resize((int(img.width * sc), int(img.height * sc)), Image.NEAREST)
    return np.array(img)


def _warp(arr, sw, ne):
    from . import proj3059 as P
    out = np.zeros((P.H, P.W, 4), np.uint8)
    for c in range(4):
        out[..., c] = P.resample_mercator_image(arr[..., c], sw, ne)
    return out


def _sample(grid, x, y):
    """The commonest echo colour in the box, or None if nothing is painted."""
    win = grid[max(0, y - BOX):y + BOX + 1, max(0, x - BOX):x + BOX + 1]
    rgb = win[..., :3].astype(int)
    ok = (win[..., 3] > 60) & ((rgb.max(2) - rgb.min(2)) > 25)
    if not ok.any():
        return None
    q = (rgb[ok] // QUANT) * QUANT + QUANT // 2
    uniq, cnt = np.unique(q, axis=0, return_counts=True)
    return tuple(int(v) for v in uniq[cnt.argmax()])


def collect(conn, hours_back: int = 168, min_wet: int = 5) -> dict:
    """Pair every raining gauge-hour with what its own radar was painting."""
    from . import proj3059 as P

    s = session()
    with conn.cursor() as cur:
        cur.execute("""
            select date_trunc('hour', o.time) as hr, o.source, p.lat, p.lon, o.prcp_1h
            from wx.observations_h o
            join wx.points p on p.kind = o.source and p.station_id = o.station_id
            where o.prcp_1h is not null and o.source in ('lvgmc','ee','lt')
              and p.lat is not null
              and o.time > now() - make_interval(hours => %s)
              and date_trunc('hour', o.time) in (
                  select date_trunc('hour', time) from wx.observations_h
                  where prcp_1h > 0.1 and source in ('lvgmc','ee','lt')
                    and time > now() - make_interval(hours => %s)
                  group by 1 having count(*) >= %s)
            """, (hours_back, hours_back, min_wet))
        rows = cur.fetchall()
    byhr = collections.defaultdict(list)
    for hr, src, la, lo, mm in rows:
        byhr[hr].append((src, float(la), float(lo), float(mm)))
    log.info("calibrate: %d gauge readings over %d hours", len(rows), len(byhr))

    pairs = collections.defaultdict(list)
    for hr, obs in sorted(byhr.items()):
        # The gauge total is for the hour ENDING at its stamp, so sample the
        # radar across that hour rather than at either edge of it.
        try:
            ov = fetch_overlays_window(hr - dt.timedelta(minutes=60), hr)
        except Exception:
            log.exception("calibrate: overlays failed for %s", hr)
            continue
        by = collections.defaultdict(list)
        for o in ov:
            by[o["code"]].append(o)
        votes = collections.defaultdict(collections.Counter)
        for code in set(FEED_FOR.values()):
            for o in sorted(by.get(code, []), key=lambda o: o["time"])[-6:]:
                a = _fetch(o["url"], s)
                if a is None:
                    continue
                g = _warp(a, o["sw"], o["ne"])
                for src, la, lo, mm in obs:
                    if FEED_FOR[src] != code:
                        continue
                    px, py = P.to_px([la], [lo])
                    x, y = int(round(px[0])), int(round(py[0]))
                    if not (0 <= x < P.W and 0 <= y < P.H):
                        continue
                    votes[(code, la, lo)][_sample(g, x, y)] += 1
        for src, la, lo, mm in obs:
            v = votes.get((FEED_FOR[src], la, lo))
            if not v:
                continue
            colour, _ = v.most_common(1)[0]
            pairs[f"{FEED_FOR[src]}|{'clear' if colour is None else '%d,%d,%d' % colour}"].append(mm)
        log.info("calibrate: %s done", hr)
    return {k: v for k, v in pairs.items()}


def merge_and_store(new: dict) -> dict:
    """Append to whatever earlier runs collected. The tables only get better
    with more weather, and re-collecting a year every time would be absurd."""
    s3 = r2_client()
    try:
        old = json.loads(s3.get_object(Bucket=config.R2_BUCKET,
                                       Key=KEY)["Body"].read())["pairs"]
    except Exception:
        old = {}
    for k, v in new.items():
        old.setdefault(k, []).extend(v)
    summary = {}
    for k, v in old.items():
        feed, col = k.split("|", 1)
        summary.setdefault(feed, []).append({
            "rgb": col, "n": len(v), "mean": round(float(np.mean(v)), 3),
            "p50": round(float(np.median(v)), 3),
            "wet_frac": round(float(np.mean(np.array(v) > 0.1)), 3)})
    for f in summary:
        summary[f].sort(key=lambda e: e["mean"])
    body = json.dumps({"pairs": old, "summary": summary,
                       "updated": dt.datetime.now(dt.timezone.utc).isoformat()})
    s3.put_object(Bucket=config.R2_BUCKET, Key=KEY, Body=body.encode(),
                  ContentType="application/json",
                  CacheControl="public, max-age=300")
    log.info("calibrate: %d colour buckets stored", sum(len(v) for v in summary.values()))
    return summary


# --- turning the pairs into something the classifier can use ----------------
# A colour is only trusted once enough gauge-hours have sat under it. Below
# this it keeps whatever the hue rules said, so the table can only ever add
# knowledge — there is no state of the world in which collecting more data
# makes the map worse.
MIN_N = 40
SCALES_KEY = "maps/radar_cal/scales.json"


def fit(summary: dict | None = None) -> dict:
    """Colour -> mm/h for every colour with enough gauge-hours behind it.

    The mean, not the median: half the hours under a light-rain colour recorded
    nothing at all, so the median is zero for colours that clearly do carry
    rain. What matters for "how much fell" is the mean.
    """
    s3 = r2_client()
    if summary is None:
        summary = json.loads(s3.get_object(Bucket=config.R2_BUCKET,
                                           Key=KEY)["Body"].read())["summary"]
    out, counts = {}, {}
    for feed, rows in summary.items():
        table = []
        for e in rows:
            if e["rgb"] == "clear" or e["n"] < MIN_N:
                continue
            rgb = [int(v) for v in e["rgb"].split(",")]
            table.append({"rgb": rgb, "mmh": round(float(e["mean"]), 3),
                          "n": int(e["n"])})
        if table:
            out[feed] = table
        counts[feed] = (len(table), len(rows))
    body = {"scales": out, "min_n": MIN_N,
            "coverage": {k: f"{a}/{b} colours have {MIN_N}+ gauge-hours"
                         for k, (a, b) in counts.items()},
            "updated": dt.datetime.now(dt.timezone.utc).isoformat()}
    s3.put_object(Bucket=config.R2_BUCKET, Key=SCALES_KEY,
                  Body=json.dumps(body).encode(),
                  ContentType="application/json",
                  CacheControl="public, max-age=600")
    for k, (a, b) in counts.items():
        log.info("calibrate: %s -> %d of %d colours qualified", k, a, b)
    return body
