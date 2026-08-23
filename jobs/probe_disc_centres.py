"""Follow-up to the range-arc fit: is Estonia's product ONE disc, and what
else does meteolapa serve?

The first fit answered the big question — SMHI's footprint is visibly a union
of twelve 240 km discs, and Estonia's product is a single 245 km disc centred
on Harku, with no arc anywhere near Surgavere. Two things are worth nailing
down before RADAR_SITES is rewritten:

  1. Is that Harku disc the same in every sweep, or does the feed alternate
     between antennas? Fit the best single circle on frames spread across the
     day and compare centres.
  2. Does meteolapa publish an Estonian product we are not consuming (a
     Surgavere one), which would be real close-range coverage for southern
     Estonia rather than Harku's far range? List every code it serves with
     its bounds and size.
"""
import datetime as dt
import io
import math
import sys

sys.path.insert(0, ".")

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402
from scipy.ndimage import binary_erosion  # noqa: E402

from wxfusion import config, radar_store as R  # noqa: E402
from wxfusion.scrape_maps import (RADAR_SITES, fetch_overlays_window,  # noqa: E402
                                  parse_radar_overlays)

s3 = R.r2_client()
DAY = dt.date(2026, 8, 22)
rng = np.random.default_rng(11)


def circum(p1, p2, p3):
    (x1, y1), (x2, y2), (x3, y3) = p1, p2, p3
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-6:
        return None
    ux = ((x1 ** 2 + y1 ** 2) * (y2 - y3) + (x2 ** 2 + y2 ** 2) * (y3 - y1)
          + (x3 ** 2 + y3 ** 2) * (y1 - y2)) / d
    uy = ((x1 ** 2 + y1 ** 2) * (x3 - x2) + (x2 ** 2 + y2 ** 2) * (x1 - x3)
          + (x3 ** 2 + y3 ** 2) * (x2 - x1)) / d
    return ux, uy, math.hypot(x1 - ux, y1 - uy)


def best_circles(pts, keep=3, iters=40000, tol=4.0):
    found = []
    for _ in range(iters):
        i, j, k = rng.integers(0, len(pts), 3)
        c = circum(pts[i], pts[j], pts[k])
        if not c or not (120 <= c[2] <= 300):
            continue
        cx, cy, r = c
        inl = int((np.abs(np.hypot(pts[:, 0] - cx, pts[:, 1] - cy) - r)
                   < tol).sum())
        hit = next((n for n, (a, b, _, _) in enumerate(found)
                    if math.hypot(cx - a, cy - b) < 80), None)
        if hit is None:
            found.append((cx, cy, r, inl))
        elif inl > found[hit][3]:
            found[hit] = (cx, cy, r, inl)
    found.sort(key=lambda t: -t[3])
    return found[:keep]


print("=== EE product: one disc per sweep, or alternating antennas? ===")
idx = R._load_index(s3, DAY)
ee = sorted((f for f in idx.get("frames", [])
             if f.get("code") == "EE" and not f.get("grid3059")),
            key=lambda f: f["time"])
print(f"{len(ee)} EE frames stored on {DAY}")
for f in ee[::max(1, len(ee) // 10)][:10]:
    body = s3.get_object(Bucket=config.R2_BUCKET, Key=f["key"])["Body"].read()
    im = Image.open(io.BytesIO(body)).convert("RGBA")
    W, H = im.size
    (s_lat, s_lon), (n_lat, n_lon) = tuple(f["sw"]), tuple(f["ne"])
    kmx = (n_lon - s_lon) * 111.32 * math.cos(
        math.radians((s_lat + n_lat) / 2)) / W
    kmy = (n_lat - s_lat) * 111.32 / H
    painted = np.array(im)[..., 3] > 60
    edge = painted & ~binary_erosion(painted, np.ones((3, 3), bool))
    ys, xs = np.nonzero(edge)
    if len(xs) < 200:
        print(f"  {f['time']}  boundary={len(xs)} px — too little to fit")
        continue
    out = []
    for cx, cy, r, inl in best_circles(np.stack([xs * kmx, ys * kmy], 1)):
        lo = s_lon + (cx / kmx / W) * (n_lon - s_lon)
        la = n_lat - (cy / kmy / H) * (n_lat - s_lat)
        near = min(RADAR_SITES, key=lambda s: (s[1] - la) ** 2 + (s[2] - lo) ** 2)
        dkm = math.hypot((near[1] - la) * 111.32,
                         (near[2] - lo) * 111.32 * math.cos(math.radians(la)))
        out.append(f"{la:.2f}N {lo:.2f}E r={r:.0f} fit={inl} [{near[0]} {dkm:.0f}km]")
    print(f"  {f['time']}  painted={int(painted.sum())}  " + " | ".join(out))

print("\n=== what meteolapa serves right now ===")
try:
    for o in sorted(parse_radar_overlays(), key=lambda o: (o["code"], o["time"])):
        print(f"  live {o['code']:5s} {o['time']:%H:%M}  sw={o['sw']} ne={o['ne']}"
              f"  {o['url'].rsplit('/', 1)[-1]}")
except Exception as e:
    print("  live listing failed:", type(e).__name__, e)

print("\n=== codes in a 3 h archive window of 22.08 ===")
try:
    lo_t = dt.datetime(2026, 8, 22, 12, tzinfo=dt.timezone.utc)
    seen = {}
    for o in fetch_overlays_window(lo_t, lo_t + dt.timedelta(hours=3)):
        seen.setdefault(o["code"], []).append(o)
    for code, lst in sorted(seen.items()):
        o = lst[0]
        print(f"  {code:5s} {len(lst):3d} frames  sw={o['sw']} ne={o['ne']}")
except Exception as e:
    print("  archive listing failed:", type(e).__name__, e)
