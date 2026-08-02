"""What each radar feed's colours are actually worth, in millimetres per hour.

The three national feeds are not on one scale and nothing in the imagery says
so. Left to hue rules the map is wrong twice over: within a feed six steps of
one ramp collapse into a single class, and across feeds the same rain changes
colour at the border. Both were measured — see analysis/README_radar_calibration.md.

This job supplies the missing number: the rate on each step of each feed's own
ramp. Two halves, and only one of them needs gauges.

  ORDER comes from the imagery. Rank each feed's palette steps by which
  colours border which, over a few hundred archived frames, and one
  unambiguous chain falls out per feed — Latvia's fifteen steps run
  #0000c7 -> #0034fe -> ... -> #800000 and nothing else is consistent with
  who touches whom. That chain is fixed in wxfusion/assets/radar_rates.json
  and this job does not re-derive it.

  RATE comes from the gauges. A gauge sits under one radar at short range;
  the obvious alternative — calibrating feeds against each other where they
  overlap — was tried and measured, and it fails on geometry: the only place
  two of these radars see the same sky is a border strip where both are at
  150-200 km, looking through the top of the weather. 49,952 pixel pairs
  produced a flat line.

Method notes that matter:

  * an hourly gauge total against one instantaneous radar frame is mostly
    noise, so every frame in the hour is sampled and the colour that appears
    most often over the station wins.
  * 5 km around the station, not one pixel: a gauge is a point, a radar pixel
    is a kilometre, and an hour of rain does not fall on the square kilometre
    it started over.
  * the MEAN hourly total, not the median. Under any light-rain colour half
    the hours record nothing at all — the beam overshot, or it evaporated on
    the way down — so the median is 0.0 for colours that plainly carry rain.
    A 10% top-trimmed mean was tried and is worse in the other direction: it
    puts the third step of Estonia's ramp at 0.01 mm/h.
  * only steps with real support are believed. A step with eight or more
    gauge-hours behind it becomes a knot at its measured mean; everything
    between knots is interpolated geometrically and the result is forced
    monotone along the chain. So the two steps of Latvia's scale that have
    four gauge-hours between them cannot invert the twelve that have hundreds.
  * the LAST step of each ramp is pinned at 20 mm/h whether or not anything
    measured it, and it never will be: these are the colours the products
    reserve for extreme rain, and a gauge has to be standing under one to
    measure it. Extrapolating each feed's own slope instead put Estonia's
    magenta at 2 mm/h — the same class as its yellow. For a flight decision,
    under-reading a downpour is the expensive direction to be wrong in.

Pairs accumulate in R2 across runs. The bundled table is what ships; this job
only ever replaces it with one built from more weather.
"""
from __future__ import annotations

import collections
import datetime as dt
import io
import json
import logging
import pathlib

import numpy as np
from PIL import Image

from . import config
from .http import session
from .scrape_maps import (LT_MAXSIZE, RATES_ASSET, fetch_overlays_window,
                          r2_client)
from .maps_meps import RAIN_LEVELS

log = logging.getLogger(__name__)

KEY = "maps/radar_cal/pairs.json"
SCALES_KEY = "maps/radar_cal/scales.json"
# Which feed covers which network's gauges. A Latvian gauge calibrates the
# Latvian radar and nothing else — that is the whole point of using gauges.
FEED_FOR = {"lvgmc": "LV", "ee": "EE", "lt": "LT2"}
BOX = 2            # +/- pixels around the station, so 5 km across
SNAP_MAX = 40.0    # how far a pixel may sit from a ramp step and still count
FRAMES_PER_HOUR = 6

# Gauge-hours a step needs before its own mean is used as a knot rather than
# interpolated from its neighbours. Eight is low, and deliberately: over a
# season the well-travelled steps carry hundreds, and the ones that never get
# there are the heavy end, which cannot be measured by waiting either.
MIN_ANCHOR_N = 8
# What the top step of a ramp is worth. See the module docstring.
TOP_MMH = 20.0
FLOOR_MMH = 0.06


def ramps() -> dict:
    """{feed: (palette Nx3 float, hexes)} in ramp order, from the asset."""
    d = json.loads(pathlib.Path(RATES_ASSET).read_text())
    return {f: (np.array([r["rgb"] for r in rows], np.float32),
                [r["hex"] for r in rows])
            for f, rows in d["scales"].items()}


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


def _sample(grid, x, y, pal):
    """Commonest ramp step in the box, or None if nothing is painted there."""
    win = grid[max(0, y - BOX):y + BOX + 1, max(0, x - BOX):x + BOX + 1]
    rgb = win[..., :3].astype(np.float32)
    ok = (win[..., 3] > 60) & ((rgb.max(2) - rgb.min(2)) > 25)
    if not ok.any():
        return None
    v = rgb[ok]
    d = np.linalg.norm(v[:, None, :] - pal[None, :, :], axis=2)
    i = d.argmin(1)[d.min(1) <= SNAP_MAX]
    if not len(i):
        return None
    return int(np.bincount(i).argmax())


def collect(conn, hours_back: int = 168, min_wet: int = 2) -> dict:
    """Pair every gauge-hour in a raining hour with what its radar painted."""
    from . import proj3059 as P

    s = session()
    pal = ramps()
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
            if o["code"] in pal:
                by[o["code"]].append(o)
        votes = collections.defaultdict(collections.Counter)
        for code, lst in by.items():
            for o in sorted(lst, key=lambda o: o["time"])[-FRAMES_PER_HOUR:]:
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
                    votes[(code, la, lo)][_sample(g, x, y, pal[code][0])] += 1
        for src, la, lo, mm in obs:
            v = votes.get((FEED_FOR[src], la, lo))
            if not v:
                continue
            step, _ = v.most_common(1)[0]
            pairs[f"{FEED_FOR[src]}|{'clear' if step is None else step}"].append(mm)
        log.info("calibrate: %s done", hr)
    return dict(pairs)


def merge_and_store(new: dict) -> dict:
    """Append to whatever earlier runs collected. The table only gets better
    with more weather, and re-collecting a season every time would be absurd."""
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
        feed, step = k.split("|", 1)
        a = np.array(v, float)
        summary.setdefault(feed, {})[step] = {
            "n": len(a), "mean": round(float(a.mean()), 3),
            "p50": round(float(np.median(a)), 3),
            "wet_frac": round(float((a > 0.1).mean()), 3)}
    body = json.dumps({"pairs": old, "summary": summary,
                       "updated": dt.datetime.now(dt.timezone.utc).isoformat()})
    s3.put_object(Bucket=config.R2_BUCKET, Key=KEY, Body=body.encode(),
                  ContentType="application/json",
                  CacheControl="public, max-age=300")
    log.info("calibrate: %d gauge-hours held",
             sum(len(v) for v in old.values()))
    return summary


def _isotonic(y: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Pool-adjacent-violators: the nearest non-decreasing sequence."""
    stack: list[list[float]] = []
    for val, wt in zip(map(float, y), map(float, w)):
        stack.append([val, wt, 1])
        while len(stack) > 1 and stack[-2][0] > stack[-1][0]:
            b = stack.pop(); a = stack.pop()
            tw = a[1] + b[1]
            stack.append([(a[0] * a[1] + b[0] * b[1]) / tw, tw, a[2] + b[2]])
    out: list[float] = []
    for v in stack:
        out += [v[0]] * int(v[2])
    return np.array(out)


def fit(summary: dict | None = None) -> dict:
    """Ramp step -> mm/h for every feed, written to R2 in the asset's shape."""
    s3 = r2_client()
    if summary is None:
        summary = json.loads(s3.get_object(Bucket=config.R2_BUCKET,
                                           Key=KEY)["Body"].read())["summary"]
    pal = ramps()
    out, coverage = {}, {}
    for feed, (rgb, hexes) in pal.items():
        rows = summary.get(feed, {})
        n = np.array([rows.get(str(i), {}).get("n", 0) for i in range(len(hexes))], float)
        y = np.array([rows.get(str(i), {}).get("mean", np.nan) for i in range(len(hexes))], float)
        y = np.where(np.isnan(y), np.nan, np.maximum(y, FLOOR_MMH))
        idx = np.arange(len(hexes), dtype=float)
        anchors = [i for i in range(len(hexes))
                   if n[i] >= MIN_ANCHOR_N and not np.isnan(y[i])]
        if len(anchors) < 3:
            log.info("calibrate: %s has %d anchored steps, keeping the "
                     "bundled table", feed, len(anchors))
            continue
        knots = [(i, float(y[i])) for i in anchors]
        last = len(hexes) - 1
        if knots[-1][0] != last:
            knots.append((last, TOP_MMH))
        if knots[0][0] != 0:
            # extend the first measured pair's ratio down to step 0
            (i1, v1), (i2, v2) = knots[0], knots[1]
            knots.insert(0, (0, v1 / ((v2 / v1) ** (1 / (i2 - i1))) ** i1))
        rate = np.exp(np.interp(idx, [k for k, _ in knots],
                                np.log([v for _, v in knots])))
        rate = _isotonic(rate, np.where(n >= MIN_ANCHOR_N, n, 1.0))
        out[feed] = [{"rgb": [int(v) for v in rgb[i]], "hex": hexes[i],
                      "mmh": round(float(rate[i]), 3), "n": int(n[i])}
                     for i in range(len(hexes))]
        coverage[feed] = (f"{len(anchors)}/{len(hexes)} steps anchored, "
                          f"{int(n.sum())} gauge-hours")
    body = {"note": "fitted by the radar-calibrate job; overrides the bundled table",
            "levels": RAIN_LEVELS[:-1], "scales": out, "coverage": coverage,
            "updated": dt.datetime.now(dt.timezone.utc).isoformat()}
    s3.put_object(Bucket=config.R2_BUCKET, Key=SCALES_KEY,
                  Body=json.dumps(body).encode(),
                  ContentType="application/json",
                  CacheControl="public, max-age=600")
    for k, v in coverage.items():
        log.info("calibrate: %s -> %s", k, v)
    return body
