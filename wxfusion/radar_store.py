"""Keep the radar sources, render the pictures from them.

The problem this solves: every time the radar drawing changed — the colour
ramp, the beam removal, the coverage fade, the freezing palette, the weak-echo
range limit, all of which changed in the last week — re-rendering meant
re-fetching hundreds of overlays from meteolapa. Slow, rude to them, and it
made a styling change feel like an expensive decision.

MEPS already works the right way: keep the model grid for a year, keep the
pictures for a fortnight, and any hour inside the year re-renders in a second
with no call upstream. This does the same for radar.

  maps/radar_src/YYYY/MM/DD/<YYYYMMDDHHMM><CODE>.png   as served, 365 days
  maps/radar_src/YYYY/MM/DD/index.json                 what is there, with bounds
  maps/radar/frames3059/<stamp>.png                    rendered, rolling 21 days

Sizes, measured on live frames rather than guessed. As served the three
sources are 248 kB an hour together, Lithuania 204 kB of that. Stored as
palette PNG they are 98 kB — 0.88 GB for a year rather than 2.2 — and for
Estonia and Latvia that is bit-for-bit the same image. See REENCODE for what
is and is not lost. The rendered composites are 31.5 kB an hour, and only a
three weeks of them is kept now.

Storing the sources rather than the classified grids is deliberate. The
classifier is the part that keeps changing — the whole gauge calibration
exists to change it — and a stored class grid could not be reclassified. A
palette PNG still cannot: it is the same image with a smaller colour table.
"""
from __future__ import annotations

import collections
import datetime as dt
import io
import json
import logging

from PIL import Image

from . import config
from .http import session
from .scrape_maps import (_classify_intensity, _despeckle, _despeckle_intensity,
                          _prepare_source,
                          _fade_edges,
                          _freezing_mask, _recolor, _remove_beam_lines,
                          _remove_beams_polar, LT_MAXSIZE, draw_borders,
                          fetch_overlays_window, overlay_bytes, r2_client)

log = logging.getLogger(__name__)

SRC_PREFIX = "maps/radar_src"
FRAME_PREFIX = "maps/radar/frames3059"
ARCHIVE_KEY = "maps/radar/archive.json"
SRC_KEEP_DAYS = 365
# Re-encode the originals as palette PNG before storing. Measured on live
# frames: 248 kB an hour becomes 98 kB, so a year of originals is 0.9 GB
# instead of 2.2.
#
# Nothing is lost for Estonia (8 distinct RGBA values in a frame) or Latvia
# (15) — the palette is built from the exact colours and the round trip is
# bit-for-bit. Lithuania serves 6,561 colours, nearly all of them compression
# noise around a much smaller real ramp, and gets median-cut to 255: 99.47% of
# echo pixels classify identically and 90% of the rest move by exactly one
# class, which is a boundary pixel changing its mind.
#
# Tried and rejected: plain grayscale, which destroys the classification
# outright (0% agreement) because different hues share a luminance —
# Lithuania's cyan is 156 and its bright green 167. And picking Lithuania's
# palette by frequency instead of median-cut, which scored WORSE (97.81%)
# even though the top 256 colours cover 97.2% of pixels. On a radar image the
# rare colours are the heavy cores, and those are the ones worth keeping.
REENCODE = True
# Long enough to cover every week the page can navigate to. The arrows
# reach two whole weeks back, and a Monday-aligned week two back starts
# up to 20 days behind today, so a fortnight left the oldest reachable
# week with a meteogram and no maps beside it.
FRAME_KEEP_DAYS = 21
# How far a sweep may be from the hour it is filed under. The sources run on
# their own ~10 minute cadence and do not line up with the clock.
NEAR_MIN = 40


def _to_palette(body: bytes) -> tuple[bytes, str]:
    """Original PNG bytes -> palette PNG bytes, and which route was taken."""
    import numpy as np

    im = Image.open(io.BytesIO(body)).convert("RGBA")
    a = np.array(im)
    flat = a.reshape(-1, 4)
    uniq, inv = np.unique(flat, axis=0, return_inverse=True)
    if len(uniq) <= 256:
        idx = inv.reshape(a.shape[:2]).astype(np.uint8)
        pal = uniq
        how = "exact"
    else:
        q = im.convert("RGB").quantize(colors=255,
                                       method=Image.Quantize.MEDIANCUT,
                                       dither=Image.Dither.NONE)
        qp = np.array(q.getpalette()[:255 * 3], np.uint8).reshape(-1, 3)
        # index 255 is the transparent one, on the same >60 test the
        # classifier uses to decide a pixel is painted at all
        idx = np.where(a[..., 3] > 60, np.array(q), 255).astype(np.uint8)
        pal = np.vstack([np.c_[qp, np.full(len(qp), 255, np.uint8)],
                         np.array([[0, 0, 0, 0]], np.uint8)])
        how = "medcut255"
    p = Image.fromarray(idx, "P")
    p.putpalette(pal[:, :3].astype(np.uint8).ravel().tolist())
    out = io.BytesIO()
    p.save(out, "PNG", optimize=True,
           transparency=bytes(pal[:, 3].astype(np.uint8)))
    return out.getvalue(), how


def _day_prefix(t: dt.datetime) -> str:
    return f"{SRC_PREFIX}/{t:%Y/%m/%d}"


def _src_key(o: dict) -> str:
    return f"{_day_prefix(o['time'])}/{o['time']:%Y%m%d%H%M}{o['code']}.png"


def _load_index(s3, day: dt.date) -> dict:
    key = f"{SRC_PREFIX}/{day:%Y/%m/%d}/index.json"
    try:
        return json.loads(s3.get_object(Bucket=config.R2_BUCKET,
                                        Key=key)["Body"].read())
    except Exception:
        return {"frames": []}


def _save_index(s3, day: dt.date, idx: dict) -> None:
    s3.put_object(Bucket=config.R2_BUCKET,
                  Key=f"{SRC_PREFIX}/{day:%Y/%m/%d}/index.json",
                  Body=json.dumps(idx).encode(),
                  ContentType="application/json",
                  CacheControl="public, max-age=300")


def store_sources(hours_back: int = 6, per_hour: int = 1) -> int:
    """Pull the overlays for a window and keep them exactly as served.

    The bounds come with the overlay listing, not with the image, so they are
    written into the day index alongside it. Without them a stored PNG is an
    unplaceable rectangle.
    """
    s, s3 = session(), r2_client()
    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    stored = 0
    # One listing for the whole window, not one per hour. The endpoint takes an
    # epoch range and hands back everything inside it, so asking seven times
    # for seven overlapping hours was seven requests where one does; the
    # per-hour buckets are a groupby over the single answer.
    try:
        allov = fetch_overlays_window(
            now - dt.timedelta(hours=hours_back, minutes=NEAR_MIN),
            now + dt.timedelta(minutes=NEAR_MIN))
    except Exception:
        log.exception("store: overlay listing failed")
        return 0
    for back in range(hours_back, -1, -1):
        hour = now - dt.timedelta(hours=back)
        by = collections.defaultdict(list)
        for o in allov:
            if abs((o["time"] - hour).total_seconds()) <= NEAR_MIN * 60:
                by[o["code"]].append(o)
        idx = _load_index(s3, hour.date())
        have = {f["key"] for f in idx["frames"]}
        dirty = False
        for code, frames in by.items():
            frames.sort(key=lambda o: abs((o["time"] - hour).total_seconds()))
            for o in frames[:per_hour]:
                key = _src_key(o)
                if key in have:
                    continue
                try:
                    # Through the shared cache: the composite step in the same
                    # tick has usually just fetched this exact frame.
                    raw = overlay_bytes(o["url"], s)
                    if raw is None:
                        continue
                except Exception:
                    log.exception("store: fetch failed %s", o["url"])
                    continue
                body, how = raw, "as-served"
                if REENCODE:
                    try:
                        body, how = _to_palette(raw)
                    except Exception:
                        log.exception("store: re-encode failed for %s, "
                                      "keeping the original", key)
                        body, how = raw, "as-served"
                s3.put_object(Bucket=config.R2_BUCKET, Key=key, Body=body,
                              ContentType="image/png",
                              CacheControl="public, max-age=31536000, immutable")
                idx["frames"].append({
                    "key": key, "code": code, "enc": how,
                    "bytes": len(body), "src_bytes": len(raw),
                    "time": o["time"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "sw": list(o["sw"]), "ne": list(o["ne"]),
                })
                have.add(key)
                stored += 1
                dirty = True
        if dirty:
            idx["frames"].sort(key=lambda f: f["time"])
            _save_index(s3, hour.date(), idx)
    log.info("radar store: %d new source overlays", stored)
    return stored


def _classify_stored(s3, frame: dict):
    """A stored overlay -> intensity classes, exactly as the live path does."""
    body = s3.get_object(Bucket=config.R2_BUCKET, Key=frame["key"])["Body"].read()
    img = Image.open(io.BytesIO(body)).convert("RGBA")
    if max(img.size) > LT_MAXSIZE:
        sc = LT_MAXSIZE / max(img.size)
        img = img.resize((int(img.width * sc), int(img.height * sc)), Image.NEAREST)
    import numpy as np
    # Beams come off in the radar's own frame, before this is reprojected.
    return _prepare_source(np.array(img), frame["code"],
                           {"sw": tuple(frame["sw"]), "ne": tuple(frame["ne"])})


def render_stored(hours_back: int = 336, force: bool = False) -> int:
    """Build hourly composites from the stored originals. No upstream traffic.

    This is the function a styling change should call. It reads only R2, so
    re-rendering a fortnight costs seconds and nothing on meteolapa's side.
    """
    import numpy as np

    from . import proj3059 as P

    s3 = r2_client()
    try:
        arch = json.loads(s3.get_object(Bucket=config.R2_BUCKET,
                                        Key=ARCHIVE_KEY)["Body"].read())
    except Exception:
        arch = {"path": FRAME_PREFIX, "hours": {}}
    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    rev = dt.datetime.now(dt.timezone.utc).strftime("%y%m%d%H%M")
    days = {}
    done = 0
    for back in range(hours_back, -1, -1):
        hour = now - dt.timedelta(hours=back)
        hour_key = hour.strftime("%Y%m%dT%H")
        if not force and hour_key in arch["hours"]:
            continue
        d = hour.date()
        if d not in days:
            days[d] = _load_index(s3, d)
        # Yesterday's index too: a sweep at 23:52 belongs to today's 00:00.
        prev = (hour - dt.timedelta(hours=1)).date()
        if prev not in days:
            days[prev] = _load_index(s3, prev)
        pool = days[d]["frames"] + days[prev]["frames"]
        best = {}
        for f in pool:
            t = dt.datetime.strptime(f["time"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=dt.timezone.utc)
            gap = abs((t - hour).total_seconds())
            if gap > NEAR_MIN * 60:
                continue
            cur = best.get(f["code"])
            if cur is None or gap < cur[0]:
                best[f["code"]] = (gap, t, f)
        if not best:
            continue
        canvas = Image.new("RGBA", (P.W, P.H), (0, 0, 0, 0))
        cold = _freezing_mask(hour)
        used = []
        # LV last so it lands on top, as in the live composite.
        for code in ("EE", "LT2", "LT", "LV"):
            if code not in best:
                continue
            _, t, f = best[code]
            try:
                cls = _classify_stored(s3, f)
            except Exception:
                log.exception("render: %s unreadable", f["key"])
                continue
            grid = P.resample_mercator_image(cls, tuple(f["sw"]), tuple(f["ne"]))
            grid = _despeckle_intensity(
                _remove_beam_lines(_remove_beams_polar(_despeckle(grid))))
            canvas.alpha_composite(_recolor(grid, cold, code))
            used.append(t)
        if not used:
            continue
        canvas = _fade_edges(canvas)
        draw_borders(canvas)
        # Named after the sweep it came from, not the hour it is filed under.
        stamp = max(used)
        vtag = stamp.strftime("%Y%m%dT%H%M")
        buf = io.BytesIO()
        canvas.save(buf, "PNG", optimize=True)
        s3.put_object(Bucket=config.R2_BUCKET, Key=f"{FRAME_PREFIX}/{vtag}.png",
                      Body=buf.getvalue(), ContentType="image/png",
                      CacheControl="public, max-age=604800")
        arch["hours"][hour_key] = vtag
        arch.setdefault("rev", {})[hour_key] = rev   # see the note in radar_backfill
        done += 1
    s3.put_object(Bucket=config.R2_BUCKET, Key=ARCHIVE_KEY,
                  Body=json.dumps(arch).encode(),
                  ContentType="application/json",
                  CacheControl="public, max-age=300")
    log.info("radar render: %d composites from stored sources", done)
    return done


def prune(src_days: int = SRC_KEEP_DAYS, frame_days: int = FRAME_KEEP_DAYS) -> dict:
    """Originals for a year, pictures for a fortnight.

    The pictures are the disposable half now — anything inside the year
    re-renders from what is kept, so holding old renders would only mean
    holding renders made with old rules.
    """
    s3 = r2_client()
    now = dt.datetime.now(dt.timezone.utc)
    out = {"sources": 0, "frames": 0}

    def _sweep(prefix, older_than, stamp_of):
        n = 0
        token = None
        while True:
            kw = {"Bucket": config.R2_BUCKET, "Prefix": prefix, "MaxKeys": 1000}
            if token:
                kw["ContinuationToken"] = token
            page = s3.list_objects_v2(**kw)
            gone = []
            for obj in page.get("Contents", []):
                t = stamp_of(obj["Key"])
                if t is not None and t < older_than:
                    gone.append({"Key": obj["Key"]})
            for i in range(0, len(gone), 1000):
                s3.delete_objects(Bucket=config.R2_BUCKET,
                                  Delete={"Objects": gone[i:i + 1000]})
            n += len(gone)
            if not page.get("IsTruncated"):
                return n
            token = page.get("NextContinuationToken")

    import re

    def src_stamp(key):
        m = re.search(r"/(\d{12})[A-Z0-9]+\.png$", key)
        return (dt.datetime.strptime(m.group(1), "%Y%m%d%H%M")
                .replace(tzinfo=dt.timezone.utc)) if m else None

    def frame_stamp(key):
        m = re.search(r"/(\d{8}T\d{4})\.png$", key)
        return (dt.datetime.strptime(m.group(1), "%Y%m%dT%H%M")
                .replace(tzinfo=dt.timezone.utc)) if m else None

    out["sources"] = _sweep(SRC_PREFIX + "/",
                            now - dt.timedelta(days=src_days), src_stamp)
    out["frames"] = _sweep(FRAME_PREFIX + "/",
                           now - dt.timedelta(days=frame_days), frame_stamp)
    log.info("radar prune: %d originals, %d rendered frames", *out.values())
    return out
