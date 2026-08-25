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
