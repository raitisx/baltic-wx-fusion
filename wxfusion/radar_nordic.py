"""SMHI (Sweden) + FMI (Finland) radar composites as feeds for the Baltic map.

Both open data, CC BY 4.0, key-free — verified live 22.08.2026:

  SMHI  .../api/version/latest/area/sweden/product/comp/{Y/m/d} JSON listing;
        GeoTIFF ~31 kB, 471x887 @ 2 km, EPSG:25833, uint8 carrying REAL dBZ
        (dBZ = 0.4*v - 30; 0 = clear, 255 = outside coverage). ~2.5-5 min
        cadence, 8-11 min latency. Covers the Baltic sea west of Courland.

  FMI   WFS stored query fmi::radar::composite::rr lists GeoTIFF URLs in
        gml:fileReference; 1987x3144 @ 500 m, EPSG:3067, uint16 carrying REAL
        rain rate (value/100 mm/h; 0 = nodata). 5 min cadence, 4-7 min
        latency. Covers the Gulf of Finland and northern Estonia.

The values are numbers, not colours, so classification is rate_to_lev() on
real mm/h — none of the palette matching the meteolapa feeds need. Each fetch
is warped straight onto the shared 570x690 EPSG:3059 grid and stored as a
small grayscale LEVEL png in the radar_src day index (entry flag
"grid3059": true), so render_stored rebuilds past hours from the same store
the meteolapa feeds use. A fetch failure is logged and skipped — the Baltic
composite must never fail over a neighbour.

Credit lines for the map: SMHI Öppna Data and FMI open data, both CC BY 4.0.
"""
from __future__ import annotations

import datetime as dt
import functools
import io
import json
import logging
import re

import numpy as np
from PIL import Image

from . import config
from .http import session

log = logging.getLogger(__name__)

SMHI_LIST = ("https://opendata-download-radar.smhi.se/api/version/latest/"
             "area/sweden/product/comp/{d:%Y/%m/%d}")
FMI_WFS = ("https://opendata.fmi.fi/wfs?service=WFS&version=2.0.0"
           "&request=getFeature&storedquery_id=fmi::radar::composite::rr")
# FMI's public S3 mirror. The WFS above serves only ~7 days; the bucket holds
# 5-min national composites (finrad, PCAPPI 600 m DBZH, uint8, EPSG:3067,
# 250 m, nodata 255) back to 2020 — probed 22.08.2026.
FMI_S3 = "https://fmi-opendata-radar-geotiff.s3.eu-west-1.amazonaws.com"

SRC_PREFIX = "maps/radar_src"          # same store the meteolapa originals use

# Backfill slotting — mirrors radar_store's quarter grid (kept local so this
# module never imports radar_store).
SLOT_MIN, SLOT_TOL_MIN = 15, 8


def _dst_transform():
    from rasterio.transform import from_origin

    from . import proj3059 as P
    return from_origin(P.X0, P.Y1, 1000, 1000)


def _warp_rr_to_grid(rr, src_transform, src_crs):
    """Rain rate (mm/h) on a source grid -> mm/h on our 570x690 EPSG:3059."""
    from rasterio.warp import Resampling, reproject

    from . import proj3059 as P
    dst = np.zeros((P.H, P.W), np.float32)
    reproject(rr.astype(np.float32), dst,
              src_transform=src_transform, src_crs=src_crs,
              dst_transform=_dst_transform(), dst_crs="EPSG:3059",
              dst_nodata=0.0, resampling=Resampling.max)
    return dst


def _to_lev(rr_grid):
    from .scrape_maps import rate_to_lev
    lev = np.zeros(rr_grid.shape, np.uint8)
    wet = rr_grid >= 0.06          # below ~0.06 mm/h is radar noise, not rain
    lev[wet] = rate_to_lev(rr_grid[wet])
    return lev


# Cross-feed calibration, middle anchor chosen by the user 22.08.2026 from
# test renders: everything sits at HALF of FMI's rr product. From the co-wet
# probe medians (SE_raw/FI x0.60, SE_raw/LT2 x2.0, FI/EE x4.5):
#   FI  /2      -> FMI_RATE_DIV = 2 (as dB through Marshall-Palmer: 4.8)
#   SE  -1.3 dB -> SE_raw x0.83 = FI/2 through the direct SE-FI overlap
#   EE/LT/LV lifted in scrape_maps.CAL_LEV_FACTOR (x2.25 / x1.67).
# SMHI's comp is column-max reflectivity (runs hot through MP), hence SE still
# gets a negative offset while the palette feeds get lifts.
SMHI_DBZ_ADJ = 1.3
FMI_DBZ_ADJ = 4.8
FMI_RATE_DIV = 10.0 ** (FMI_DBZ_ADJ / 16.0)      # ~2.0
# The edge softening lives in the COMPOSITE (scrape_maps SE_EDGE_*), never
# here: a decode-time fade cannot know whether another feed covers the pixel,
# and on 22.08 it erased a real storm west of the Estonian islands where SE
# was the only witness. Stored SE frames carry the full product.


_last_se_cov = None       # coverage of the most recently decoded SMHI frame


def _decode_smhi(body):
    """SMHI GeoTIFF bytes -> level grid on our 3059 raster."""
    global _last_se_cov
    import rasterio
    with rasterio.open(io.BytesIO(body)) as ds:
        v = ds.read(1)
        # dBZ = 0.4*v - 30; 0 = clear, 255 = outside coverage
        mask = (v > 0) & (v < 255)
        dbz = np.where(mask,
                       0.4 * v.astype(np.float32) - 30.0 - SMHI_DBZ_ADJ,
                       -100.0)
        rr = np.where(mask, (10.0 ** (dbz / 10.0) / 200.0) ** (1.0 / 1.6), 0.0)
        _last_se_cov = _warp_rr_to_grid((v != 255).astype(np.float32),
                                        ds.transform, ds.crs) > 0.5
        return _to_lev(_warp_rr_to_grid(rr, ds.transform, ds.crs))


def store_edge_map(s3):
    """km-to-edge map of the SE product footprint -> R2, for the composite's
    SE fade (scrape_maps SE_EDGE_KEY). Called after an SE decode; cheap
    (~20 kB), so it simply overwrites and the footprint stays current."""
    if _last_se_cov is None:
        return
    from scipy.ndimage import distance_transform_edt
    edge = np.clip(distance_transform_edt(_last_se_cov), 0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(edge, "L").save(buf, "PNG", optimize=True)
    s3.put_object(Bucket=config.R2_BUCKET, Key="maps/radar_src/se_edge_km.png",
                  Body=buf.getvalue(), ContentType="image/png")


def _decode_fmi(body):
    """FMI rr GeoTIFF bytes -> level grid, on the FMI_RATE_DIV anchor."""
    import rasterio
    with rasterio.open(io.BytesIO(body)) as ds:
        v = ds.read(1).astype(np.float32)
        rr = np.where((v > 0) & (v < 65000),
                      v / 100.0 / FMI_RATE_DIV, 0.0)   # value/100 = mm/h
        return _to_lev(_warp_rr_to_grid(rr, ds.transform, ds.crs))


def _decode_fmi_dbzh(body):
    """finrad PCAPPI DBZH GeoTIFF from FMI's S3 mirror -> level grid.

    uint8 with the standard ODIM 8-bit scaling (dBZ = 0.5*v - 32, 255 =
    nodata), through the same Marshall-Palmer with the FMI_DBZ_ADJ anchor so
    both FI paths land on the same levels."""
    import rasterio
    with rasterio.open(io.BytesIO(body)) as ds:
        v = ds.read(1)
        mask = (v > 0) & (v < 255)
        dbz = np.where(mask,
                       0.5 * v.astype(np.float32) - 32.0 - FMI_DBZ_ADJ,
                       -100.0)
        rr = np.where(mask, (10.0 ** (dbz / 10.0) / 200.0) ** (1.0 / 1.6), 0.0)
        return _to_lev(_warp_rr_to_grid(rr, ds.transform, ds.crs))


def _pair_fmi(urls, stamps):
    """WFS fileReference/timePosition lists -> (stamp, url) pairs, or None.

    Time-window queries return a begin AND an end timePosition per member
    (2N stamps for N urls) — the end is the frame's nominal time. The plain
    latest-composite query returns one stamp each."""
    if len(stamps) == 2 * len(urls):
        stamps = stamps[1::2]
    if not urls or len(stamps) != len(urls):
        return None
    return [(dt.datetime.fromisoformat(t.replace("Z", "+00:00")),
             u.replace("&amp;", "&")) for u, t in zip(urls, stamps)]


# FMI's twelve radars are published individually on the same S3 mirror, as
# {Y/m/d}/{site}/{stamp}_{site}_cappi_600_dbzh_qc.tif — 2000x2000 at 250 m,
# so the grid is a 250 km box centred on the antenna and its CENTRE PIXEL IS
# THE ANTENNA (verified 23.08.2026: fikor's centre lands on Korppoo to 400 m).
# Only the five whose range reaches our raster are worth fetching; the
# coordinates below are read off those grids, not from a published list.
FI_SITES = {
    "FIKOR": ("fikor", "FI Korppoo", 60.129, 21.639),
    "FIVIH": ("fivih", "FI Vihti", 60.556, 24.494),
    "FIANJ": ("fianj", "FI Anjalankoski", 60.904, 27.108),
    "FIKAN": ("fikan", "FI Kankaanpaa", 61.811, 22.500),
    "FIKES": ("fikes", "FI Kesalahti", 61.907, 29.799),
}


def _fmi_site_entries(s, day, site):
    """(stamp, url) pairs for one day of ONE FMI radar's cappi product."""
    r = s.get(f"{FMI_S3}/?list-type=2&max-keys=1000"
              f"&prefix={day:%Y/%m/%d}/{site}/", timeout=60)
    out = []
    for k in re.findall(r"<Key>([^<]+)</Key>", r.text):
        m = re.search(rf"/(\d{{12}})_{site}_cappi_600_dbzh", k)
        if m:
            out.append((dt.datetime.strptime(m.group(1), "%Y%m%d%H%M")
                        .replace(tzinfo=dt.timezone.utc), f"{FMI_S3}/{k}"))
    return sorted(out)


def fetch_fmi_site(code):
    """Latest sweep of one FMI radar -> (stamp, level grid) or None.

    Same product family as the national composite (uint8 DBZH, ODIM 8-bit
    scaling, 255 nodata), so it goes through the same decode and lands on the
    same calibrated levels — the difference is that this one is ONE radar.
    """
    site = FI_SITES[code][0]
    s = session()
    now = dt.datetime.now(dt.timezone.utc)
    ent = []
    for day in (now.date(), (now - dt.timedelta(days=1)).date()):
        ent += _fmi_site_entries(s, day, site)
        if ent:
            break
    if not ent:
        return None
    stamp, url = max(ent)
    return stamp, _decode_fmi_dbzh(s.get(url, timeout=120).content)


def _fmi_s3_entries(s, day):
    """(stamp, url) pairs for one day's finrad composites on the S3 mirror."""
    r = s.get(f"{FMI_S3}/?list-type=2&max-keys=1000"
              f"&prefix={day:%Y/%m/%d}/finrad/", timeout=60)
    out = []
    for k in re.findall(r"<Key>([^<]+)</Key>", r.text):
        m = re.search(r"/(\d{12})_composite_cappi_600_dbzh", k)
        if m:
            out.append((dt.datetime.strptime(m.group(1), "%Y%m%d%H%M")
                        .replace(tzinfo=dt.timezone.utc), f"{FMI_S3}/{k}"))
    return out


def fetch_smhi():
    """Latest SMHI composite -> (stamp, level grid) or None."""
    s = session()
    now = dt.datetime.now(dt.timezone.utc)
    files = []
    for day in (now.date(), (now - dt.timedelta(days=1)).date()):
        try:
            r = s.get(SMHI_LIST.format(d=day), timeout=45)
            if r.ok:
                files += r.json().get("files") or []
        except Exception:
            continue
    if not files:
        return None
    # Newest file that actually offers a GeoTIFF — the very latest entry can
    # briefly list only the png while the tif is still being written.
    latest = tif = None
    for cand in sorted(files, key=lambda f: f.get("valid", ""), reverse=True):
        t = next((f["link"] for f in cand.get("formats", [])
                  if f.get("key") == "tif"), None)
        if t:
            latest, tif = cand, t
            break
    if latest is None:
        return None
    stamp = dt.datetime.strptime(latest["valid"], "%Y-%m-%d %H:%M").replace(
        tzinfo=dt.timezone.utc)
    return stamp, _decode_smhi(s.get(tif, timeout=60).content)


def fetch_fmi():
    """Latest FMI rr composite -> (stamp, level grid) or None."""
    s = session()
    r = s.get(FMI_WFS, timeout=60)
    r.raise_for_status()
    urls = re.findall(r"<gml:fileReference>\s*([^<]+?)\s*</gml:fileReference>",
                      r.text)
    stamps = re.findall(r"<gml:timePosition>([^<]+)</gml:timePosition>", r.text)
    if not urls:
        return None
    stamp = dt.datetime.fromisoformat(
        stamps[-1].replace("Z", "+00:00")) if stamps else dt.datetime.now(dt.timezone.utc)
    return stamp, _decode_fmi(s.get(urls[-1].replace("&amp;", "&"),
                                    timeout=120).content)


def _index_key(day):
    return f"{SRC_PREFIX}/{day:%Y/%m/%d}/index.json"


def _load_day_idx(s3, day, cache=None):
    if cache is not None and day in cache:
        return cache[day]
    try:
        idx = json.loads(s3.get_object(Bucket=config.R2_BUCKET,
                                       Key=_index_key(day))["Body"].read())
    except Exception:
        idx = {"frames": []}
    if cache is not None:
        cache[day] = idx
    return idx


def _save_day_idx(s3, day, idx):
    s3.put_object(Bucket=config.R2_BUCKET, Key=_index_key(day),
                  Body=json.dumps(idx).encode(),
                  ContentType="application/json",
                  CacheControl="public, max-age=300")


def _store_frame(s3, code, stamp, lev, idx_cache=None, dirty=None):
    """Level grid -> grayscale png in the radar_src store + day index entry.

    The GeoTIFF the grid came from is never stored — only this small lossless
    png. With idx_cache/dirty the index write is deferred to the caller (the
    backfill saves each touched day once instead of once per frame)."""
    key = f"{SRC_PREFIX}/{stamp:%Y/%m/%d}/{stamp:%Y%m%d%H%M}{code}.png"
    buf = io.BytesIO()
    Image.fromarray(lev, "L").save(buf, "PNG", optimize=True)
    s3.put_object(Bucket=config.R2_BUCKET, Key=key, Body=buf.getvalue(),
                  ContentType="image/png")
    idx = _load_day_idx(s3, stamp.date(), idx_cache)
    if not any(f.get("key") == key for f in idx["frames"]):
        idx["frames"].append({"key": key, "code": code,
                              "time": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                              "grid3059": True, "enc": "lev",
                              "bytes": buf.getbuffer().nbytes})
        if dirty is not None:
            dirty.add(stamp.date())
        else:
            _save_day_idx(s3, stamp.date(), idx)


def load_stored(s3, frame):
    """Day-index entry with grid3059 -> level grid."""
    body = s3.get_object(Bucket=config.R2_BUCKET,
                         Key=frame["key"])["Body"].read()
    return np.array(Image.open(io.BytesIO(body)).convert("L"), np.uint8)


def _backfill_slots(s3, s, code, entries, decode, idx_cache, dirty,
                    overwrite=False):
    """Store one sweep per 15-min slot from (stamp, url) entries.

    Slots that already hold a stored sweep for this code (within the slot
    tolerance) are skipped, so re-runs and overlap with the live feed cost
    nothing. overwrite ignores what is stored and re-decodes everything —
    for when the decode itself changed (same stamps -> same keys, so the
    stale pngs are replaced in place). Returns the number stored."""
    stored = 0
    by_day = {}
    for stamp, url in entries:
        by_day.setdefault(stamp.date(), []).append((stamp, url))
    for day, avail in sorted(by_day.items()):
        idx = _load_day_idx(s3, day, idx_cache)
        have = [] if overwrite else [
            dt.datetime.strptime(f["time"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=dt.timezone.utc)
            for f in idx["frames"] if f.get("code") == code]
        day0 = dt.datetime.combine(day, dt.time(0), tzinfo=dt.timezone.utc)
        for n in range(24 * 60 // SLOT_MIN):
            slot = day0 + dt.timedelta(minutes=n * SLOT_MIN)
            if any(abs((t - slot).total_seconds()) <= SLOT_TOL_MIN * 60
                   for t in have):
                continue
            best = min(avail, key=lambda e: abs((e[0] - slot).total_seconds()))
            if abs((best[0] - slot).total_seconds()) > SLOT_TOL_MIN * 60:
                continue
            try:
                lev = decode(s.get(best[1], timeout=120).content)
            except Exception as e:
                log.warning("backfill %s %s: %s", code, best[0], e)
                continue
            _store_frame(s3, code, best[0], lev, idx_cache, dirty)
            have.append(best[0])
            stored += 1
        log.info("backfill %s %s: day done (%d stored so far)",
                 code, day, stored)
    return stored


def topup(s3, hours=3):
    """Fill the recent hours' 15-min slots for SE + FI from the archives.

    The live fetch stores only the newest sweep per tick (a 30-min cadence),
    which would leave half the quarter slots empty going forward. Both
    archives serve the near past, so every tick tops up whatever slots the
    live path missed — the nordic feeds then follow the same 15-min rules as
    the meteolapa frames. Cheap: at most a handful of small downloads."""
    s = session()
    now = dt.datetime.now(dt.timezone.utc)
    cut = now - dt.timedelta(hours=hours)
    idx_cache, dirty = {}, set()
    stored = 0

    entries = []
    for day in sorted({cut.date(), now.date()}):
        try:
            r = s.get(SMHI_LIST.format(d=day), timeout=45)
            for f in (r.json().get("files") or []) if r.ok else []:
                tif = next((x["link"] for x in f.get("formats", [])
                            if x.get("key") == "tif"), None)
                if not tif:
                    continue
                stamp = dt.datetime.strptime(
                    f["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc)
                if stamp >= cut:
                    entries.append((stamp, tif))
        except Exception as e:
            log.warning("nordic topup SE listing: %s", e)
    stored += _backfill_slots(s3, s, "SE", entries, _decode_smhi,
                              idx_cache, dirty)
    try:
        store_edge_map(s3)
    except Exception:
        log.exception("SE edge map not stored")

    entries = []
    try:
        r = s.get(f"{FMI_WFS}&starttime={cut:%Y-%m-%dT%H:%M:00Z}", timeout=120)
        urls = re.findall(
            r"<gml:fileReference>\s*([^<]+?)\s*</gml:fileReference>", r.text)
        stamps = re.findall(r"<gml:timePosition>([^<]+)</gml:timePosition>",
                            r.text)
        entries = _pair_fmi(urls, stamps) or []
    except Exception as e:
        log.warning("nordic topup FI listing: %s", e)
    stored += _backfill_slots(s3, s, "FI", entries, _decode_fmi,
                              idx_cache, dirty)

    for day in sorted(dirty):
        _save_day_idx(s3, day, idx_cache[day])
    if stored:
        log.info("nordic topup: %d frames stored", stored)
    return stored


def reindex(s3, days=20):
    """Repair day indexes for stored SE/FI pngs that are missing from them.

    The backfill flushes indexes at the end, so a cancelled run leaves stored
    objects invisible. Lists the store and adds the missing entries; safe to
    re-run any time."""
    now = dt.datetime.now(dt.timezone.utc)
    dirty, cache = set(), {}
    added = 0
    for back in range(days, -1, -1):
        day = (now - dt.timedelta(days=back)).date()
        idx = _load_day_idx(s3, day, cache)
        known = {f["key"] for f in idx["frames"]}
        token = None
        while True:
            kw = dict(Bucket=config.R2_BUCKET,
                      Prefix=f"{SRC_PREFIX}/{day:%Y/%m/%d}/")
            if token:
                kw["ContinuationToken"] = token
            r = s3.list_objects_v2(**kw)
            for o in r.get("Contents", []):
                m = re.search(r"/(\d{12})(SE|FI)\.png$", o["Key"])
                if not m or o["Key"] in known:
                    continue
                stamp = dt.datetime.strptime(
                    m.group(1), "%Y%m%d%H%M").replace(tzinfo=dt.timezone.utc)
                idx["frames"].append(
                    {"key": o["Key"], "code": m.group(2),
                     "time": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "grid3059": True, "enc": "lev", "bytes": int(o["Size"])})
                dirty.add(day)
                added += 1
            if not r.get("IsTruncated"):
                break
            token = r.get("NextContinuationToken")
    for day in sorted(dirty):
        _save_day_idx(s3, day, cache[day])
    log.info("nordic reindex: %d entries restored over %d days",
             added, len(dirty))
    return added


def backfill(s3, se_days=14, fi_days=14, se_overwrite=False,
             fi_overwrite=False):
    """Pull past SE + FI sweeps onto the 15-min slot grid of the store.

    SMHI's dated listing reaches back years. FMI's WFS serves only about a
    week, so days it comes back empty for fall back to the S3 mirror's
    finrad DBZH composites. Only the warped level grids are stored (lossless
    grayscale pngs), never the source GeoTIFFs. se_/fi_overwrite re-decode
    frames that are already stored (after an SMHI_DBZ_ADJ change)."""
    s = session()
    now = dt.datetime.now(dt.timezone.utc)
    idx_cache, dirty = {}, set()

    entries = []
    for back in range(se_days, -1, -1):
        day = (now - dt.timedelta(days=back)).date()
        try:
            r = s.get(SMHI_LIST.format(d=day), timeout=45)
            for f in (r.json().get("files") or []) if r.ok else []:
                tif = next((x["link"] for x in f.get("formats", [])
                            if x.get("key") == "tif"), None)
                if tif:
                    entries.append((dt.datetime.strptime(
                        f["valid"], "%Y-%m-%d %H:%M").replace(
                            tzinfo=dt.timezone.utc), tif))
        except Exception as e:
            log.warning("backfill SE listing %s: %s", day, e)
    n_se = _backfill_slots(s3, s, "SE", entries, _decode_smhi,
                           idx_cache, dirty, overwrite=se_overwrite)
    try:
        store_edge_map(s3)
    except Exception:
        log.exception("SE edge map not stored")

    wfs_entries, s3_entries = [], []
    for back in range(fi_days, -1, -1):
        day = (now - dt.timedelta(days=back)).date()
        day_entries = None
        try:
            q = (f"{FMI_WFS}&starttime={day:%Y-%m-%d}T00:00:00Z"
                 f"&endtime={day + dt.timedelta(days=1):%Y-%m-%d}T00:00:00Z")
            r = s.get(q, timeout=120)
            urls = re.findall(
                r"<gml:fileReference>\s*([^<]+?)\s*</gml:fileReference>",
                r.text)
            stamps = re.findall(
                r"<gml:timePosition>([^<]+)</gml:timePosition>", r.text)
            day_entries = _pair_fmi(urls, stamps)
            if urls and day_entries is None:
                log.warning("backfill FI %s: %d urls vs %d stamps, ignored",
                            day, len(urls), len(stamps))
        except Exception as e:
            log.warning("backfill FI listing %s: %s", day, e)
        if day_entries:
            wfs_entries += day_entries
        else:
            try:
                s3_entries += _fmi_s3_entries(s, day)
            except Exception as e:
                log.warning("backfill FI s3 listing %s: %s", day, e)
    n_fi = _backfill_slots(s3, s, "FI", wfs_entries, _decode_fmi,
                           idx_cache, dirty, overwrite=fi_overwrite)
    n_fi += _backfill_slots(s3, s, "FI", s3_entries, _decode_fmi_dbzh,
                            idx_cache, dirty, overwrite=fi_overwrite)

    for day in sorted(dirty):
        _save_day_idx(s3, day, idx_cache[day])
    log.info("nordic backfill: %d SE + %d FI frames stored, %d day "
             "indexes updated", n_se, n_fi, len(dirty))
    return n_se + n_fi


def live_layers(s3):
    """Fetch SE + FI now, store the frames, return [(code, level grid)].

    Best-effort per feed; an unreachable or stale neighbour is skipped."""
    out = []
    now = dt.datetime.now(dt.timezone.utc)
    fetches = [("SE", fetch_smhi), ("FI", fetch_fmi)]
    # The single radars ride alongside the composites: they are what the
    # debug page needs to judge one antenna against another, and what a
    # range-graded correction needs to know a real distance from. The main
    # composite still draws the national products until the sites are
    # calibrated against them.
    fetches += [(c, functools.partial(fetch_fmi_site, c)) for c in FI_SITES]
    for code, fetch in fetches:
        try:
            got = fetch()
            if not got:
                log.warning("nordic %s: nothing listed", code)
                continue
            stamp, lev = got
            if (now - stamp).total_seconds() > 90 * 60:
                log.warning("nordic %s: latest sweep %s is stale", code, stamp)
                continue
            try:
                _store_frame(s3, code, stamp, lev)
            except Exception:
                log.exception("nordic %s: frame not stored (still drawn)", code)
            if code == "SE":
                try:
                    store_edge_map(s3)
                except Exception:
                    log.exception("SE edge map not stored")
            out.append((code, lev))
            log.info("nordic %s: sweep %s, %d px echo", code, stamp,
                     int((lev > 0).sum()))
        except Exception:
            log.exception("nordic %s: skipped", code)
    return out
