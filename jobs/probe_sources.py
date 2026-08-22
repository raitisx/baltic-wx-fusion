"""One-off: current image from every radar source — ours (R2), SMHI, FMI,
IMGW (Gdansk/POLRAD, discovery), EUMETNET ORD (grid extraction attempt) —
with country borders drawn on each, base64'd into the log. Throwaway."""
import base64
import datetime as dt
import io
import json
import os
import re
import sys

import boto3
import requests
from PIL import Image, ImageDraw

BORDERS = json.load(open("wxfusion/assets/borders_baltic.json"))


def draw_borders(im, to_px):
    """to_px(lon, lat) -> (x, y) or None. White casing + dark core."""
    d = ImageDraw.Draw(im)
    for c in BORDERS:
        for line in c["lines"]:
            pts = [to_px(lo, la) for lo, la in line]
            pts = [p for p in pts if p]
            if len(pts) > 1:
                d.line(pts, fill=(255, 253, 244, 230), width=3)
                d.line(pts, fill=(70, 65, 50, 255), width=1)
    return im


def emit(name, im, max_px=640):
    if max(im.size) > max_px:
        sc = max_px / max(im.size)
        im = im.resize((int(im.width * sc), int(im.height * sc)))
    buf = io.BytesIO()
    im.convert("RGBA").quantize(colors=192).save(buf, "PNG", optimize=True)
    print(f"@@IMG:{name}:{base64.b64encode(buf.getvalue()).decode()}")


s = requests.Session()
s.headers["User-Agent"] = "baltic-wx-fusion probe (weather radar survey)"

# --- ours (borders already on it) -----------------------------------------
try:
    s3 = boto3.client("s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"])
    arch = json.loads(s3.get_object(Bucket=os.environ["R2_BUCKET"],
                                    Key="maps/radar/archive.json")["Body"].read())
    vtag = arch["hours"][max(arch["hours"])]
    body = s3.get_object(Bucket=os.environ["R2_BUCKET"],
                         Key=f"maps/radar/frames3059/{vtag}.png")["Body"].read()
    emit("ours_" + vtag, Image.open(io.BytesIO(body)))
except Exception as e:
    print(f"@@ERR:ours: {type(e).__name__}: {e}")

# --- FMI (EPSG:4326 WMS -> equirectangular border mapping) -----------------
try:
    S, WLON, N, ELON, W, H = 56.0, 19.0, 61.5, 29.5, 630, 660
    wms = ("https://openwms.fmi.fi/geoserver/Radar/wms?service=WMS&version=1.3.0"
           "&request=GetMap&layers=Radar:suomi_dbz_eureffin&styles="
           f"&crs=EPSG:4326&bbox={S},{WLON},{N},{ELON}"
           f"&width={W}&height={H}&format=image/png&transparent=false")
    img = s.get(wms, timeout=90); img.raise_for_status()
    im = Image.open(io.BytesIO(img.content)).convert("RGBA")
    def fmi_px(lo, la):
        if not (WLON <= lo <= ELON and S <= la <= N):
            return None
        return ((lo - WLON) / (ELON - WLON) * W, (N - la) / (N - S) * H)
    emit("fmi_suomi_dbz", draw_borders(im, fmi_px))
except Exception as e:
    print(f"@@ERR:fmi: {type(e).__name__}: {e}")

# --- SMHI (png colours + tif georeference) ---------------------------------
try:
    import rasterio
    from pyproj import Transformer
    today = dt.datetime.now(dt.timezone.utc)
    r = s.get("https://opendata-download-radar.smhi.se/api/version/latest/"
              f"area/sweden/product/comp/{today:%Y/%m/%d}", timeout=60)
    r.raise_for_status()
    latest = max(r.json().get("files") or [], key=lambda f: f.get("valid", ""))
    fmts = {f["key"]: f["link"] for f in latest.get("formats", [])}
    png = Image.open(io.BytesIO(s.get(fmts["png"], timeout=60).content)).convert("RGBA")
    tifb = s.get(fmts["tif"], timeout=60).content
    with rasterio.open(io.BytesIO(tifb)) as ds:
        tr, crs, tw, th = ds.transform, ds.crs, ds.width, ds.height
    print(f"smhi tif: {tw}x{th} crs={crs}")
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    sx, sy = png.width / tw, png.height / th
    def smhi_px(lo, la):
        x, y = fwd.transform(lo, la)
        col, row = ~tr * (x, y)
        if not (0 <= col < tw and 0 <= row < th):
            return None
        return (col * sx, row * sy)
    emit("smhi_" + str(latest.get("valid", "x"))[:16].replace(":", "").replace(" ", "_"),
         draw_borders(png, smhi_px))
except Exception as e:
    print(f"@@ERR:smhi: {type(e).__name__}: {e}")

# --- IMGW (Poland, incl. Gdansk) — endpoint discovery ----------------------
imgw_found = False
for url, tag in [
    ("https://meteo.imgw.pl/api/map/radar,cmax", "api_map"),
    ("https://meteo.imgw.pl/api/map/radar,cmax/latest", "api_map_latest"),
    ("https://danepubliczne.imgw.pl/datastore", "datastore"),
]:
    try:
        r = s.get(url, timeout=45)
        ct = r.headers.get("content-type", "")
        print(f"imgw {tag}: HTTP {r.status_code} {ct} {len(r.content)} B")
        if "image" in ct:
            emit(f"imgw_{tag}", Image.open(io.BytesIO(r.content)))
            imgw_found = True
        elif r.ok:
            head = r.text[:600].replace("\n", " ")
            print(f"imgw {tag} head: {head}")
            # If JSON listing with timestamps, try to fetch the newest image.
            try:
                j = r.json()
                print(f"imgw {tag} json keys/sample: "
                      + json.dumps(j)[:400])
            except Exception:
                # HTML: surface hrefs that mention radar/compo
                links = re.findall(r'href="([^"]*(?:adar|ompo|POLCOMP|cmax)[^"]*)"',
                                   r.text, re.I)[:10]
                print(f"imgw {tag} links: {links}")
    except Exception as e:
        print(f"@@ERR:imgw_{tag}: {type(e).__name__}: {e}")

# --- EUMETNET ORD — try an anonymous grid extraction -----------------------
ORD = "https://api.meteogate.eu/eu-eumetnet-weather-radar"
now = dt.datetime.now(dt.timezone.utc)
t0 = (now - dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:00:00Z")
t1 = now.strftime("%Y-%m-%dT%H:%M:00Z")
for url, tag in [
    (f"{ORD}/collections/observations/cube?bbox=19,54,30,60"
     f"&parameter-name=RATE:comp&datetime={t0}/{t1}", "cube"),
    (f"{ORD}/collections/observations/area?coords=POLYGON((19 54,30 54,30 60,19 60,19 54))"
     f"&parameter-name=RATE:comp&datetime={t0}/{t1}", "area"),
]:
    try:
        r = s.get(url, timeout=90)
        ct = r.headers.get("content-type", "")
        print(f"ord {tag}: HTTP {r.status_code} {ct} {len(r.content)} B")
        if r.ok and "json" in ct:
            j = r.json()
            print(f"ord {tag} keys: {list(j)[:8]}")
            dom = j.get("domain", {})
            ax = dom.get("axes", {})
            xs, ys = ax.get("x", {}).get("values"), ax.get("y", {}).get("values")
            rng = next(iter(j.get("ranges", {}).values()), {})
            vals = rng.get("values")
            if xs and ys and vals:
                import numpy as np
                arr = np.array([v if v is not None else 0.0 for v in vals],
                               dtype=float)
                # take the last time step if 3-D
                nt = max(1, arr.size // (len(xs) * len(ys)))
                g = arr[-len(xs) * len(ys):].reshape(len(ys), len(xs))
                mx = max(1e-6, float(g.max()))
                im = Image.new("RGBA", (len(xs), len(ys)), (251, 243, 222, 255))
                px = im.load()
                for yy in range(len(ys)):
                    for xx in range(len(xs)):
                        v = g[yy, xx]
                        if v > 0.05:
                            k = min(1.0, v / mx)
                            px[xx, yy] = (int(40 + 30 * (1 - k)), int(160 - 90 * k), 40, 255)
                im = im.resize((len(xs) * 4, len(ys) * 4), Image.NEAREST)
                W2, H2 = im.size
                lo0, lo1 = min(xs), max(xs); la0, la1 = min(ys), max(ys)
                def ord_px(lo, la):
                    if not (lo0 <= lo <= lo1 and la0 <= la <= la1):
                        return None
                    return ((lo - lo0) / (lo1 - lo0) * W2, (la1 - la) / (la1 - la0) * H2)
                emit(f"eumetnet_{tag}", draw_borders(im, ord_px))
                break
        else:
            print(f"ord {tag} head: {r.text[:300]}")
    except Exception as e:
        print(f"@@ERR:ord_{tag}: {type(e).__name__}: {e}")

print("DONE")
