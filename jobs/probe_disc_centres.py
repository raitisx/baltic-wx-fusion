"""Why are leads 0, 3, 6 and 12 blank when their neighbours are full?

A sweep found those four empty in every published UM4_CLOUD run while
+1/+2/+4/+9/+18/+24/+36/+48 come back 92-99% covered. That pattern is not a
model gap — it has no arithmetic to it — it looks like GeoWebCache holding
blank tiles for those DIM_FORECAST values, cached while the run was still
being written and never invalidated.

If so, asking GeoServer directly (tiled=false, so the request goes past the
cache) returns real data for the same lead. That is testable, and it decides
whether those hours can be recovered or are simply missing upstream.
"""
import datetime as dt
import sys

sys.path.insert(0, ".")

import numpy as np  # noqa: E402

from wxfusion import scrape_maps as S  # noqa: E402
from wxfusion.http import session  # noqa: E402
from PIL import Image  # noqa: E402
import io  # noqa: E402

s = session()
now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0,
                                               microsecond=0, tzinfo=None)
run = now.replace(hour=0 if now.hour < 12 else 12) - dt.timedelta(hours=12)
print(f"run {run:%Y-%m-%dT%H}Z, one tile of the grid\n")
x0, x1, y0, y1 = S._tile_range(S.ZOOM)
bb = S._tile_bbox_900913(S.ZOOM, x0 + 1, y0 + 1)

def ask(lead, tiled, direct=False):
    params = {"service": "WMS", "version": "1.1.1", "request": "GetMap",
              "layers": "um:UM4_CLOUD", "styles": "",
              "bbox": ",".join(f"{v:.9f}" for v in bb),
              "width": 256, "height": 256, "srs": "EPSG:900913",
              "format": "image/png", "transparent": "true",
              "TIME": run.strftime("%Y-%m-%dT%H:%M:%S") + "Z",
              "DIM_FORECAST": lead}
    if tiled:
        params["tiled"] = "true"
    url = ("https://mapy.meteo.pl/geoserver/um/wms" if direct else S.GWC)
    try:
        r = s.get(url, params=params, timeout=60)
    except Exception as e:
        return f"{type(e).__name__}"
    if not r.ok or "image" not in r.headers.get("content-type", ""):
        return f"HTTP {r.status_code} {r.headers.get('content-type','?')}"
    a = np.array(Image.open(io.BytesIO(r.content)).convert("RGBA"))[..., 3]
    return f"{len(r.content):6,d} B  alpha {100 * (a > 0).mean():5.1f}%"

print(f"{'lead':>5}  {'GWC tiled=true':>28}  {'GWC no tiled':>28}  "
      f"{'GeoServer /um/wms':>28}")
for lead in (0, 1, 3, 6, 9, 12, 15, 21):
    print(f"{lead:>5}  {ask(lead, True):>28}  {ask(lead, False):>28}  "
          f"{ask(lead, False, direct=True):>28}")
