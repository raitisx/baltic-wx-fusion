"""One-off diagnostic for the stalled meteo.pl UM scrape.

Runs in GitHub Actions (which has open egress, unlike the dev sandbox) and
answers one question: why does every recent cycle come back empty? It hits the
GWC GetCapabilities, then replays the exact tile request the scraper makes for
the last several 00Z/12Z cycles, printing status / content-type / size / alpha
so we can tell "meteo.pl has no data for this run" from "the layer/params
changed" from "they're refusing us".

Self-contained on purpose — it does NOT import wxfusion.scrape_maps (that pulls
in the heavy map stack); it copies the handful of constants it needs.
"""
import datetime as dt
import io
import math
import re
import sys

import numpy as np
import requests
from PIL import Image

GWC = "https://mapy.meteo.pl/geoserver/gwc/service/wms"
ZOOM = 6
WORLD = 20037508.342789244
BBOX = (53.8, 59.9, 20.0, 28.6)          # lat_min, lat_max, lon_min, lon_max
LAYER = "um:UM4_CLOUD"
UA = {"User-Agent": "baltic-wx-fusion diagnostic (raitis.upmalis@gmail.com)"}


def tile_bbox(z, x, y):
    n = 2 ** z
    size = 2 * WORLD / n
    minx = -WORLD + x * size
    maxy = WORLD - y * size
    return (minx, maxy - size, minx + size, maxy)


def tile_range(z):
    n = 2 ** z
    def lon2x(lon): return int((lon + 180) / 360 * n)
    def lat2y(lat):
        r = math.radians(lat)
        return int((1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * n)
    return (lon2x(BBOX[2]), lon2x(BBOX[3]), lat2y(BBOX[1]), lat2y(BBOX[0]))


def probe_capabilities():
    print("=== GetCapabilities ===", flush=True)
    try:
        r = requests.get(GWC, params={"service": "WMS", "version": "1.1.1",
                                      "request": "GetCapabilities"},
                         headers=UA, timeout=90)
    except Exception as e:
        print(f"  ERROR {type(e).__name__}: {e}", flush=True)
        return
    print(f"  HTTP {r.status_code}  {r.headers.get('content-type')}  "
          f"{len(r.content)} bytes", flush=True)
    if r.status_code != 200:
        print("  body head:", r.text[:400], flush=True)
        return
    txt = r.text
    names = re.findall(r"<Name>([^<]*)</Name>", txt)
    um = [n for n in names if "UM" in n.upper()]
    print(f"  {len(names)} layers total; UM-ish names:", flush=True)
    for n in um[:40]:
        print("    ", n, flush=True)
    # dimensions on the UM4_CLOUD layer, if present
    i = txt.find("UM4_CLOUD")
    if i < 0:
        print("  !! UM4_CLOUD not found in capabilities — layer renamed?", flush=True)
        return
    seg = txt[max(0, i - 400): i + 2000]
    for dim in re.findall(r'<(?:Dimension|Extent)\b[^>]*\bname="([^"]+)"[^>]*>'
                          r'([^<]*)</(?:Dimension|Extent)>', seg):
        print(f"  UM4 {dim[0]}: {dim[1][:160]}", flush=True)


def probe_tile(run, lead):
    """One raw tile request — the scraper's exact params — reporting verbatim."""
    x0, x1, y0, y1 = tile_range(ZOOM)
    bb = tile_bbox(ZOOM, x0, y0)
    params = {
        "service": "WMS", "version": "1.1.1", "request": "GetMap",
        "layers": LAYER, "styles": "",
        "bbox": ",".join(f"{v:.9f}" for v in bb),
        "width": 256, "height": 256, "srs": "EPSG:900913",
        "format": "image/png", "transparent": "true", "tiled": "true",
        "TIME": run.strftime("%Y-%m-%dT%H:%M:%S") + "Z",
        "DIM_FORECAST": lead,
    }
    try:
        r = requests.get(GWC, params=params, headers=UA, timeout=90)
    except Exception as e:
        return f"ERROR {type(e).__name__}: {e}"
    ct = r.headers.get("content-type", "")
    tag = f"HTTP {r.status_code} {ct} {len(r.content)}B"
    if r.status_code == 200 and ct.startswith("image"):
        try:
            a = np.array(Image.open(io.BytesIO(r.content)).convert("RGBA"))
            alpha = bool(a[..., 3].any())
            opaque = int((a[..., 3] > 0).sum())
            return f"{tag}  alpha_any={alpha}  opaque_px={opaque}"
        except Exception as e:
            return f"{tag}  (decode failed: {e})"
    # not an image — meteo.pl usually returns a ServiceException XML here
    return f"{tag}\n      body: {r.text[:300]}"


def main() -> int:
    now = dt.datetime.now(dt.timezone.utc)
    print(f"now = {now.isoformat()}\n", flush=True)
    probe_capabilities()
    print("\n=== tile probe (scraper's exact request), lead +1h ===", flush=True)
    run = now.replace(minute=0, second=0, microsecond=0,
                      hour=0 if now.hour < 12 else 12)
    for _ in range(6):
        print(f"  TIME={run:%Y-%m-%dT%H}:00Z  ->  {probe_tile(run, 1)}", flush=True)
        run -= dt.timedelta(hours=12)
    # a couple of variants in case the layer name / prefix changed
    print("\n=== variants on the freshest cycle ===", flush=True)
    run = now.replace(minute=0, second=0, microsecond=0,
                      hour=0 if now.hour < 12 else 12)
    for layer in ("UM4_CLOUD", "um:UM4_CLOUD", "meteo:UM4_CLOUD"):
        globals()["LAYER"] = layer
        print(f"  layer={layer:20s} -> {probe_tile(run, 1)}", flush=True)

    # Rule out "only the analysis hour is blank": sweep several leads on the two
    # freshest cycles. If a later lead has data, the fix is to probe a mid-lead,
    # not lead +1h; if every lead is empty, it's purely an upstream data outage.
    globals()["LAYER"] = "um:UM4_CLOUD"
    print("\n=== lead sweep on the two freshest cycles ===", flush=True)
    for hoff in (0, 12):
        c = (now - dt.timedelta(hours=hoff)).replace(minute=0, second=0,
                                                     microsecond=0,
                                                     hour=0 if (now - dt.timedelta(hours=hoff)).hour < 12 else 12)
        for lead in (0, 1, 2, 3, 6, 12, 24, 48):
            print(f"  TIME={c:%Y-%m-%dT%H}:00Z +{lead:>2}h -> {probe_tile(c, lead)}",
                  flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
