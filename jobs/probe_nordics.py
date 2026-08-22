"""Evaluate SMHI + FMI radar as composite feeds: access (cadence, latency,
format, size) and classification (raw value encoding -> our intensity levels),
ending in a trial render of each on OUR 570x690 EPSG:3059 grid with borders.
Base64s the trials into the log. Throwaway."""
import base64
import datetime as dt
import io
import json
import re
import sys

import numpy as np
import rasterio
import requests
from PIL import Image, ImageDraw
from rasterio.warp import Resampling, reproject
from rasterio.transform import from_origin

sys.path.insert(0, ".")
from wxfusion import proj3059 as P  # noqa: E402

BORDERS = json.load(open("wxfusion/assets/borders_baltic.json"))
s = requests.Session()
s.headers["User-Agent"] = "baltic-wx-fusion feed evaluation"

DST_TR = from_origin(P.X0, P.Y1, 1000, 1000)          # our 1 km grid
# mm/h class edges for a 6-level trial (matches the spirit of our ramp)
EDGES = [0.1, 0.5, 1.0, 2.0, 5.0, 10.0]
COLS = [(200, 230, 176), (137, 199, 116), (87, 182, 71),
        (0, 140, 0), (255, 180, 60), (226, 86, 15)]


def dbz_to_rr(dbz):
    return (10.0 ** (dbz / 10.0) / 200.0) ** (1.0 / 1.6)   # Marshall-Palmer


def classify_rr(rr):
    lev = np.zeros(rr.shape, np.uint8)
    for i, e in enumerate(EDGES, start=1):
        lev[rr >= e] = i
    return lev


def render_ours(lev, name):
    im = Image.new("RGBA", (P.W, P.H), (251, 243, 222, 255))
    a = np.array(im)
    for i, c in enumerate(COLS, start=1):
        a[lev == i] = (*c, 255)
    im = Image.fromarray(a)
    d = ImageDraw.Draw(im)
    for cc in BORDERS:
        for line in cc["lines"]:
            pts = []
            for lo, la in line:
                x, y = P.to_xy(lo, la)
                pts.append(((x - P.X0) / 1000.0, (P.Y1 - y) / 1000.0))
            if len(pts) > 1:
                d.line(pts, fill=(255, 253, 244, 230), width=3)
                d.line(pts, fill=(70, 65, 50, 255), width=1)
    buf = io.BytesIO()
    im.convert("RGBA").quantize(colors=128).save(buf, "PNG", optimize=True)
    print(f"@@IMG:{name}:{base64.b64encode(buf.getvalue()).decode()}")


def warp_to_ours(band, src_transform, src_crs, src_nodata):
    dst = np.full((P.H, P.W), np.nan, np.float32)
    reproject(band.astype(np.float32), dst,
              src_transform=src_transform, src_crs=src_crs,
              src_nodata=src_nodata,
              dst_transform=DST_TR, dst_crs="EPSG:3059",
              dst_nodata=np.nan, resampling=Resampling.max)
    return dst


now = dt.datetime.now(dt.timezone.utc)

# ---------- SMHI ------------------------------------------------------------
try:
    r = s.get("https://opendata-download-radar.smhi.se/api/version/latest/"
              f"area/sweden/product/comp/{now:%Y/%m/%d}", timeout=60)
    r.raise_for_status()
    files = r.json().get("files") or []
    times = sorted(f.get("valid", "") for f in files)
    print(f"SMHI: {len(times)} frames today; first {times[:1]} last {times[-1:]}")
    if len(times) >= 3:
        t_last = dt.datetime.strptime(times[-1], "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc)
        t_prev = dt.datetime.strptime(times[-2], "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc)
        print(f"SMHI cadence: {(t_last-t_prev).seconds//60} min; "
              f"latency now: {(now-t_last).seconds//60} min")
    latest = max(files, key=lambda f: f.get("valid", ""))
    tif_url = next(f["link"] for f in latest["formats"] if f["key"] == "tif")
    t0 = dt.datetime.now()
    tifb = s.get(tif_url, timeout=90).content
    print(f"SMHI tif: {len(tifb)} B in {(dt.datetime.now()-t0).total_seconds():.1f}s")
    with rasterio.open(io.BytesIO(tifb)) as ds:
        print(f"SMHI grid: {ds.width}x{ds.height} {ds.crs} res {ds.res} "
              f"dtype {ds.dtypes} bands {ds.count} nodata {ds.nodata}")
        b = ds.read(1)
        vals, counts = np.unique(b, return_counts=True)
        print(f"SMHI band1: min {b.min()} max {b.max()} distinct {len(vals)}; "
              f"top vals {[(int(v), int(c)) for v, c in sorted(zip(vals, counts), key=lambda t: -t[1])[:6]]}")
        # Nordic 8-bit dBZ convention: dBZ = 0.4*v - 30 (BALTRAD); test both
        for k, off, sc in [("baltrad(0.4v-30)", -30.0, 0.4), ("half(0.5v-32)", -32.0, 0.5)]:
            hi = (b > 0) & (b < 255)
            if hi.any():
                dbz = off + sc * b[hi].astype(float)
                print(f"SMHI as {k}: dBZ range {dbz.min():.1f}..{dbz.max():.1f}")
        mask = (b > 0) & (b < 255)
        dbz = -30.0 + 0.4 * np.where(mask, b, 0).astype(np.float32)
        rr = np.where(mask, dbz_to_rr(dbz), 0.0)
        g = warp_to_ours(rr, ds.transform, ds.crs, None)
        lev = classify_rr(np.nan_to_num(g))
        print(f"SMHI trial on our grid: {int((lev>0).sum())} px echo, "
              f"per class {[int((lev==i).sum()) for i in range(1,7)]}")
        render_ours(lev, "trial_smhi")
except Exception as e:
    print(f"@@ERR:smhi: {type(e).__name__}: {e}")

# ---------- FMI -------------------------------------------------------------
try:
    # Stored query lists downloadable GeoTIFFs of the national composite.
    q = ("https://opendata.fmi.fi/wfs?service=WFS&version=2.0.0&request=getFeature"
         "&storedquery_id=fmi::radar::composite::rr")
    r = s.get(q, timeout=90)
    print(f"FMI wfs rr: HTTP {r.status_code} {len(r.content)} B")
    urls = re.findall(r"<gml:fileReference>\s*([^<]+?)\s*</gml:fileReference>", r.text)
    if not urls:
        urls = re.findall(r'(https://opendata\.fmi\.fi/download[^"<\s]+)', r.text)
    stamps = re.findall(r"<gml:timePosition>([^<]+)</gml:timePosition>", r.text)
    print(f"FMI: {len(urls)} rr grids listed; last stamps {stamps[-3:]}")
    if stamps:
        t_last = dt.datetime.fromisoformat(stamps[-1].replace("Z", "+00:00"))
        print(f"FMI latency now: {(now-t_last).total_seconds()/60:.0f} min")
    url = urls[-1].replace("&amp;", "&")
    t0 = dt.datetime.now()
    tifb = s.get(url, timeout=120).content
    print(f"FMI tif: {len(tifb)} B in {(dt.datetime.now()-t0).total_seconds():.1f}s")
    with rasterio.open(io.BytesIO(tifb)) as ds:
        print(f"FMI grid: {ds.width}x{ds.height} {ds.crs} res {ds.res} "
              f"dtype {ds.dtypes} bands {ds.count} nodata {ds.nodata}")
        b = ds.read(1).astype(np.float32)
        finite = b[(b > 0) & (b < 65000)]
        print(f"FMI band1: min {b.min():.0f} max {b.max():.0f}; "
              f"nonzero sample quantiles {np.percentile(finite, [50, 90, 99]).round(1).tolist() if finite.size else '—'}")
        # FMI rr GeoTIFF convention: value/100 = mm/h (16-bit), 65535 nodata
        rr = np.where((b > 0) & (b < 65000), b / 100.0, 0.0)
        print(f"FMI as rr/100: max {rr.max():.1f} mm/h")
        g = warp_to_ours(rr, ds.transform, ds.crs, None)
        lev = classify_rr(np.nan_to_num(g))
        print(f"FMI trial on our grid: {int((lev>0).sum())} px echo, "
              f"per class {[int((lev==i).sum()) for i in range(1,7)]}")
        render_ours(lev, "trial_fmi")
except Exception as e:
    print(f"@@ERR:fmi: {type(e).__name__}: {e}")

print("DONE")
