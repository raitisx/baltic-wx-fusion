"""How many radars are in each product, and where? RANSAC on the disc edges.

The first attempt took distance-transform peaks, which cannot work: two
overlapping discs merge into one region with a single interior maximum, and
what it actually found was a rain blob. A coverage disc leaves a circular
ARC on the boundary of the covered area instead, so this samples boundary
points, fits circumcircles through random triples, keeps the ones whose
radius is radar-like and that many boundary points agree with, and reports
the distinct circles found. Circles are named against RADAR_SITES, so an
antenna we never listed shows up as a good fit with nothing near it.

Two kinds of product are probed:

  EE / LT2 / LV   the stored meteolapa frames, whose alpha channel marks the
                  painted area. Their boundary mixes coverage edge with the
                  edge of rain, so only strong fits mean anything.
  SE / FI         fetched live from SMHI / FMI in their NATIVE grid, where a
                  reserved value marks "outside coverage" — a clean coverage
                  footprint whose outer boundary is made of the range arcs of
                  the border radars. This is what says how many antennas hide
                  behind the two SE sites we list.
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

from wxfusion import config, proj3059 as P, radar_store as R  # noqa: E402
from wxfusion.radar_nordic import FMI_S3, SMHI_LIST  # noqa: E402
from wxfusion.http import session  # noqa: E402
from wxfusion.scrape_maps import RADAR_SITES  # noqa: E402

s3 = R.r2_client()
WHEN = dt.datetime(2026, 8, 22, 12, 47, tzinfo=dt.timezone.utc)
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


def ransac(pts, iters=40000, rlo=150.0, rhi=290.0, tol=4.0, min_inl=250):
    """Boundary points in km -> [(cx, cy, r, inliers)], best first."""
    found = []
    for _ in range(iters):
        i, j, k = rng.integers(0, len(pts), 3)
        c = circum(pts[i], pts[j], pts[k])
        if not c or not (rlo <= c[2] <= rhi):
            continue
        cx, cy, r = c
        d = np.abs(np.hypot(pts[:, 0] - cx, pts[:, 1] - cy) - r)
        inl = int((d < tol).sum())
        if inl < min_inl:
            continue
        hit = next((n for n, (a, b, _, _) in enumerate(found)
                    if math.hypot(cx - a, cy - b) < 60), None)
        if hit is None:
            found.append((cx, cy, r, inl))
        elif inl > found[hit][3]:
            found[hit] = (cx, cy, r, inl)
    found.sort(key=lambda t: -t[3])
    return found


def name_circle(la, lo, r, inl, tag=""):
    near = min(RADAR_SITES, key=lambda s: (s[1] - la) ** 2 + (s[2] - lo) ** 2)
    dkm = math.hypot((near[1] - la) * 111.32,
                     (near[2] - lo) * 111.32 * math.cos(math.radians(la)))
    flag = "LISTED " if dkm < 25 else "UNLISTED"
    print(f"   {flag} {la:.3f}N {lo:.3f}E  r={r:.0f} km  fit={inl} px"
          f"   nearest listed: {near[0]} ({dkm:.0f} km){tag}")


def send(im, name, cap=1100):
    bg = Image.new("RGB", im.size, (247, 245, 239))
    bg.paste(im.convert("RGBA"), (0, 0), im.convert("RGBA"))
    if max(bg.size) > cap:
        k = cap / max(bg.size)
        bg = bg.resize((int(bg.width * k), int(bg.height * k)), Image.LANCZOS)
    buf = io.BytesIO()
    bg.save(buf, "PNG", optimize=True)
    print(f"@@IMG:{name}:{base64.b64encode(buf.getvalue()).decode()}")


# ---------------------------------------------------------------- meteolapa
idx = R._load_index(s3, WHEN.date())
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
    found = ransac(np.stack([xs * kmx, ys * kmy], 1))
    dr = ImageDraw.Draw(im)
    for cx, cy, r, inl in found[:5]:
        lo = s_lon + (cx / kmx / W) * (n_lon - s_lon)
        la = n_lat - (cy / kmy / H) * (n_lat - s_lat)
        name_circle(la, lo, r, inl)
        px, py = cx / kmx, cy / kmy
        dr.ellipse([px - 9, py - 9, px + 9, py + 9], outline=(255, 0, 0), width=5)
        rp = r / kmx
        dr.ellipse([px - rp, py - rp, px + rp, py + rp],
                   outline=(255, 0, 0), width=2)
    if not found:
        print("   no radar-sized circle fits the boundary")
    send(im, f"discs_{code}", 900)


# ------------------------------------------------------------------- nordic
def probe_footprint(code, cov, transform, crs, note):
    """cov = bool coverage mask on a native grid -> fitted range arcs."""
    from pyproj import Transformer
    H, W = cov.shape
    px_km = abs(transform.a) / 1000.0
    edge = cov & ~binary_erosion(cov, np.ones((3, 3), bool))
    edge[:2] = edge[-2:] = False       # raster border is not a coverage edge
    edge[:, :2] = edge[:, -2:] = False
    ys, xs = np.nonzero(edge)
    print(f"--- {code} {W}x{H} native  {px_km:.2f} km/px  "
          f"covered={int(cov.sum())} px  boundary={len(xs)} px  ({note}) ---")
    print("   listed:", ", ".join(n for n, _, _ in RADAR_SITES
                                  if n.startswith(code)))
    if len(xs) < 200:
        print("   too little boundary to fit")
        return
    step = max(1, len(xs) // 6000)     # keep the inlier count cheap
    xs, ys = xs[::step], ys[::step]
    pts = np.stack([xs * px_km, ys * px_km], 1)
    found = ransac(pts, iters=60000, tol=max(4.0, 2 * px_km),
                   min_inl=max(30, len(xs) // 60))
    inv = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    fwd3059 = Transformer.from_crs("EPSG:4326", "EPSG:3059", always_xy=True)
    vis = Image.fromarray((cov * 200).astype(np.uint8)).convert("RGBA")
    dr = ImageDraw.Draw(vis)
    shown = 0
    for cx, cy, r, inl in found:
        mx = transform.c + (cx / px_km) * transform.a
        my = transform.f + (cy / px_km) * transform.e
        lo, la = inv.transform(mx, my)
        x3, y3 = fwd3059.transform(lo, la)
        reach = (P.X0 - r * 1000 <= x3 <= P.X1 + r * 1000
                 and P.Y0 - r * 1000 <= y3 <= P.Y1 + r * 1000)
        name_circle(la, lo, r, inl, "   <-- reaches our map" if reach else "")
        if shown < 12:
            px, py = cx / px_km, cy / px_km
            col = (255, 60, 60) if reach else (80, 140, 255)
            dr.ellipse([px - 6, py - 6, px + 6, py + 6], outline=col, width=4)
            rp = r / px_km
            dr.ellipse([px - rp, py - rp, px + rp, py + rp], outline=col, width=2)
            shown += 1
    if not found:
        print("   no radar-sized circle fits the footprint")
    send(vis, f"discs_{code}")


s = session()
try:
    import rasterio
    files = []
    for day in (dt.date.today(), dt.date.today() - dt.timedelta(days=1)):
        r = s.get(SMHI_LIST.format(d=day), timeout=45)
        if r.ok:
            files += r.json().get("files") or []
    tif = next(f["link"] for c in sorted(files, key=lambda f: f.get("valid", ""),
                                         reverse=True)
               for f in c.get("formats", []) if f.get("key") == "tif")
    with rasterio.open(io.BytesIO(s.get(tif, timeout=60).content)) as ds:
        v = ds.read(1)
        probe_footprint("SE", v != 255, ds.transform, ds.crs,
                        "SMHI comp, 255 = outside coverage")
except Exception as e:
    print("SE footprint probe failed:", type(e).__name__, e)

try:
    import rasterio
    from wxfusion.radar_nordic import _fmi_s3_entries
    day = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).date()
    ent = _fmi_s3_entries(s, day)
    print(f"FI: {len(ent)} composites listed for {day}, "
          f"using {ent[-1][0] if ent else None}")
    with rasterio.open(io.BytesIO(s.get(ent[-1][1], timeout=120).content)) as ds:
        v = ds.read(1)
        probe_footprint("FI", v != 255, ds.transform, ds.crs,
                        "FMI finrad DBZH, 255 = nodata")
except Exception as e:
    print("FI footprint probe failed:", type(e).__name__, e)
