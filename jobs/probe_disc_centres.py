"""How many radars are actually in each product, and where are they?

Our RADAR_SITES list is an assumption; the Voronoi split and the beam
attribution both lean on it. A radar composite is the union of per-antenna
coverage discs, so the discs can be found in the picture itself: the
distance transform of the painted area peaks at each disc centre, with the
peak value ~ the disc radius. This reports the peaks per feed in lat/lon,
next to the sites we currently list, and marks them on the image.
"""
import base64
import datetime as dt
import io
import math
import sys

sys.path.insert(0, ".")

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402
from scipy.ndimage import distance_transform_edt  # noqa: E402
from skimage.feature import peak_local_max  # noqa: E402

from wxfusion import config, radar_store as R  # noqa: E402
from wxfusion.scrape_maps import RADAR_SITES  # noqa: E402

s3 = R.r2_client()
WHEN = dt.datetime(2026, 8, 22, 12, 47, tzinfo=dt.timezone.utc)
idx = R._load_index(s3, WHEN.date())

for code in ("EE", "LT2", "LV"):
    cands = []
    for f in idx.get("frames", []):
        if f.get("code") != code or f.get("grid3059"):
            continue
        t = dt.datetime.strptime(f["time"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc)
        cands.append((abs((t - WHEN).total_seconds()), t, f))
    if not cands:
        print(f"--- {code}: nothing stored ---")
        continue
    _, t, f = min(cands)
    body = s3.get_object(Bucket=config.R2_BUCKET, Key=f["key"])["Body"].read()
    im = Image.open(io.BytesIO(body)).convert("RGBA")
    W, H = im.size
    (s_lat, s_lon), (n_lat, n_lon) = tuple(f["sw"]), tuple(f["ne"])
    painted = np.array(im)[..., 3] > 60
    km_per_px_x = (n_lon - s_lon) * 111.32 * math.cos(
        math.radians((s_lat + n_lat) / 2)) / W
    dist = distance_transform_edt(painted)
    # A radar disc is 100-300 km across, so peaks must be that far apart and
    # deep enough to be a disc centre rather than a rain blob.
    min_km = 60.0
    peaks = peak_local_max(dist, min_distance=int(min_km / km_per_px_x),
                           threshold_abs=int(80 / km_per_px_x))
    print(f"--- {code} {t:%H:%M}Z  {W}x{H}  {km_per_px_x:.2f} km/px ---")
    print("   listed sites:", ", ".join(
        n for n, _, _ in RADAR_SITES if n.startswith(code[:2])))
    dr = ImageDraw.Draw(im)
    for y, x in peaks[:8]:
        lo = s_lon + (x / W) * (n_lon - s_lon)
        la = n_lat - (y / H) * (n_lat - s_lat)
        rad_km = dist[y, x] * km_per_px_x
        near = min(((n, la0, lo0) for n, la0, lo0 in RADAR_SITES),
                   key=lambda s: (s[1] - la) ** 2 + (s[2] - lo) ** 2)
        d_km = math.hypot((near[1] - la) * 111.32,
                          (near[2] - lo) * 111.32 * math.cos(math.radians(la)))
        print(f"   disc centre {la:.3f}N {lo:.3f}E  radius~{rad_km:.0f} km"
              f"   nearest listed: {near[0]} ({d_km:.0f} km away)")
        r = 10
        dr.ellipse([x - r, y - r, x + r, y + r], outline=(255, 0, 0), width=4)
    bg = Image.new("RGB", im.size, (247, 245, 239))
    bg.paste(im, (0, 0), im)
    if max(bg.size) > 900:
        k = 900 / max(bg.size)
        bg = bg.resize((int(W * k), int(H * k)), Image.LANCZOS)
    buf = io.BytesIO()
    bg.save(buf, "PNG", optimize=True)
    print(f"@@IMG:discs_{code}:{base64.b64encode(buf.getvalue()).decode()}")
