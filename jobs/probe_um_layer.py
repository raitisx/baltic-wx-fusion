"""meteo.pl has been answering blank at every lead for days — why?

Since 06.09.2026 every um-maps run has ended in "no published run found in
the last 4 cycles": all six probe leads blank across 48 h of cycles. The
scrape GUESSES which run to ask for (today 00Z or 12Z, then back four
cycles), so it cannot tell "ICM stopped publishing" apart from "ICM is
publishing something we are not asking for".

The capabilities document knows. It lists the layers that exist and, for
each, the exact TIME and DIM_FORECAST values the server will honour. So ask
it rather than guessing, then fetch a frame at a time it actually
advertises. That separates the three possibilities:

  * server unreachable / erroring     -> it is down, nothing to fix here
  * um:UM4_CLOUD gone or renamed      -> follow the layer
  * layer there, TIME extent moved    -> our run guess is wrong, and the
                                         advertised value is the fix
"""
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
block = ""

for ver in ("1.1.1", "1.3.0"):
    url = (f"{S.GWC}?service=WMS&version={ver}&request=GetCapabilities")
    try:
        r = s.get(url, timeout=120)
    except Exception as e:
        print(f"GetCapabilities {ver}: {type(e).__name__}: {e}")
        continue
    print(f"GetCapabilities {ver}: HTTP {r.status_code} "
          f"{r.headers.get('content-type', '?')} {len(r.content):,} B")
    if not r.ok:
        print(r.text[:500])
        continue
    xml = r.text
    names = re.findall(r"<Name>([^<]*UM[^<]*)</Name>", xml)
    print(f"  layers matching UM ({len(names)}): {names[:40]}")
    # The block for our layer, if it is still there.
    m = re.search(r"<Layer[^>]*>(?:(?!</Layer>).)*?"
                  + re.escape(LAYER) + r"(?:(?!</Layer>).)*?</Layer>",
                  xml, re.S)
    if not m:
        print(f"  {LAYER}: NOT PRESENT")
        continue
    block = m.group(0)
    for tag in ("Dimension", "Extent"):
        for d in re.findall(rf"<{tag}[^>]*>(.*?)</{tag}>", block, re.S):
            head = d.strip()
            print(f"  <{tag}> {head[:300]}{'...' if len(head) > 300 else ''}")
    for d in re.findall(r"<(?:Dimension|Extent)[^>]*/>", block):
        print(f"  {d[:300]}")
    break

# Whatever TIME values the server admits to, try the newest few.
print("\n--- GetMap at advertised times ---")
times = []
if block:
    for d in re.findall(r"<(?:Dimension|Extent)[^>]*name=\"time\"[^>]*>"
                        r"(.*?)</(?:Dimension|Extent)>", block, re.S):
        times = [t.strip() for t in d.split(",") if t.strip()]
if not times:
    print("no TIME extent advertised — falling back to the scrape's guess")

x0, x1, y0, y1 = S._tile_range(S.ZOOM)
bb = S._tile_bbox_900913(S.ZOOM, x0 + 1, y0 + 1)


def getmap(tval, lead):
    params = {"service": "WMS", "version": "1.1.1", "request": "GetMap",
              "layers": LAYER, "styles": "",
              "bbox": ",".join(f"{v:.9f}" for v in bb),
              "width": 256, "height": 256, "srs": "EPSG:900913",
              "format": "image/png", "transparent": "true", "tiled": "true",
              "TIME": tval, "DIM_FORECAST": lead}
    try:
        r = s.get(S.GWC, params=params, timeout=60)
    except Exception as e:
        return f"{type(e).__name__}"
    if not r.ok or "image" not in r.headers.get("content-type", ""):
        body = re.sub(r"\s+", " ", r.text[:160]) if r.text else ""
        return f"HTTP {r.status_code} {body}"
    a = np.array(Image.open(io.BytesIO(r.content)).convert("RGBA"))[..., 3]
    return f"{len(r.content):7,d} B  alpha {100 * (a > 0).mean():5.1f}%"


for tval in times[-4:] or []:
    for lead in (2, 4, 9, 18):
        print(f"  TIME={tval:<28} +{lead:<3} {getmap(tval, lead)}")
