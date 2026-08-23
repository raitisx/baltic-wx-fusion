"""EE 2nd radar present in the 12:30 frame, gone at 12:45 — where in OUR
chain does it vanish? The 12:47 source is complete (510k painted px), so
split the stages: classified -> source beam repair -> polar passes ->
rendered frame, counting echo in the Surgavere band (56.8-58.4N 23.5-27E).
Throwaway probe (missing-probe.yml).
"""
import datetime as dt
import io
import sys

sys.path.insert(0, ".")

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from wxfusion import config, proj3059 as P, radar_store as R  # noqa: E402
from wxfusion.scrape_maps import (_classify_intensity, _close_radial_spokes,  # noqa: E402
                                  _despeckle, _despeckle_intensity,
                                  _prepare_source, _remove_beam_lines,
                                  _remove_beams_polar)

s3 = R.r2_client()
BBOX = (56.8, 58.4, 23.5, 27.0)


def bbox_mask():
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


M = bbox_mask()
idx = R._load_index(s3, dt.date(2026, 8, 22))
for stamp in ("2026-08-22T12:29:00Z", "2026-08-22T12:47:00Z"):
    f = next(x for x in idx["frames"]
             if x["code"] == "EE" and x["time"] == stamp)
    body = s3.get_object(Bucket=config.R2_BUCKET, Key=f["key"])["Body"].read()
    arr = np.array(Image.open(io.BytesIO(body)).convert("RGBA"))
    plain = _classify_intensity(arr, "EE")
    plain_g = P.resample_mercator_image(plain, tuple(f["sw"]), tuple(f["ne"]))
    cls = _prepare_source(arr, "EE", f)
    grid = P.resample_mercator_image(cls, tuple(f["sw"]), tuple(f["ne"]))
    comp = _close_radial_spokes(_despeckle_intensity(
        _remove_beam_lines(_remove_beams_polar(_despeckle(grid)))))
    print(f"EE {stamp[11:16]}: classified bbox={int(((plain_g > 0) & M).sum())} "
          f"total={int((plain_g > 0).sum())} | after source-repair "
          f"bbox={int(((grid > 0) & M).sum())} total={int((grid > 0).sum())} | "
          f"after polar bbox={int(((comp > 0) & M).sum())} "
          f"total={int((comp > 0).sum())}")

for vtag in ("20260822T1234", "20260822T1247"):
    body = s3.get_object(Bucket=config.R2_BUCKET,
                         Key=f"{R.FRAME_PREFIX}/{vtag}.png")["Body"].read()
    a = np.array(Image.open(io.BytesIO(body)).convert("RGBA"))
    # coloured = not background cream and not border grey: use alpha>0 and
    # saturation-ish test: green/orange px have channel spread
    spread = a[..., :3].max(2).astype(int) - a[..., :3].min(2).astype(int)
    echo = (spread > 25)
    print(f"frame {vtag}: coloured bbox={int((echo & M).sum())} "
          f"total={int(echo.sum())}")
