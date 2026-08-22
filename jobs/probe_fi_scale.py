"""TEST render v2: middle-ground calibration + SE edge fade.

Anchor = FI/2 (halfway between the current Baltic anchor and FMI's hot rr
product). Factors from the 22.08 co-wet-pixel probe, halved from FI scale:

  FI  x0.5    SE  x1.7 (on stored -5 dB levels)    EE  x2.25
  LT2/LT/LV x1.67

Plus "dampening to the edge" for SE: the SMHI composite ends in a hard
coverage wall; echo now fades over the last EDGE_FADE_KM approaching it
(coverage mask taken from a live SMHI tif, v=255 = outside).

Emits three @@IMG frames for 13:00 UTC today: current, middle, and the
previous full FI-scale for reference. Throwaway probe (missing-probe.yml).
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
from wxfusion.http import session  # noqa: E402
from wxfusion.maps_meps import RAIN_ANCHORS, pos_rain, ramp_rgb  # noqa: E402
from wxfusion.radar_nordic import SMHI_LIST, _warp_rr_to_grid  # noqa: E402
from wxfusion.scrape_maps import (LEV_MAX, _close_radial_spokes,  # noqa: E402
                                  _despeckle, _despeckle_intensity,
                                  _prepare_source, _remove_beam_lines,
                                  _remove_beams_polar, draw_borders,
                                  rate_to_lev)

MID = {"FI": 0.5, "SE": 1.7, "EE": 2.25, "LT2": 1.67, "LT": 1.67, "LV": 1.67}
FULL = {"FI": 1.0, "SE": 3.4, "EE": 4.5, "LT2": 3.33, "LT": 3.33, "LV": 3.33}
EDGE_FADE_KM = 70.0
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

# SE coverage-edge alpha from a live SMHI frame (v=255 marks outside).
se_alpha = np.ones((P.H, P.W), np.float64)
try:
    import rasterio
    from scipy.ndimage import distance_transform_edt
    s = session()
    files = s.get(SMHI_LIST.format(d=HOUR.date()), timeout=45).json()["files"]
    tif = next(x["link"] for fl in reversed(files)
               for x in fl.get("formats", []) if x.get("key") == "tif")
    with rasterio.open(io.BytesIO(s.get(tif, timeout=60).content)) as ds:
        v = ds.read(1)
        cov = _warp_rr_to_grid((v != 255).astype(np.float32),
                               ds.transform, ds.crs) > 0.5
    se_alpha = np.clip(distance_transform_edt(cov) / EDGE_FADE_KM, 0.0, 1.0)
    print(f"SE coverage: {int(cov.sum())} px, "
          f"faded band {int(((se_alpha > 0) & (se_alpha < 1)).sum())} px")
except Exception as e:
    print("SE edge alpha failed, no fade:", e)


def scale_lev(lev, k, alpha=None):
    out = lev.copy()
    wet = lev > 0
    r = pos_rain((lev[wet].astype(np.float64) - 0.5) / LEV_MAX) * k
    if alpha is not None:
        r = r * alpha[wet]
    lv = rate_to_lev(r)
    lv[r < 0.08] = 0                     # faded-out edge echo disappears
    out[wet] = lv
    return out


def render(factors, fade_se):
    comp = np.zeros((P.H, P.W), np.uint8)
    for code, g in grids.items():
        k = factors[code] if factors else 1.0
        al = se_alpha if (fade_se and code == "SE") else None
        gg = scale_lev(g, k, al) if (k != 1.0 or al is not None) else g
        comp = np.maximum(comp, gg)
    rgb = np.full((P.H, P.W, 3), 247, np.uint8)
    wet = comp > 0
    rgb[wet] = np.array(ramp_rgb((comp[wet].astype(np.float64) - 0.5)
                                 / LEV_MAX, RAIN_ANCHORS))
    img = Image.fromarray(rgb).convert("RGBA")
    draw_borders(img)
    return img.convert("RGB")


for name, factors, fade in (("current", None, False),
                            ("middle", MID, True),
                            ("fi-scale", FULL, False)):
    buf = io.BytesIO()
    render(factors, fade).save(buf, "PNG", optimize=True)
    print(f"@@IMG:{name}:{base64.b64encode(buf.getvalue()).decode()}")
