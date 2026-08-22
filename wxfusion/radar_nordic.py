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


def _decode_smhi(body):
    """SMHI GeoTIFF bytes -> level grid on our 3059 raster."""
    import rasterio
    with rasterio.open(io.BytesIO(body)) as ds:
        v = ds.read(1)
        # dBZ = 0.4*v - 30; 0 = clear, 255 = outside coverage
        mask = (v > 0) & (v < 255)
        dbz = np.where(mask, 0.4 * v.astype(np.float32) - 30.0, -100.0)
        rr = np.where(mask, (10.0 ** (dbz / 10.0) / 200.0) ** (1.0 / 1.6), 0.0)
        return _to_lev(_warp_rr_to_grid(rr, ds.transform, ds.crs))


def _decode_fmi(body):
    """FMI rr GeoTIFF bytes -> level grid on our 3059 raster."""
    import rasterio
    with rasterio.open(io.BytesIO(body)) as ds:
        v = ds.read(1).astype(np.float32)
        rr = np.where((v > 0) & (v < 65000), v / 100.0, 0.0)   # value/100 = mm/h
        return _to_lev(_warp_rr_to_grid(rr, ds.transform, ds.crs))


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


def _backfill_slots(s3, s, code, entries, decode, idx_cache, dirty):
    """Store one sweep per 15-min slot from (stamp, url) entries.

    Slots that already hold a stored sweep for this code (within the slot
    tolerance) are skipped, so re-runs and overlap with the live feed cost
    nothing. Returns the number of frames stored."""
    stored = 0
    by_day = {}
    for stamp, url in entries:
        by_day.setdefault(stamp.date(), []).append((stamp, url))
    for day, avail in sorted(by_day.items()):
        idx = _load_day_idx(s3, day, idx_cache)
        have = [dt.datetime.strptime(f["time"], "%Y-%m-%dT%H:%M:%SZ").replace(
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


def backfill(s3, se_days=14, fi_days=7):
    """Pull past SE + FI sweeps onto the 15-min slot grid of the store.

    SMHI's dated listing reaches back years; FMI's WFS serves only about a
    week — both verified 22.08.2026. Only the warped level grids are stored
    (lossless grayscale pngs), never the source GeoTIFFs."""
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
                           idx_cache, dirty)

    entries = []
    for back in range(fi_days, -1, -1):
        day = (now - dt.timedelta(days=back)).date()
        q = (f"{FMI_WFS}&starttime={day:%Y-%m-%d}T00:00:00Z"
             f"&endtime={day + dt.timedelta(days=1):%Y-%m-%d}T00:00:00Z")
        try:
            r = s.get(q, timeout=120)
            urls = re.findall(
                r"<gml:fileReference>\s*([^<]+?)\s*</gml:fileReference>",
                r.text)
            stamps = re.findall(
                r"<gml:timePosition>([^<]+)</gml:timePosition>", r.text)
            if len(urls) != len(stamps):
                log.warning("backfill FI %s: %d urls vs %d stamps, skipped",
                            day, len(urls), len(stamps))
                continue
            entries += [(dt.datetime.fromisoformat(t.replace("Z", "+00:00")),
                         u.replace("&amp;", "&"))
                        for u, t in zip(urls, stamps)]
        except Exception as e:
            log.warning("backfill FI listing %s: %s", day, e)
    n_fi = _backfill_slots(s3, s, "FI", entries, _decode_fmi,
                           idx_cache, dirty)

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
    for code, fetch in (("SE", fetch_smhi), ("FI", fetch_fmi)):
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
            out.append((code, lev))
            log.info("nordic %s: sweep %s, %d px echo", code, stamp,
                     int((lev > 0).sum()))
        except Exception:
            log.exception("nordic %s: skipped", code)
    return out
