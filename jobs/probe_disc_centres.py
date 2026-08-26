"""Where does the straight edge in the SE composite come from?

A near-horizontal line runs across the Swedish product with echo above it
and nothing below. That is either theirs (the product's own raster extent or
its coverage mask) or ours (the warp, the fade, the canvas). The source is
discarded at decode, so fetch one live composite and look at it directly.
"""
import io
import sys

sys.path.insert(0, ".")

import numpy as np  # noqa: E402
import rasterio  # noqa: E402

from wxfusion import proj3059 as P, radar_nordic as N  # noqa: E402

s = N.session()
import datetime as dt  # noqa: E402
day = dt.datetime.now(dt.timezone.utc).date()
r = s.get(N.SMHI_LIST.format(d=day), timeout=45)
files = r.json().get("files") or []
tif = None
for f in reversed(files):
    tif = next((x["link"] for x in f.get("formats", []) if x.get("key") == "tif"),
               None)
    if tif:
        print("sweep:", f["valid"])
        break
body = s.get(tif, timeout=120).content

with rasterio.open(io.BytesIO(body)) as ds:
    v = ds.read(1)
    print(f"native grid {ds.width}x{ds.height} {ds.crs}")
    print("bounds:", [round(b) for b in ds.bounds])
    print("value counts: 0 (clear)", int((v == 0).sum()),
          " 255 (no coverage)", int((v == 255).sum()),
          " data", int(((v > 0) & (v < 255)).sum()))
    cov_native = v != 255
    # A straight edge in the NATIVE grid shows up as a row/column where the
    # covered count changes abruptly.
    rows = cov_native.sum(1)
    jumps = [(i, int(rows[i - 1]), int(rows[i]))
             for i in range(1, len(rows))
             if abs(int(rows[i]) - int(rows[i - 1])) > ds.width // 20]
    print(f"native rows with a big coverage jump: {len(jumps)}")
    for i, a, b in jumps[:8]:
        y = ds.bounds.top + i * ds.transform.e
        print(f"  row {i:5d} (north {y:,.0f} m): {a:5d} -> {b:5d} px covered")
    cov = N._warp_rr_to_grid(cov_native.astype(np.float32),
                             ds.transform, ds.crs) > 0.5

print(f"\non our canvas: {int(cov.sum()):,} of {P.W * P.H:,} px covered")
rows = cov.sum(1)
print("row (lat)      covered px      what changes")
prev = None
for j in range(P.H):
    n = int(rows[j])
    if prev is not None and abs(n - prev) > 25:
        lat = P.to_lonlat((P.X0 + P.X1) / 2,
                          P.Y1 - j * (P.Y1 - P.Y0) / P.H)[1]
        print(f"  y={j:3d} ({lat:5.2f} N)  {prev:5d} -> {n:5d}")
    prev = n

print("\nedges of the covered area, every 40th row:")
for j in range(0, P.H, 40):
    xs = np.flatnonzero(cov[j])
    if xs.size:
        print(f"  y={j:3d}  x {xs.min():3d}..{xs.max():3d}  ({xs.size} px)")
    else:
        print(f"  y={j:3d}  none")
