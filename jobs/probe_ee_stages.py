"""Where does Surgavere's rain go? Count COLOURED echo per stage.

Earlier attempts measured the wrong things: painted pixels (which include
Estonia's grey coverage wash, so they track product extent, not rain) and a
hand-picked bbox too small to stand for the antenna's half of the country.
This measures rain only, over Surgavere's actual Voronoi half of the EE
product, at every stage the pixel passes through:

  classified -> source beam repair -> polar passes -> recolor+fade -> the
  composite frame the page actually serves.

Run for the slots under suspicion; the stage where the count collapses is
the culprit.
"""
import datetime as dt
import io
import sys

sys.path.insert(0, ".")

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from wxfusion import config, proj3059 as P, radar_store as R  # noqa: E402
from wxfusion.scrape_maps import (RADAR_SITES, _classify_intensity,  # noqa: E402
                                  _close_radial_spokes, _despeckle,
                                  _despeckle_intensity, _fade_edges,
                                  _prepare_source, _recolor,
                                  _remove_beam_lines, _remove_beams_polar,
                                  _freezing_mask)

s3 = R.r2_client()
DAY = dt.date(2026, 8, 22)
SLOTS = [dt.datetime(2026, 8, 22, h, m, tzinfo=dt.timezone.utc)
         for h, m in ((12, 15), (12, 30), (12, 45), (13, 0))]

sites = [(n, la, lo) for n, la, lo in RADAR_SITES if n.startswith("EE")]
yy, xx = np.mgrid[0:P.H, 0:P.W]
dists = []
for n, la, lo in sites:
    x, y = P.to_xy(lo, la)
    sx = (x - P.X0) / (P.X1 - P.X0) * P.W
    sy = (P.Y1 - y) / (P.Y1 - P.Y0) * P.H
    dists.append(np.hypot(xx - sx, yy - sy))
near = np.stack(dists).argmin(0)
SURG = near == [n for n, _, _ in sites].index("EE Surgavere")
print("Surgavere half:", int(SURG.sum()), "px of", P.W * P.H)

idx = R._load_index(s3, DAY)
arch = None
try:
    import json
    arch = json.loads(s3.get_object(Bucket=config.R2_BUCKET,
                                    Key=R.ARCHIVE_KEY)["Body"].read())
except Exception as e:
    print("archive unreadable:", e)

for slot in SLOTS:
    cands = []
    for f in idx["frames"]:
        if f.get("code") != "EE":
            continue
        t = dt.datetime.strptime(f["time"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc)
        gap = abs((t - slot).total_seconds())
        if gap <= R.QUARTER_FILL_MIN * 60:
            cands.append((gap, t, f))
    if not cands:
        print(f"{slot:%H:%M}Z  EE: no candidate")
        continue
    gap, t, f = R._pick(cands, R.SLOT_TOL_MIN * 60)
    body = s3.get_object(Bucket=config.R2_BUCKET, Key=f["key"])["Body"].read()
    arr = np.array(Image.open(io.BytesIO(body)).convert("RGBA"))

    plain = _classify_intensity(arr, "EE")
    g_plain = P.resample_mercator_image(plain, tuple(f["sw"]), tuple(f["ne"]))
    cls = _prepare_source(arr, "EE", f)          # + beam repair + calibration
    g_prep = P.resample_mercator_image(cls, tuple(f["sw"]), tuple(f["ne"]))
    g_pol = _close_radial_spokes(_despeckle_intensity(
        _remove_beam_lines(_remove_beams_polar(_despeckle(g_prep)))))

    rgba = _recolor(g_pol, _freezing_mask(slot), "EE", None)
    a = np.array(rgba)[..., 3]
    canvas = Image.new("RGBA", (P.W, P.H), (0, 0, 0, 0))
    canvas.alpha_composite(rgba)
    faded = np.array(_fade_edges(canvas))[..., 3]

    def n(g):
        return int(((g > 0) & SURG).sum())

    line = (f"{slot:%H:%M}Z  sweep {t:%H:%M}{'*' if gap > R.SLOT_TOL_MIN*60 else ''}"
            f"  classified={n(g_plain)}  prepared={n(g_prep)}"
            f"  polar={n(g_pol)}  alpha>0={int(((a > 0) & SURG).sum())}"
            f"  alpha>40={int(((a > 40) & SURG).sum())}"
            f"  after_edgefade={int(((faded > 40) & SURG).sum())}")
    if arch:
        key = slot.strftime("%Y%m%dT%H%M")
        vtag = (arch.get("quarters", {}).get(key)
                or arch.get("hours", {}).get(slot.strftime("%Y%m%dT%H")))
        if vtag:
            try:
                fb = s3.get_object(Bucket=config.R2_BUCKET,
                                   Key=f"{R.FRAME_PREFIX}/{vtag}.png")["Body"].read()
                fa = np.array(Image.open(io.BytesIO(fb)).convert("RGB"))
                spread = fa.max(2).astype(int) - fa.min(2).astype(int)
                line += f"  | composite {vtag}: coloured={int(((spread > 25) & SURG).sum())}"
            except Exception as e:
                line += f"  | composite {vtag}: {e}"
    print(line)
