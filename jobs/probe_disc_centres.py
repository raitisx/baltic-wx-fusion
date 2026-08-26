"""Is the straight edge in the SE panel theirs, or ours?

The product's own footprint is a smooth curve (471x887 at 2 km, EPSG:25833),
so a straight horizontal cut is not their coverage boundary. Take the stored
SE frame for the slot in question and, row by row, compare where the ECHO
stops against where the COVERAGE stops. Echo ending at the coverage wall is
theirs; echo ending well inside it is ours — the fade, the cleaning, or the
warp.
"""
import datetime as dt
import io
import sys

sys.path.insert(0, ".")

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from wxfusion import config, proj3059 as P, radar_store as R  # noqa: E402

s3 = R.r2_client()

# The footprint as the composite stores it (km to the coverage edge).
edge = None
try:
    body = s3.get_object(Bucket=config.R2_BUCKET,
                         Key="maps/radar_src/se_edge_km.png")["Body"].read()
    edge = np.array(Image.open(io.BytesIO(body)).convert("L"), np.uint8)
    print(f"edge map {edge.shape}, covered (>0): {int((edge > 0).sum()):,} px")
except Exception as e:
    print("no edge map:", e)


def lat_of(j):
    return P.to_lonlat((P.X0 + P.X1) / 2, P.Y1 - j * (P.Y1 - P.Y0) / P.H)[1]


for day in (dt.date(2026, 8, 22), dt.date(2026, 8, 23)):
    idx = R._load_index(s3, day)
    want = f"{day:%Y-%m-%d}T21:"
    se = sorted((f for f in idx.get("frames", [])
                 if f.get("code") == "SE" and f["time"].startswith(want)),
                key=lambda f: f["time"])
    print(f"\n=== {day}: {len(se)} SE sweeps in the 21:00 hour ===")
    if not se:
        continue
    f = min(se, key=lambda f: abs(int(f["time"][14:16]) - 15))
    print("sweep", f["time"])
    grid, _ = R._feed_grid(s3, "SE", f)
    wet = grid > 0
    print(f"echo {int(wet.sum()):,} px")
    print(" row   lat     echo x-range      coverage x-range   "
          "gap between them")
    prev_stop = None
    for j in range(0, P.H, 10):
        xs = np.flatnonzero(wet[j])
        cs = np.flatnonzero(edge[j] > 0) if edge is not None else np.array([])
        if not xs.size:
            continue
        stop, cstop = int(xs.max()), (int(cs.max()) if cs.size else -1)
        flag = ""
        if prev_stop is not None and abs(stop - prev_stop) > 40:
            flag = f"   <-- jumps {prev_stop} -> {stop}"
        prev_stop = stop
        print(f" {j:4d} {lat_of(j):5.2f}  {int(xs.min()):3d}..{stop:3d}"
              f"        {int(cs.min()) if cs.size else -1:3d}..{cstop:3d}"
              f"         {cstop - stop:4d} km{flag}")
