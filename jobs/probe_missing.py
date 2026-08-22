"""One-off: why is echo visible on meteolapa at 22.08 16:37 missing from our
composite? Inventories the stored source overlays around that time: per feed,
the sweep used, colour inventory (r,g,b,alpha,count->class) inside the two
reported areas, and raw vs alpha-gated vs classified counts. Throwaway."""
import datetime as dt
import io
import json
import math
import sys

sys.path.insert(0, ".")
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from wxfusion import radar_store as R  # noqa: E402
from wxfusion.scrape_maps import _classify_intensity  # noqa: E402

TARGET = dt.datetime(2026, 8, 22, 16, 37, tzinfo=dt.timezone.utc)
BOXES = {"riga_sw": (56.6, 57.0, 23.5, 24.4),
         "zemgale": (56.1, 56.7, 25.0, 26.4)}

s3 = R.r2_client()
frames = R._load_index(s3, TARGET.date()).get("frames", [])


def merc_y(la):
    r = math.radians(la)
    return math.log(math.tan(r) + 1.0 / math.cos(r))


def bbox_mask(shape, sw, ne, box):
    h, w = shape[:2]
    latS, latN, lonW, lonE = box
    x0 = int((lonW - sw[1]) / (ne[1] - sw[1]) * w)
    x1 = int((lonE - sw[1]) / (ne[1] - sw[1]) * w)
    y0 = int((merc_y(ne[0]) - merc_y(latN)) / (merc_y(ne[0]) - merc_y(sw[0])) * h)
    y1 = int((merc_y(ne[0]) - merc_y(latS)) / (merc_y(ne[0]) - merc_y(sw[0])) * h)
    m = np.zeros(shape[:2], bool)
    m[max(0, y0):min(h, y1), max(0, x0):min(w, x1)] = True
    return m


for code in ("LV", "EE", "LT2"):
    cands = []
    for f in frames:
        if f.get("code") != code:
            continue
        t = dt.datetime.strptime(f["time"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc)
        gap = abs((t - TARGET).total_seconds())
        if gap <= 45 * 60:
            cands.append((gap, t, f))
    if not cands:
        print(f"{code}: no sweep near {TARGET:%H:%M}")
        continue
    cands.sort()
    gap, t, f = cands[0]
    body = s3.get_object(Bucket=R.config.R2_BUCKET, Key=f["key"])["Body"].read()
    arr = np.array(Image.open(io.BytesIO(body)).convert("RGBA"))
    lev = _classify_intensity(arr, code)
    a = arr[..., 3].astype(int)
    print(f"\n=== {code} sweep {t:%H:%M} ({gap/60:+.0f} min) "
          f"[also had: {[c[1].strftime('%H:%M') for c in cands[1:4]]}] ===")
    for name, box in BOXES.items():
        m = bbox_mask(arr.shape, tuple(f["sw"]), tuple(f["ne"]), box)
        vis = m & (a > 0)
        gated = m & (a > 60)
        clsd = m & (lev > 0)
        print(f"  {name}: any-alpha {int(vis.sum())} px | alpha>60 "
              f"{int(gated.sum())} px | classified {int(clsd.sum())} px")
        if vis.any():
            px = arr[vis]
            colors, counts = np.unique(px.reshape(-1, 4), axis=0, return_counts=True)
            order = np.argsort(-counts)[:10]
            for i in order:
                r_, g_, b_, a_ = colors[i]
                # class this colour maps to (probe a 1x1 array)
                one = np.zeros((1, 1, 4), np.uint8); one[0, 0] = colors[i]
                c = int(_classify_intensity(one, code)[0, 0])
                print(f"    rgba({r_},{g_},{b_},{a_}) x{counts[i]} -> lev {c}")
print("\nDONE")
