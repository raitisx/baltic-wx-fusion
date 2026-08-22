"""TEST render: what the composite looks like with every feed on FI's scale.

FMI's rr product is the hottest-reading feed; the user wants to see the map
with all other radars lifted to it instead of FI damped down. Factors from
the 22.08 co-wet-pixel probe, chained through the direct overlaps:

  FI  x1.0   (anchor — FMI's own rain rates)
  SE  x3.4   (stored SE carries -5 dB; SE_raw/FI = x0.60 -> 2.05/0.60)
  EE  x4.5   (measured FI/EE)
  LT2 x3.33  (via SE: (SE_raw/LT2)/(SE_raw/FI) = 2.0/0.6)
  LV  x3.33  (assumed LT2-like; the direct LV sample was 745 px only)

Emits two @@IMG frames for 13:00 UTC today: current calibration vs FI scale.
Throwaway probe, dispatched via missing-probe.yml.
"""
import base64
import datetime as dt
import io
import logging
import sys

logging.basicConfig(level=logging.WARNING)
sys.path.insert(0, ".")

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from wxfusion import config, proj3059 as P, radar_store as R  # noqa: E402
from wxfusion.maps_meps import RAIN_ANCHORS, pos_rain, ramp_rgb  # noqa: E402
from wxfusion.scrape_maps import (LEV_MAX, _close_radial_spokes,  # noqa: E402
                                  _despeckle, _despeckle_intensity,
                                  _prepare_source, _remove_beam_lines,
                                  _remove_beams_polar, draw_borders,
                                  rate_to_lev)

FACTORS = {"FI": 1.0, "SE": 3.4, "EE": 4.5, "LT2": 3.33, "LT": 3.33,
           "LV": 3.33}
HOUR = dt.datetime.now(dt.timezone.utc).replace(
    hour=13, minute=0, second=0, microsecond=0)

s3 = R.r2_client()
idx = R._load_index(s3, HOUR.date())
best = {}
for f in idx.get("frames", []):
    t = dt.datetime.strptime(f["time"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=dt.timezone.utc)
    gap = abs((t - HOUR).total_seconds())
    if gap <= 12 * 60 and (f["code"] not in best or gap < best[f["code"]][0]):
        best[f["code"]] = (gap, f)
print("feeds at", HOUR, ":", sorted(best))

grids = {}
for code, (_, f) in best.items():
    body = s3.get_object(Bucket=config.R2_BUCKET, Key=f["key"])["Body"].read()
    if f.get("grid3059"):
        grids[code] = np.array(Image.open(io.BytesIO(body)).convert("L"),
                               np.uint8)
    else:
        arr = np.array(Image.open(io.BytesIO(body)).convert("RGBA"))
        cls = _prepare_source(arr, f["code"], f)
        g = P.resample_mercator_image(cls, tuple(f["sw"]), tuple(f["ne"]))
        grids[code] = _close_radial_spokes(_despeckle_intensity(
            _remove_beam_lines(_remove_beams_polar(_despeckle(g)))))
    print(f"  {code}: {(grids[code] > 0).sum()} px echo")


def scale_lev(lev, k):
    if k == 1.0:
        return lev
    out = lev.copy()
    wet = lev > 0
    r = pos_rain((lev[wet].astype(np.float64) - 0.5) / LEV_MAX) * k
    out[wet] = rate_to_lev(r)
    return out


def render(scaled):
    comp = np.zeros((P.H, P.W), np.uint8)
    for code, g in grids.items():
        gg = scale_lev(g, FACTORS[code]) if scaled else g
        comp = np.maximum(comp, gg)
    rgb = np.full((P.H, P.W, 3), 247, np.uint8)
    wet = comp > 0
    rgb[wet] = (np.array(ramp_rgb((comp[wet].astype(np.float64) - 0.5)
                                  / LEV_MAX, RAIN_ANCHORS)) )
    img = Image.fromarray(rgb).convert("RGBA")
    draw_borders(img)
    return img.convert("RGB")


for name, scaled in (("current", False), ("fi-scale", True)):
    buf = io.BytesIO()
    render(scaled).save(buf, "PNG", optimize=True)
    print(f"@@IMG:{name}:{base64.b64encode(buf.getvalue()).decode()}")
