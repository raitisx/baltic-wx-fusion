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
from .scrape_maps import (_classify_intensity, _close_radial_spokes,
                          _despeckle, _despeckle_intensity,
                          _prepare_source,
                          _fade_edges,
                          _freezing_mask, _recolor, _remove_beam_lines,
                          _remove_beams_polar, LT_MAXSIZE, active_feeds,
                          draw_borders,
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


# The store keeps a sweep per 15-minute slot: fine enough for the seek bar to
# step quarter-hours, and the round-hour sweeps are just the :00 slots. A
# sweep counts for a slot only within this tolerance, or a lone 13:29 sweep
# would be filed under both 13:15 and 13:30.
SLOT_MIN = 15
SLOT_TOL_MIN = 8
# Quarter frames borrow a feed's nearest sweep this far out when nothing sits
# within SLOT_TOL_MIN — wide enough to bridge the ~hourly sweeps of the days
# before the 15-min store, so no layer blinks out between its updates.
QUARTER_FILL_MIN = 31
# Upstream products ship single sweeps with a radar missing: on 22.08
# 09:00-10:00 the SMHI composite held 49k echo px at 09:15 but 13k at
# 09:30/09:45 (the eastern radars gone), and Estonia's 09:29 sweep painted
# 272k px against 448k at 09:47 (one antenna out). At 15-min cadence every
# such dropout used to blink a whole layer off for one frame. A partial
# product compresses far smaller, so a sweep whose stored size falls below
# this fraction of the best candidate in the window is passed over for the
# nearest complete one (and only used when nothing complete is in reach).
DEGRADED_FRAC = 0.7


def _pick(cands, tol_s):
    """One feed's [(gap_s, t, frame)] -> the sweep to use for a slot.

    Complete sweeps beat degraded ones, the fresh ring beats the fill ring,
    then the smallest gap wins."""
    ref = max(f.get("bytes", 0) for _, _, f in cands)
    return min(cands, key=lambda c: (ref and c[2].get("bytes", 0)
                                     < DEGRADED_FRAC * ref,
                                     c[0] > tol_s, c[0]))
# How long the quarter-hour detail is kept — sources and rendered frames both.
# After this only the round-hour sweeps stay (for the year), which is all the
# hourly archive ever needed.
QUARTER_DAYS = 14


def store_sources(hours_back: int = 6, per_hour: int = 1) -> int:
    """Pull the overlays for a window and keep them exactly as served.

    One sweep per feed per 15-minute slot. The bounds come with the overlay
    listing, not with the image, so they are written into the day index
    alongside it. Without them a stored PNG is an unplaceable rectangle.
    """
    s, s3 = session(), r2_client()
    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    stored = 0
    # One listing for the whole window, not one per slot. The endpoint takes an
    # epoch range and hands back everything inside it; the per-slot buckets are
    # a groupby over the single answer.
    try:
        allov = fetch_overlays_window(
            now - dt.timedelta(hours=hours_back, minutes=NEAR_MIN),
            now + dt.timedelta(minutes=NEAR_MIN))
    except Exception:
        log.exception("store: overlay listing failed")
        return 0
    idx_cache, dirty_days = {}, set()
    n_slots = hours_back * (60 // SLOT_MIN) + 1
    for back in range(n_slots - 1, -1, -1):
        hour = now - dt.timedelta(minutes=back * SLOT_MIN)
        by = collections.defaultdict(list)
        for o in allov:
            if abs((o["time"] - hour).total_seconds()) <= SLOT_TOL_MIN * 60:
                by[o["code"]].append(o)
        if hour.date() not in idx_cache:
            idx_cache[hour.date()] = _load_index(s3, hour.date())
        idx = idx_cache[hour.date()]
        have = {f["key"] for f in idx["frames"]}
        dirty = False
        for code, frames in by.items():
            frames.sort(key=lambda o: abs((o["time"] - hour).total_seconds()))
            for o in frames[:1]:
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
            dirty_days.add(hour.date())
    for d in dirty_days:
        idx_cache[d]["frames"].sort(key=lambda f: f["time"])
        _save_index(s3, d, idx_cache[d])
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


def render_stored(hours_back: int = 336, force: bool = False,
                  only_day: str | None = None) -> int:
    """Build hourly composites from the stored originals. No upstream traffic.

    This is the function a styling change should call. It reads only R2, so
    re-rendering a fortnight costs seconds and nothing on meteolapa's side.
    only_day ("YYYYMMDD", UTC) limits both passes to that day plus a 3-hour
    lead-in, so the Riga-local day is fully covered — for targeted re-renders.
    """
    import numpy as np

    from . import proj3059 as P

    day_lo = day_hi = None
    if only_day:
        d0 = dt.datetime.strptime(only_day, "%Y%m%d").replace(
            tzinfo=dt.timezone.utc)
        day_lo, day_hi = d0 - dt.timedelta(hours=3), d0 + dt.timedelta(hours=24)
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
        if day_lo is not None and not (day_lo <= hour < day_hi):
            continue
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
        cands = {}
        for f in pool:
            t = dt.datetime.strptime(f["time"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=dt.timezone.utc)
            gap = abs((t - hour).total_seconds())
            if gap > NEAR_MIN * 60:
                continue
            cands.setdefault(f["code"], []).append((gap, t, f))
        if not cands:
            continue
        best = {c: _pick(v, NEAR_MIN * 60) for c, v in cands.items()}
        vtag = _render_slot(s3, best, hour)
        if vtag is None:
            continue
        arch["hours"][hour_key] = vtag
        arch.setdefault("rev", {})[hour_key] = rev   # see the note in radar_backfill
        done += 1
    # Quarter-hour frames for the near past: every :15/:30/:45 slot of the
    # last QUARTER_DAYS gets its own composite wherever the store holds sweeps
    # within the slot tolerance. Keyed separately (arch["quarters"]) so the
    # hourly contract — and every consumer of it — is untouched; the page
    # treats the quarters as extra candidates for its nearest-frame lookup.
    #
    # Feeds are matched in two rings: fresh sweeps within SLOT_TOL_MIN win,
    # and a feed with nothing that close borrows its nearest sweep within
    # QUARTER_FILL_MIN instead of dropping out. Days from before the 15-min
    # store hold only ~hourly sweeps per feed; without the fill their quarter
    # frames were built from whichever feeds happened to sit near each slot,
    # and seeking blinked layers in and out (user report 23.08, EE on 22.08).
    # A borrowed layer just persists between its updates — no blink.
    quarters = arch.setdefault("quarters", {})
    qcut = (now - dt.timedelta(days=QUARTER_DAYS)).strftime("%Y%m%dT%H%M")
    for k in [k for k in quarters if k < qcut]:
        del quarters[k]
    q_done = 0
    for back in range(QUARTER_DAYS * 24 * 4):
        slot = now - dt.timedelta(minutes=SLOT_MIN * back)
        if slot.minute == 0:
            continue                                 # the hourly pass owns :00
        if day_lo is not None and not (day_lo <= slot < day_hi):
            continue
        qkey = slot.strftime("%Y%m%dT%H%M")
        if not force and qkey in quarters:
            continue
        d = slot.date()
        if d not in days:
            days[d] = _load_index(s3, d)
        pool = days[d]["frames"]
        if slot.hour == 0:                 # the fill ring reaches yesterday
            prev = (slot - dt.timedelta(hours=1)).date()
            if prev not in days:
                days[prev] = _load_index(s3, prev)
            pool = pool + days[prev]["frames"]
        cands = {}
        for f in pool:
            t = dt.datetime.strptime(f["time"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=dt.timezone.utc)
            gap = abs((t - slot).total_seconds())
            if gap > QUARTER_FILL_MIN * 60:
                continue
            cands.setdefault(f["code"], []).append((gap, t, f))
        best = {c: _pick(v, SLOT_TOL_MIN * 60) for c, v in cands.items()}
        # A quarter frame needs a Baltic sweep. Backfilled SE/FI history goes
        # further back than the 15-min meteolapa store; an SE-only composite
        # would paint the Baltic blank — worse than the page falling back to
        # the round hour's frame.
        if not any(c in best for c in ("EE", "LT", "LT2", "LV")):
            continue
        vtag = _render_slot(s3, best, slot)
        if vtag is None:
            continue
        quarters[qkey] = vtag
        arch.setdefault("rev", {})[qkey] = rev
        q_done += 1
    s3.put_object(Bucket=config.R2_BUCKET, Key=ARCHIVE_KEY,
                  Body=json.dumps(arch).encode(),
                  ContentType="application/json",
                  CacheControl="public, max-age=300")
    log.info("radar render: %d hourly + %d quarter composites from stored sources",
             done, q_done)
    return done + q_done


def _render_slot(s3, best, when):
    """One slot's chosen sweeps -> a stored composite frame; returns its vtag.

    Neighbours first, LV last on top, as in the live composite. The nordic
    frames are stored as level grids already on the 3059 canvas (grid3059), so
    they skip the classify/resample/beam chain. Recolor happens once every
    feed is in, so the weak-echo fade knows which radars are awake
    (coverage-aware — see _weak_alpha_aware)."""
    from . import proj3059 as P
    canvas = Image.new("RGBA", (P.W, P.H), (0, 0, 0, 0))
    cold = _freezing_mask(when)
    used = []
    layers = []
    for code in ("SE", "FI", "EE", "LT2", "LT", "LV"):
        if code not in best:
            continue
        _, t, f = best[code]
        try:
            if f.get("grid3059"):
                from .radar_nordic import load_stored
                grid = load_stored(s3, f)
            else:
                cls = _classify_stored(s3, f)
                grid = P.resample_mercator_image(cls, tuple(f["sw"]), tuple(f["ne"]))
                grid = _close_radial_spokes(_despeckle_intensity(
                    _remove_beam_lines(_remove_beams_polar(_despeckle(grid)))))
        except Exception:
            log.exception("render: %s unreadable", f["key"])
            continue
        layers.append((code, grid))
        used.append(t)
    if not used:
        return None
    act = active_feeds(layers)
    for code, grid in layers:
        canvas.alpha_composite(_recolor(grid, cold, code, act))
    canvas = _fade_edges(canvas)
    draw_borders(canvas)
    # Named after the sweep it came from, not the slot it is filed under.
    vtag = max(used).strftime("%Y%m%dT%H%M")
    buf = io.BytesIO()
    canvas.save(buf, "PNG", optimize=True)
    s3.put_object(Bucket=config.R2_BUCKET, Key=f"{FRAME_PREFIX}/{vtag}.png",
                  Body=buf.getvalue(), ContentType="image/png",
                  CacheControl="public, max-age=604800")
    return vtag


def prune(src_days: int = SRC_KEEP_DAYS, frame_days: int = FRAME_KEEP_DAYS) -> dict:
    """Quarter-hour detail for a fortnight, round hours for a year.

    Two-phase, sources and rendered frames alike: inside QUARTER_DAYS
    everything stays; between QUARTER_DAYS and the long limit only stamps
    within the slot tolerance of a round hour survive (the sweeps the hourly
    archive is built from); past the long limit everything goes. Anything
    inside the year still re-renders hourly from what is kept.
    """
    s3 = r2_client()
    now = dt.datetime.now(dt.timezone.utc)
    out = {"sources": 0, "frames": 0}
    q_cut = now - dt.timedelta(days=QUARTER_DAYS)
    # Frames the hourly archive references are never minute-pruned: an hourly
    # frame is NAMED by its sweep, which NEAR_MIN lets sit well off the hour.
    try:
        arch = json.loads(s3.get_object(Bucket=config.R2_BUCKET,
                                        Key=ARCHIVE_KEY)["Body"].read())
        protected = {f"{FRAME_PREFIX}/{v}.png" for v in arch.get("hours", {}).values()}
    except Exception:
        protected = set()

    def near_hour(t):
        m = t.minute
        return m <= SLOT_TOL_MIN or m >= 60 - SLOT_TOL_MIN

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
                if t is None or obj["Key"] in protected:
                    continue
                if t < older_than or (t < q_cut and not near_hour(t)):
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


# --- per-feed debug frames -------------------------------------------------
# The composite hides WHICH feed lost an area: a gap can be an upstream
# dropout, a cleaning pass, a fade, or the slot picking a different sweep.
# These render every feed alone on the same canvas, per slot, so seeking the
# debug page shows exactly which layer blinks and when (docs/radar_feeds.html).
FEED_PREFIX = "maps/radar/feeds3059"
FEED_ARCHIVE_KEY = "maps/radar/feeds.json"
FEED_CODES = ("LV", "EE", "LT2", "LT", "SE", "FI")


def _site_pixels_for(code):
    """[(site name, x px, y px)] for the antennas belonging to one feed."""
    from .scrape_maps import RADAR_SITES
    from . import proj3059 as P
    out = []
    for name, la, lo in RADAR_SITES:
        if not name.startswith(code[:2]):
            continue
        x, y = P.to_xy(lo, la)
        out.append((name, (x - P.X0) / (P.X1 - P.X0) * P.W,
                    (P.Y1 - y) / (P.Y1 - P.Y0) * P.H))
    return out


def _feed_grid(s3, code, f):
    """The feed's cleaned level grid on our raster (the expensive part)."""
    from . import proj3059 as P
    if f.get("grid3059"):
        from .radar_nordic import load_stored
        return load_stored(s3, f)
    cls = _classify_stored(s3, f)
    grid = P.resample_mercator_image(cls, tuple(f["sw"]), tuple(f["ne"]))
    return _close_radial_spokes(_despeckle_intensity(
        _remove_beam_lines(_remove_beams_polar(_despeckle(grid)))))


def _render_feed_slot(s3, code, t, f, when):
    """One sweep -> one stored frame PER ANTENNA of that feed.

    A national product mixes several radars, and "which radar dropped out"
    is the question the debug page exists to answer — so each pixel is
    assigned to the NEAREST antenna of that feed and every antenna gets its
    own frame. For the nordic composites our site list holds only the
    antennas whose range reaches this map, so their panels read as "the part
    of the product nearest this site", which is what makes one of SMHI's
    eastern radars going missing visible on its own.

    Returns {site name: vtag}; the whole feed also goes out as one frame
    under the feed code, so the old whole-product view stays available.
    """
    import numpy as np

    from . import proj3059 as P
    try:
        grid = _feed_grid(s3, code, f)
    except Exception:
        log.exception("feed render: %s unreadable", f["key"])
        return {}
    cold = _freezing_mask(when)
    out = {}

    def emit(g, tag):
        canvas = Image.new("RGBA", (P.W, P.H), (0, 0, 0, 0))
        # No active-feeds argument: each layer is judged on its own here,
        # which is the point — the page shows it as the radar delivered it.
        canvas.alpha_composite(_recolor(g, cold, code, None))
        canvas = _fade_edges(canvas)
        draw_borders(canvas)
        vtag = f"{tag}_{t:%Y%m%dT%H%M}"
        buf = io.BytesIO()
        canvas.save(buf, "PNG", optimize=True)
        s3.put_object(Bucket=config.R2_BUCKET, Key=f"{FEED_PREFIX}/{vtag}.png",
                      Body=buf.getvalue(), ContentType="image/png",
                      CacheControl="public, max-age=604800")
        return vtag

    out[code] = emit(grid, code)
    sites = _site_pixels_for(code)
    if len(sites) > 1:
        yy, xx = np.mgrid[0:P.H, 0:P.W]
        d = np.stack([np.hypot(xx - sx, yy - sy) for _, sx, sy in sites])
        nearest = d.argmin(0)
        for i, (name, _, _) in enumerate(sites):
            g = np.where(nearest == i, grid, 0).astype(grid.dtype)
            if (g > 0).any():
                out[name] = emit(g, name.replace(" ", "-"))
            else:
                out[name] = emit(g, name.replace(" ", "-"))
    elif sites:
        out[sites[0][0]] = out[code]
    return out


def render_feeds(only_day: str, from_h: int | None = None,
                 to_h: int | None = None) -> int:
    """Per-feed debug frames for one UTC day, every 15-min slot.

    from_h/to_h narrow it to a UTC hour window (e.g. 9..16) so a stretch
    under investigation renders in minutes instead of the whole day.

    Writes FEED_ARCHIVE_KEY as {"path", "slots": {slotkey: {code: vtag}},
    "picked": {slotkey: {code: "HH:MM(*)"}}} — the star marks a sweep taken
    from the fill ring, so the page can show where a layer is being held over
    rather than freshly observed."""
    s3 = r2_client()
    d0 = dt.datetime.strptime(only_day, "%Y%m%d").replace(tzinfo=dt.timezone.utc)
    lo = d0 + dt.timedelta(hours=from_h) if from_h is not None \
        else d0 - dt.timedelta(hours=3)
    hi = d0 + dt.timedelta(hours=to_h) if to_h is not None \
        else d0 + dt.timedelta(hours=24)
    try:
        arch = json.loads(s3.get_object(Bucket=config.R2_BUCKET,
                                        Key=FEED_ARCHIVE_KEY)["Body"].read())
    except Exception:
        arch = {}
    panels = []
    for c in FEED_CODES:
        sites = [n for n, _, _ in _site_pixels_for(c)]
        panels.append({"code": c, "sites": sites})
    arch.update({"path": FEED_PREFIX, "day": only_day, "panels": panels,
                 "window": f"{lo:%H:%M}-{hi:%H:%M}Z",
                 "generated_at": dt.datetime.now(dt.timezone.utc).strftime(
                     "%Y%m%dT%H%M")})
    slots, picked = {}, {}
    days, done = {}, 0
    # A sweep is usually picked by two or three neighbouring slots (and by the
    # fill ring beyond that). The frame is named after the sweep, so render
    # each distinct one ONCE and let the later slots point at it — this is
    # most of the run's cost on the meteolapa feeds, which go through the
    # whole classify/beam/polar chain.
    seen = {}

    def flush():
        arch["slots"], arch["picked"] = slots, picked
        s3.put_object(Bucket=config.R2_BUCKET, Key=FEED_ARCHIVE_KEY,
                      Body=json.dumps(arch).encode(),
                      ContentType="application/json",
                      CacheControl="public, max-age=30")

    # An empty index up front, so the page has something to read while the
    # rest of the day renders rather than failing on a missing key.
    flush()
    slot = lo
    while slot < hi:
        d = slot.date()
        for dd in (d, (slot - dt.timedelta(hours=1)).date()):
            if dd not in days:
                days[dd] = _load_index(s3, dd)
        pool = days[d]["frames"] + days[(slot - dt.timedelta(hours=1)).date()]["frames"]
        cands = {}
        for f in pool:
            t = dt.datetime.strptime(f["time"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=dt.timezone.utc)
            gap = abs((t - slot).total_seconds())
            if gap > QUARTER_FILL_MIN * 60:
                continue
            cands.setdefault(f["code"], []).append((gap, t, f))
        skey = slot.strftime("%Y%m%dT%H%M")
        for code, v in cands.items():
            gap, t, f = _pick(v, SLOT_TOL_MIN * 60)
            key = (code, t)
            if key in seen:
                tags = seen[key]
            else:
                tags = _render_feed_slot(s3, code, t, f, slot)
                seen[key] = tags
                done += len(tags)
            stamp = t.strftime("%H:%M") + ("*" if gap > SLOT_TOL_MIN * 60
                                           else "")
            for panel, vtag in tags.items():
                slots.setdefault(skey, {})[panel] = vtag
                picked.setdefault(skey, {})[panel] = stamp
        if skey in slots:
            flush()          # the page can seek what is ready already
        slot += dt.timedelta(minutes=SLOT_MIN)
    arch["complete"] = True
    flush()
    log.info("radar feeds: %d per-feed frames over %d slots (%s)",
             done, len(slots), only_day)
    return done
