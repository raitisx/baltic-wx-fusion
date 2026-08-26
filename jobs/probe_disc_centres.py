"""Southern Lithuania is empty in our render. Which stage empties it?

The source paints echo down there; our LT2 render, and both antenna panels
cut from it, are blank below roughly 55 N. Run one sweep through the whole
chain and count the echo in that band at every stage, then look at what the
source actually puts there, colour by colour.
"""
import datetime as dt
import io
import sys

sys.path.insert(0, ".")

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from wxfusion import config, proj3059 as P, radar_store as R  # noqa: E402
from wxfusion import scrape_maps as S  # noqa: E402

s3 = R.r2_client()
# The band the user circled: southern Lithuania and over the border.
Y0, Y1, X0, X1 = 530, 690, 60, 470
roi = np.zeros((P.H, P.W), bool)
roi[Y0:Y1, X0:X1] = True
print(f"ROI rows {Y0}..{Y1}, cols {X0}..{X1} "
      f"({P.to_lonlat((P.X0 + P.X1) / 2, P.Y1 - Y1 * (P.Y1 - P.Y0) / P.H)[1]:.2f}"
      f"..{P.to_lonlat((P.X0 + P.X1) / 2, P.Y1 - Y0 * (P.Y1 - P.Y0) / P.H)[1]:.2f} N)")


def n(a, m=None):
    b = a > 0
    return int((b & roi).sum()), int(b.sum())


for day in (dt.date(2026, 8, 22), dt.date(2026, 8, 23)):
    idx = R._load_index(s3, day)
    lt = sorted((f for f in idx.get("frames", [])
                 if f.get("code") == "LT2" and not f.get("grid3059")),
                key=lambda f: f["time"])
    if not lt:
        print(f"\n{day}: no LT2 frames")
        continue
    tgt = dt.datetime.combine(day, dt.time(12, 13), tzinfo=dt.timezone.utc)
    f = min(lt, key=lambda f: abs(
        (dt.datetime.strptime(f["time"], "%Y-%m-%dT%H:%M:%SZ")
         .replace(tzinfo=dt.timezone.utc) - tgt).total_seconds()))
    print(f"\n=== {day} sweep {f['time']} ===")
    body = s3.get_object(Bucket=config.R2_BUCKET, Key=f["key"])["Body"].read()
    arr = np.array(Image.open(io.BytesIO(body)).convert("RGBA"))
    sw, ne = tuple(f["sw"]), tuple(f["ne"])

    raw = P.resample_mercator_image(arr, sw, ne)
    painted = (raw[..., 3] > 60)
    print(f"  source painted            ROI {int((painted & roi).sum()):7,d}"
          f"   all {int(painted.sum()):8,d}")

    plain = S._classify_intensity(arr, "LT2")
    prep = S._prepare_source_raw(arr, "LT2", f)
    cal = S._calibrate_lev(prep, "LT2")
    for name, g in (("classify (source frame)", plain),
                    ("after source beam work", prep),
                    ("after calibration", cal)):
        # these are in the SOURCE frame, so warp before counting on our grid
        w = P.resample_mercator_image(g, sw, ne)
        a, b = n(w)
        print(f"  {name:25s} ROI {a:7,d}   all {b:8,d}")

    grid = P.resample_mercator_image(cal, sw, ne)
    stages = [("warped to canvas", grid)]
    g = S._despeckle(grid);                stages.append(("despeckle", g))
    g = S._remove_beams_polar(g);          stages.append(("beams polar", g))
    g = S._remove_beam_lines(g);           stages.append(("beam lines", g))
    g = S._despeckle_intensity(g);         stages.append(("despeckle intensity", g))
    g = S._close_radial_spokes(g);         stages.append(("close spokes", g))
    for name, gg in stages:
        a, b = n(gg)
        print(f"  {name:25s} ROI {a:7,d}   all {b:8,d}")

    rgba = np.array(S._recolor(g, None, "LT2", None))
    vis = rgba[..., 3] > 8
    print(f"  {'after recolor + fade':25s} ROI {int((vis & roi).sum()):7,d}"
          f"   all {int(vis.sum()):8,d}")

    # What IS the source painting in that band, colour by colour?
    src_roi = raw[roi & painted]
    if src_roi.size:
        cols, cnt = np.unique(src_roi[:, :3].reshape(-1, 3), axis=0,
                              return_counts=True)
        order = np.argsort(-cnt)[:12]
        print("  top source colours in the ROI, and the level they classify to:")
        for i in order:
            c = cols[i]
            px = np.zeros((1, 1, 4), np.uint8)
            px[0, 0, :3] = c
            px[0, 0, 3] = 255
            lev = int(S._classify_intensity(px, "LT2")[0, 0])
            print(f"    rgb{tuple(int(x) for x in c)}  {int(cnt[i]):6,d} px"
                  f"   -> level {lev}{'   DROPPED' if lev == 0 else ''}")
