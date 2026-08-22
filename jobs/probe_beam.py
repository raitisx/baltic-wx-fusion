"""One-off: why did the LV interference cone at 2026-08-21 21:00 survive the
beam cleaning? Rebuilds that stored LV source through the exact clean chain
render_stored uses, with RADAR_BEAM_DEBUG on, and prints the per-component
diagnosis (shape gates + embedded veto). Writes nothing. Throwaway."""
import datetime as dt
import logging
import os
import sys

os.environ["RADAR_BEAM_DEBUG"] = "1"
logging.basicConfig(level=logging.INFO, format="%(message)s")
sys.path.insert(0, ".")

import numpy as np  # noqa: E402
from wxfusion import proj3059 as P, radar_store as R  # noqa: E402
from wxfusion.scrape_maps import (_close_radial_spokes, _despeckle,  # noqa: E402
                                  _despeckle_intensity, _remove_beam_lines,
                                  _remove_beams_polar)

HOUR = dt.datetime(2026, 8, 21, 21, tzinfo=dt.timezone.utc)
s3 = R.r2_client()
idx = R._load_index(s3, HOUR.date())
idxp = R._load_index(s3, (HOUR - dt.timedelta(hours=1)).date())
pool = idx["frames"] + idxp["frames"]

# nearest LV sweep to the hour
best = None
for f in pool:
    if f.get("code") != "LV":
        continue
    t = dt.datetime.strptime(f["time"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    gap = abs((t - HOUR).total_seconds())
    if gap <= R.NEAR_MIN * 60 and (best is None or gap < best[0]):
        best = (gap, t, f)

if not best:
    print("no LV sweep near", HOUR)
    raise SystemExit(0)

_, t, f = best
print(f"LV sweep {t:%Y-%m-%d %H:%M} ({round((t-HOUR).total_seconds()/60):+d} min), key {f['key']}")
cls = R._classify_stored(s3, f)                      # runs _prepare_source (+ per-source repair)
grid = P.resample_mercator_image(cls, tuple(f["sw"]), tuple(f["ne"]))
print(f"echo after resample+source-repair: {int((grid>0).sum())} px")
print("--- composite beam passes (RADAR_BEAM_DEBUG) ---")
g1 = _despeckle(grid)
g2 = _remove_beams_polar(g1)
print(f"after _remove_beams_polar: {int((g2>0).sum())} px (was {int((g1>0).sum())})")
g3 = _remove_beam_lines(g2)
print(f"after _remove_beam_lines: {int((g3>0).sum())} px")
g4 = _close_radial_spokes(_despeckle_intensity(g3))
print(f"final: {int((g4>0).sum())} px")
print("DONE")
