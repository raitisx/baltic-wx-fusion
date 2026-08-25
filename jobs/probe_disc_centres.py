"""Is Estonia's product one Harku disc, or Harku plus Surgavere?

The user recalls Harku appearing as a clean separate disc while Surgavere
looked merged — and the arc fitting agrees: the strongest circle on nine of
ten frames was Harku-centred, Surgavere-centred only once. If the product is
in fact Harku alone, then the "Surgavere crescent" is empty by construction
and every number measured in it is meaningless.

That is decidable from the painted extent, not from where the rain happens to
be. For each sweep this counts the product's painted pixels that:
  - lie inside 250 km of Harku (Harku alone could explain them),
  - lie OUTSIDE 250 km of Harku (only Surgavere can),
  - lie outside 250 km of Surgavere (only Harku can),
and reports the furthest painted pixel from each dish.
"""
import datetime as dt
import sys

sys.path.insert(0, ".")

import numpy as np  # noqa: E402

from wxfusion import proj3059 as P, radar_store as R  # noqa: E402

s3 = R.r2_client()
DAY = dt.date(2026, 8, 22)
yy, xx = np.mgrid[0:P.H, 0:P.W]
D = {n: np.hypot(xx - sx, yy - sy)
     for n, sx, sy in R._site_pixels_for("EE", on_canvas=True)}
HAR, SUR = D["EE Harku"], D["EE Surgavere"]
print("sites:", {n: (round(float(v.min()), 1)) for n, v in D.items()},
      "(min distance on canvas)")

idx = R._load_index(s3, DAY)
ee = sorted((f for f in idx.get("frames", [])
             if f.get("code") == "EE" and not f.get("grid3059")),
            key=lambda f: f["time"])
print(f"{len(ee)} EE sweeps stored on {DAY}\n")
print(f"{'sweep':6s} {'painted':>9s} {'>250km from Harku':>19s} "
      f"{'>250 from Surgavere':>21s} {'furthest: Harku':>17s} {'Surgavere':>11s}")

for f in ee[::max(1, len(ee) // 8)][:8]:
    try:
        _, raw = R._feed_grid(s3, "EE", f)
    except Exception as e:
        print(f"  {f['time']}: {type(e).__name__}")
        continue
    if raw is None:
        print(f"  {f['time']}: no raw kept")
        continue
    painted = raw[..., 3] > 60
    n = int(painted.sum())
    only_sur = int((painted & (HAR > 250)).sum())
    only_har = int((painted & (SUR > 250)).sum())
    fh = float(HAR[painted].max()) if n else 0
    fs = float(SUR[painted].max()) if n else 0
    t = f["time"][11:16]
    print(f"{t:6s} {n:9,d} {only_sur:12,d} ({100 * only_sur / max(1, n):4.1f}%)"
          f" {only_har:13,d} ({100 * only_har / max(1, n):4.1f}%)"
          f" {fh:14.0f} km {fs:8.0f} km")

print("\nIf the product were Harku alone, the '>250 km from Harku' column "
      "would be ~0\nand the furthest painted pixel from Harku would sit at "
      "its range, ~245 km.")

# ---- and the question that decides how to read all of it: is it CLIPPED?
# Our canvas stops at 28.6E and 59.9N; the product's own image stops
# wherever meteolapa cropped it. An arc that runs off either edge cannot be
# fitted, which is exactly how one dish ends up looking like a clean disc
# and the other like a merge.
import io  # noqa: E402
import math  # noqa: E402

from PIL import Image  # noqa: E402

from wxfusion import config  # noqa: E402

print("\n=== clipping ===")
f = ee[len(ee) // 2]
(s_lat, s_lon), (n_lat, n_lon) = tuple(f["sw"]), tuple(f["ne"])
print(f"product bounds  {s_lat:.3f}..{n_lat:.3f} N   {s_lon:.3f}..{n_lon:.3f} E")
print(f"our canvas      {53.8:.3f}..{59.9:.3f} N   {20.0:.3f}..{28.6:.3f} E")
for name, la, lo, rng in (("Harku", 59.398, 24.603, 245),
                          ("Surgavere", 58.482, 25.519, 245)):
    dlat = rng / 111.32
    dlon = rng / (111.32 * math.cos(math.radians(la)))
    print(f"  {name:10s} disc reaches {la - dlat:.2f}..{la + dlat:.2f} N  "
          f"{lo - dlon:.2f}..{lo + dlon:.2f} E")
    print(f"{'':13s}south arc {'INSIDE' if la - dlat > s_lat else 'CUT'} "
          f"the product's own bottom edge ({s_lat:.2f} N), "
          f"east arc {'inside' if lo + dlon < 28.6 else 'CUT'} our canvas")

body = s3.get_object(Bucket=config.R2_BUCKET, Key=f["key"])["Body"].read()
a = np.array(Image.open(io.BytesIO(body)).convert("RGBA"))
pnt = a[..., 3] > 60
H, W = pnt.shape
edges = {"top": int(pnt[0].sum()), "bottom": int(pnt[-1].sum()),
         "left": int(pnt[:, 0].sum()), "right": int(pnt[:, -1].sum())}
print(f"\n  source image {W}x{H}, painted {int(pnt.sum()):,} px")
print("  painted pixels touching each edge of the SOURCE image:", edges)
print("  (a non-zero edge means the product itself is cut there)")

