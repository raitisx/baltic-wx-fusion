"""One-off: the rain cell E of Riga (~56.7N 26.5E) shows at 17.08 15:22, is
gone at 16:01, back at 17:01. Was it in the stored originals and deleted by
processing, or absent upstream? Counts echo in the cell's bbox at each stage:
raw decode -> after _prepare_source (classify + beam repair) -> after the
composite polar passes. Throwaway."""
import datetime as dt
import io
import logging
import math
import os
import sys

os.environ.setdefault("RADAR_BEAM_DEBUG", "0")
logging.basicConfig(level=logging.WARNING)
sys.path.insert(0, ".")

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from wxfusion import proj3059 as P, radar_store as R  # noqa: E402
from wxfusion.scrape_maps import (_close_radial_spokes, _despeckle,  # noqa: E402
                                  _despeckle_intensity, _prepare_source,
                                  _remove_beam_lines, _remove_beams_polar)

BBOX = (56.4, 57.1, 25.9, 27.3)      # latS, latN, lonW, lonE around the cell
DAY = dt.date(2026, 8, 17)
HOURS = [15, 16, 17]

s3 = R.r2_client()
idx = R._load_index(s3, DAY)
frames = idx.get("frames", [])
print(f"{DAY}: {len(frames)} stored source overlays")


def merc_y(la):
    r = math.radians(la)
    return math.log(math.tan(r) + 1.0 / math.cos(r))


def bbox_mask(shape, sw, ne):
    h, w = shape[:2]
    latS, latN, lonW, lonE = BBOX
    x0 = int((lonW - sw[1]) / (ne[1] - sw[1]) * w)
    x1 = int((lonE - sw[1]) / (ne[1] - sw[1]) * w)
    y0 = int((merc_y(ne[0]) - merc_y(latN)) / (merc_y(ne[0]) - merc_y(sw[0])) * h)
    y1 = int((merc_y(ne[0]) - merc_y(latS)) / (merc_y(ne[0]) - merc_y(sw[0])) * h)
    m = np.zeros(shape[:2], bool)
    m[max(0, y0):min(h, y1), max(0, x0):min(w, x1)] = True
    return m


def grid_bbox_mask():
    latS, latN, lonW, lonE = BBOX
    xs, ys = [], []
    for la, lo in [(latS, lonW), (latS, lonE), (latN, lonW), (latN, lonE)]:
        x, y = P.to_xy(lo, la)
        xs.append(x); ys.append(y)
    c0 = int((min(xs) - P.X0) / (P.X1 - P.X0) * P.W)
    c1 = int((max(xs) - P.X0) / (P.X1 - P.X0) * P.W)
    r0 = int((P.Y1 - max(ys)) / (P.Y1 - P.Y0) * P.H)
    r1 = int((P.Y1 - min(ys)) / (P.Y1 - P.Y0) * P.H)
    m = np.zeros((P.H, P.W), bool)
    m[max(0, r0):min(P.H, r1), max(0, c0):min(P.W, c1)] = True
    return m


GMASK = grid_bbox_mask()

for hour in HOURS:
    target = dt.datetime(DAY.year, DAY.month, DAY.day, hour, tzinfo=dt.timezone.utc)
    print(f"\n=== hour {hour:02d}:00 ===")
    for code in ("LV", "EE", "LT2"):
        cands = []
        for f in frames:
            if f.get("code") != code:
                continue
            t = dt.datetime.strptime(f["time"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=dt.timezone.utc)
            gap = abs((t - target).total_seconds())
            if gap <= 40 * 60:
                cands.append((gap, t, f))
        if not cands:
            print(f"  {code}: no sweep within 40 min")
            continue
        cands.sort()
        gap, t, f = cands[0]
        body = s3.get_object(Bucket=R.config.R2_BUCKET, Key=f["key"])["Body"].read()
        arr = np.array(Image.open(io.BytesIO(body)).convert("RGBA"))
        m = bbox_mask(arr.shape, tuple(f["sw"]), tuple(f["ne"]))
        a = arr[..., 3].astype(int)
        rgb = arr[..., :3].astype(int)
        raw = (a > 60) & ((rgb.max(2) - rgb.min(2)) > 25)
        cls = _prepare_source(arr, f["code"], f)
        grid = P.resample_mercator_image(cls, tuple(f["sw"]), tuple(f["ne"]))
        comp = _close_radial_spokes(_despeckle_intensity(
            _remove_beam_lines(_remove_beams_polar(_despeckle(grid)))))
        print(f"  {code} sweep {t:%H:%M} ({gap/60:+.0f} min): "
              f"raw-in-bbox={int((raw & m).sum())} px, "
              f"classified-in-bbox={int(((cls > 0) & m).sum())} px, "
              f"after-composite-passes={int(((comp > 0) & GMASK).sum())} px "
              f"(frame total raw {int(raw.sum())})")
        # every other candidate sweep, raw only — did a different sweep have it?
        for gap2, t2, f2 in cands[1:4]:
            b2 = s3.get_object(Bucket=R.config.R2_BUCKET, Key=f2["key"])["Body"].read()
            a2 = np.array(Image.open(io.BytesIO(b2)).convert("RGBA"))
            m2 = bbox_mask(a2.shape, tuple(f2["sw"]), tuple(f2["ne"]))
            aa = a2[..., 3].astype(int)
            rgb2 = a2[..., :3].astype(int)
            raw2 = (aa > 60) & ((rgb2.max(2) - rgb2.min(2)) > 25)
            print(f"     alt {code} sweep {t2:%H:%M}: raw-in-bbox={int((raw2 & m2).sum())} px")
print("\nDONE")
