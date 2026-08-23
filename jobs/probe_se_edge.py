"""Generate the SE product-edge distance map for bundling.

SMHI's composite is a ~12-radar national product; judging the SE feed by
distance from the two antennas we list (Ase, Karlskrona) is the wrong
geometry and zeroed real echo. This warps the product's coverage footprint
(v = 255 marks outside) onto our grid and emits the km-to-edge distance map
(uint8, capped 255, 0 = outside coverage) to be committed as
wxfusion/data/se_edge_km.png. Throwaway probe (missing-probe.yml).
"""
import base64
import datetime as dt
import io
import sys

sys.path.insert(0, ".")

import numpy as np  # noqa: E402
import rasterio  # noqa: E402
from PIL import Image  # noqa: E402
from scipy.ndimage import distance_transform_edt  # noqa: E402

from wxfusion.http import session  # noqa: E402
from wxfusion.radar_nordic import SMHI_LIST, _warp_rr_to_grid  # noqa: E402

s = session()
day = dt.datetime.now(dt.timezone.utc).date()
files = s.get(SMHI_LIST.format(d=day), timeout=45).json()["files"]
tif = next(x["link"] for f in reversed(files)
           for x in f.get("formats", []) if x.get("key") == "tif")
with rasterio.open(io.BytesIO(s.get(tif, timeout=60).content)) as ds:
    v = ds.read(1)
    cov = _warp_rr_to_grid((v != 255).astype(np.float32),
                           ds.transform, ds.crs) > 0.5
edge = np.clip(distance_transform_edt(cov), 0, 255).astype(np.uint8)
print(f"coverage {int(cov.sum())} px, edge>0 {int((edge > 0).sum())} px, "
      f"max depth {int(edge.max())} km")
buf = io.BytesIO()
Image.fromarray(edge, "L").save(buf, "PNG", optimize=True)
print(f"@@IMG:se_edge_km:{base64.b64encode(buf.getvalue()).decode()}")
