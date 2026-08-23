"""How many radars are in each product, and where? RANSAC on the disc edges.

The first attempt took distance-transform peaks, which cannot work: two
overlapping discs merge into one region with a single interior maximum, and
what it actually found was a rain blob. A coverage disc leaves a circular
ARC on the boundary of the painted area instead, so this samples boundary
points, fits circumcircles through random triples, keeps the ones whose
radius is radar-like and that many boundary points agree with, and reports
the distinct circles found. Circles are then named against RADAR_SITES so a
missing antenna shows up as a good fit with nothing listed near it.
"""
import base64
import datetime as dt
import io
import math
import sys

sys.path.insert(0, ".")

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402
from scipy.ndimage import binary_erosion  # noqa: E402

from wxfusion import config, radar_store as R  # noqa: E402
from wxfusion.scrape_maps import RADAR_SITES  # noqa: E402

s3 = R.r2_client()
WHEN = dt.datetime(2026, 8, 22, 12, 47, tzinfo=dt.timezone.utc)
idx = R._load_index(s3, WHEN.date())
rng = np.random.default_rng(7)


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


for code in ("EE", "LT2", "LV"):
    cands = [(abs((dt.datetime.strptime(f["time"], "%Y-%m-%dT%H:%M:%SZ")
                   .replace(tzinfo=dt.timezone.utc) - WHEN).total_seconds()), f)
             for f in idx.get("frames", [])
             if f.get("code") == code and not f.get("grid3059")]
    if not cands:
        print(f"--- {code}: nothing stored ---")
        continue
    _, f = min(cands, key=lambda c: c[0])
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
    print(f"--- {code} {W}x{H}  {kmx:.2f} km/px  boundary={len(xs)} px ---")
    print("   listed:", ", ".join(n for n, _, _ in RADAR_SITES
                                  if n.startswith(code[:2])))
    if len(xs) < 200:
        print("   too little boundary to fit")
        continue
    pts = np.stack([xs * kmx, ys * kmy], 1)          # km space
    found = []
    for _ in range(30000):
        i, j, k = rng.integers(0, len(pts), 3)
        c = circum(pts[i], pts[j], pts[k])
        if not c or not (150 <= c[2] <= 290):
            continue
        cx, cy, r = c
        d = np.abs(np.hypot(pts[:, 0] - cx, pts[:, 1] - cy) - r)
        inl = int((d < 4.0).sum())
        if inl < 250:
            continue
        # distinct from what we already have?
        if any(math.hypot(cx - a, cy - b) < 60 for a, b, _, _ in found):
            for n2, (a, b, rr, ii) in enumerate(found):
                if math.hypot(cx - a, cy - b) < 60 and inl > ii:
                    found[n2] = (cx, cy, r, inl)
            continue
        found.append((cx, cy, r, inl))
    found.sort(key=lambda t: -t[3])
    dr = ImageDraw.Draw(im)
    for cx, cy, r, inl in found[:5]:
        lo = s_lon + (cx / kmx / W) * (n_lon - s_lon)
        la = n_lat - (cy / kmy / H) * (n_lat - s_lat)
        near = min(RADAR_SITES, key=lambda s: (s[1] - la) ** 2 + (s[2] - lo) ** 2)
        dkm = math.hypot((near[1] - la) * 111.32,
                         (near[2] - lo) * 111.32 * math.cos(math.radians(la)))
        print(f"   circle {la:.3f}N {lo:.3f}E  r={r:.0f} km  fit={inl} px"
              f"   nearest listed: {near[0]} ({dkm:.0f} km)")
        px, py = cx / kmx, cy / kmy
        dr.ellipse([px - 9, py - 9, px + 9, py + 9], outline=(255, 0, 0), width=5)
        rp = r / kmx
        dr.ellipse([px - rp, py - rp, px + rp, py + rp],
                   outline=(255, 0, 0), width=2)
    if not found:
        print("   no radar-sized circle fits the boundary")
    bg = Image.new("RGB", im.size, (247, 245, 239))
    bg.paste(im, (0, 0), im)
    if max(bg.size) > 900:
        k = 900 / max(bg.size)
        bg = bg.resize((int(W * k), int(H * k)), Image.LANCZOS)
    buf = io.BytesIO()
    bg.save(buf, "PNG", optimize=True)
    print(f"@@IMG:discs_{code}:{base64.b64encode(buf.getvalue()).decode()}")
