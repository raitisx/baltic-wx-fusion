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


def fetch_smhi():
    """Latest SMHI composite -> (stamp, level grid) or None."""
    import rasterio
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
    body = s.get(tif, timeout=60).content
    with rasterio.open(io.BytesIO(body)) as ds:
        v = ds.read(1)
        # dBZ = 0.4*v - 30; 0 = clear, 255 = outside coverage
        mask = (v > 0) & (v < 255)
        dbz = np.where(mask, 0.4 * v.astype(np.float32) - 30.0, -100.0)
        rr = np.where(mask, (10.0 ** (dbz / 10.0) / 200.0) ** (1.0 / 1.6), 0.0)
        grid = _warp_rr_to_grid(rr, ds.transform, ds.crs)
    return stamp, _to_lev(grid)


def fetch_fmi():
    """Latest FMI rr composite -> (stamp, level grid) or None."""
    import rasterio
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
    body = s.get(urls[-1].replace("&amp;", "&"), timeout=120).content
    with rasterio.open(io.BytesIO(body)) as ds:
        v = ds.read(1).astype(np.float32)
        rr = np.where((v > 0) & (v < 65000), v / 100.0, 0.0)   # value/100 = mm/h
        grid = _warp_rr_to_grid(rr, ds.transform, ds.crs)
    return stamp, _to_lev(grid)


def _index_key(day):
    return f"{SRC_PREFIX}/{day:%Y/%m/%d}/index.json"


def _store_frame(s3, code, stamp, lev):
    """Level grid -> grayscale png in the radar_src store + day index entry."""
    key = f"{SRC_PREFIX}/{stamp:%Y/%m/%d}/{stamp:%Y%m%d%H%M}{code}.png"
    buf = io.BytesIO()
    Image.fromarray(lev, "L").save(buf, "PNG", optimize=True)
    s3.put_object(Bucket=config.R2_BUCKET, Key=key, Body=buf.getvalue(),
                  ContentType="image/png")
    ikey = _index_key(stamp.date())
    try:
        idx = json.loads(s3.get_object(Bucket=config.R2_BUCKET,
                                       Key=ikey)["Body"].read())
    except Exception:
        idx = {"frames": []}
    tstr = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    if not any(f.get("key") == key for f in idx["frames"]):
        idx["frames"].append({"key": key, "code": code, "time": tstr,
                              "grid3059": True, "enc": "lev",
                              "bytes": buf.getbuffer().nbytes})
        s3.put_object(Bucket=config.R2_BUCKET, Key=ikey,
                      Body=json.dumps(idx).encode(),
                      ContentType="application/json",
                      CacheControl="public, max-age=300")


def load_stored(s3, frame):
    """Day-index entry with grid3059 -> level grid."""
    body = s3.get_object(Bucket=config.R2_BUCKET,
                         Key=frame["key"])["Body"].read()
    return np.array(Image.open(io.BytesIO(body)).convert("L"), np.uint8)


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
