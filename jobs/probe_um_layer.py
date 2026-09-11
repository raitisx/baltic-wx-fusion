"""meteo.pl has been answering blank at every lead since 06.09.2026 — why?

Round one established the easy half: the server is up (GetCapabilities 200,
33 KB) and um:UM4_CLOUD is still in the layer list, so it is neither down
nor renamed. But GWC's capabilities is a terse listing with no TIME extent,
so it will not tell us which runs it holds.

So ask empirically. The scrape GUESSES the run — today 00Z or 12Z, then
back four cycles, 48 h in total — and everything it asks for is blank.
Three things worth knowing, in order of how much they would explain:

  A. no TIME at all. GeoServer serves the layer's default time, which is
     normally the newest granule. Data here means the layer is publishing
     fine and our TIME is simply wrong.
  B. how far back does it hold? Sweep 00Z/12Z over twelve days at a lead
     known to be good. The newest one with pixels IS the run to ask for,
     and its age says whether 48 h of walk-back is enough.
  C. the other endpoints — GeoServer's own WMS, and WMTS, which unlike the
     WMS capabilities does advertise dimension values.
"""
import datetime as dt
import re
import sys

sys.path.insert(0, ".")

import io  # noqa: E402

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from wxfusion import scrape_maps as S  # noqa: E402
from wxfusion.http import session  # noqa: E402

s = session()
LAYER = "um:UM4_CLOUD"
x0, x1, y0, y1 = S._tile_range(S.ZOOM)
bb = ",".join(f"{v:.9f}" for v in S._tile_bbox_900913(S.ZOOM, x0 + 1, y0 + 1))


def getmap(tval=None, lead=None, url=None):
    params = {"service": "WMS", "version": "1.1.1", "request": "GetMap",
              "layers": LAYER, "styles": "", "bbox": bb,
              "width": 256, "height": 256, "srs": "EPSG:900913",
              "format": "image/png", "transparent": "true", "tiled": "true"}
    if tval is not None:
        params["TIME"] = tval
    if lead is not None:
        params["DIM_FORECAST"] = lead
    try:
        r = s.get(url or S.GWC, params=params, timeout=60)
    except Exception as e:
        return f"{type(e).__name__}"
    ct = r.headers.get("content-type", "?")
    if not r.ok or "image" not in ct:
        body = re.sub(r"\s+", " ", (r.text or ""))[:200]
        return f"HTTP {r.status_code} {ct} {body}"
    a = np.array(Image.open(io.BytesIO(r.content)).convert("RGBA"))[..., 3]
    return f"{len(r.content):7,d} B  alpha {100 * (a > 0).mean():5.1f}%"


print("=== A. no TIME (server's default granule) ===")
print(f"  no TIME, no lead : {getmap()}")
for lead in (0, 1, 2, 4):
    print(f"  no TIME, +{lead:<3}      : {getmap(lead=lead)}")

print("\n=== B. which runs does it still hold? (lead 4) ===")
now = dt.datetime.now(dt.timezone.utc).replace(
    minute=0, second=0, microsecond=0, tzinfo=None)
cand = now.replace(hour=0 if now.hour < 12 else 12)
for i in range(24):                       # 12 days of 12-hourly cycles
    t = cand - dt.timedelta(hours=12 * i)
    tval = t.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    age = (now - t).total_seconds() / 3600
    print(f"  {tval}  ({age:5.0f} h old)  {getmap(tval, 4)}", flush=True)

print("\n=== C. other endpoints ===")
for name, url in (
        ("geoserver /wms", "https://mapy.meteo.pl/geoserver/wms"),
        ("geoserver /um/wms", "https://mapy.meteo.pl/geoserver/um/wms")):
    t = cand.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    print(f"  {name:<20} {getmap(t, 4, url=url)}")

for label, url in (
        ("WMS caps (geoserver)",
         "https://mapy.meteo.pl/geoserver/wms?service=WMS&version=1.3.0"
         "&request=GetCapabilities"),
        ("WMTS caps (gwc)",
         "https://mapy.meteo.pl/geoserver/gwc/service/wmts"
         "?service=WMTS&version=1.0.0&request=GetCapabilities")):
    try:
        r = s.get(url, timeout=180)
    except Exception as e:
        print(f"  {label}: {type(e).__name__}")
        continue
    print(f"  {label}: HTTP {r.status_code} {len(r.content):,} B")
    if not r.ok:
        continue
    m = re.search(r"<Layer[^>]*>(?:(?!</Layer>).)*?" + re.escape(LAYER)
                  + r"(?:(?!</Layer>).)*?</Layer>", r.text, re.S)
    if not m:
        print(f"    {LAYER} not in this document")
        continue
    blk = m.group(0)
    for d in re.findall(r"<(?:Dimension|Extent)[^>]*>(.*?)"
                        r"</(?:Dimension|Extent)>", blk, re.S):
        v = re.sub(r"\s+", " ", d).strip()
        print(f"    dim: {v[:400]}{'...' if len(v) > 400 else ''}")
