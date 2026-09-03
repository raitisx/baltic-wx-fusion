"""meteo.pl has published nothing for 48 h. Are they down, or did they move?

The scraper judges a run published by fetching um:UM4_CLOUD at lead 6, 3 or
12 and looking for any non-transparent pixel. All three have come back empty
for four cycles. That is either an outage, a layer rename, or ICM leaving
even more of the early leads blank than the mid-August change did.

So ask the server what it has, and sweep the leads instead of trusting
three of them.
"""
import datetime as dt
import re
import sys

sys.path.insert(0, ".")

import numpy as np  # noqa: E402

from wxfusion import scrape_maps as S  # noqa: E402
from wxfusion.http import session  # noqa: E402

s = session()

print("=== what layers does the GeoServer advertise? ===")
for svc, url in (("WMS 1.1.1", f"{S.GWC}?service=WMS&version=1.1.1"
                               "&request=GetCapabilities"),
                 ("WMTS", "https://mapy.meteo.pl/geoserver/gwc/service/wmts"
                          "?service=WMTS&request=GetCapabilities")):
    try:
        r = s.get(url, timeout=90)
    except Exception as e:
        print(f"  {svc}: {type(e).__name__}: {e}")
        continue
    print(f"  {svc}: HTTP {r.status_code} "
          f"{r.headers.get('content-type','?')} {len(r.content):,} B")
    if not r.ok:
        print("   ", r.text[:200].replace("\n", " "))
        continue
    names = re.findall(r"<(?:ows:)?(?:Name|Title)>([^<]+)</", r.text)
    um = sorted({n for n in names if re.search(r"UM|CLOUD|RAIN", n, re.I)})
    print(f"    {len(names)} names, {len(um)} looking like UM:")
    for n in um[:30]:
        print("     ", n)

print("\n=== is there ANY data, at any lead, in the recent runs? ===")
now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0,
                                               microsecond=0, tzinfo=None)
cand = now.replace(hour=0 if now.hour < 12 else 12)
LEADS = (0, 1, 2, 3, 4, 6, 9, 12, 18, 24, 36, 48)
for back in range(5):
    run = cand - dt.timedelta(hours=12 * back)
    hits = []
    for pl in LEADS:
        try:
            p = S.fetch_um_frame("um:UM4_CLOUD", run, pl)
        except Exception as e:
            hits.append(f"+{pl}:{type(e).__name__}")
            continue
        if p is None:
            hits.append(f"+{pl}:none")
            continue
        a = np.array(p)[..., 3]
        hits.append(f"+{pl}:{100 * (a > 0).mean():.0f}%")
    print(f"  run {run:%Y-%m-%dT%H}Z  " + "  ".join(hits))

print("\n=== what does one tile request actually return? ===")
x0, x1, y0, y1 = S._tile_range(S.ZOOM)
bb = S._tile_bbox_900913(S.ZOOM, x0, y0)
for lead in (6, 24):
    params = {"service": "WMS", "version": "1.1.1", "request": "GetMap",
              "layers": "um:UM4_CLOUD", "styles": "",
              "bbox": ",".join(f"{v:.9f}" for v in bb),
              "width": 256, "height": 256, "srs": "EPSG:900913",
              "format": "image/png", "transparent": "true", "tiled": "true",
              "TIME": cand.strftime("%Y-%m-%dT%H:%M:%S") + "Z",
              "DIM_FORECAST": lead}
    try:
        r = s.get(S.GWC, params=params, timeout=60)
        print(f"  lead +{lead}: HTTP {r.status_code} "
              f"{r.headers.get('content-type','?')} {len(r.content):,} B")
        if "xml" in r.headers.get("content-type", "") or r.content[:1] == b"<":
            print("   ", re.sub(r"\s+", " ", r.text)[:400])
    except Exception as e:
        print(f"  lead +{lead}: {type(e).__name__}: {e}")

print("\n=== does the site itself say anything? ===")
for u in ("https://mapy.meteo.pl/", "https://www.meteo.pl/"):
    try:
        r = s.get(u, timeout=60)
        body = re.sub(r"<[^>]+>", " ", r.text)
        body = re.sub(r"\s+", " ", body).strip()
        print(f"  {u} HTTP {r.status_code} {len(r.content):,} B")
        print("   ", body[:300])
    except Exception as e:
        print(f"  {u}: {type(e).__name__}: {e}")
