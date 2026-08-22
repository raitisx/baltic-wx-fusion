"""Is the SE (SMHI) layer running hot? Calibrate against the neighbours.

For every hour of the last 48 with a stored SE sweep, load the SE level grid
and each co-timed neighbour (FI real rates; EE/LV/LT2 palette-classified the
way the composite does it), and compare rain-rate estimates on the pixels
both feeds call wet. Median log-ratio per pair -> the implied dBZ offset for
the Marshall-Palmer decode (R ~ Z^(1/1.6), so dZ = 16*log10(ratio)).
Throwaway probe, dispatched via missing-probe.yml.
"""
import datetime as dt
import io
import logging
import math
import sys

logging.basicConfig(level=logging.WARNING)
sys.path.insert(0, ".")

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from wxfusion import config, proj3059 as P, radar_store as R  # noqa: E402
from wxfusion.maps_meps import pos_rain  # noqa: E402
from wxfusion.scrape_maps import LEV_MAX, _prepare_source  # noqa: E402

from wxfusion import radar_nordic as N  # noqa: E402

s3 = R.r2_client()
N.reindex(s3)          # the cancelled backfill left stored pngs unindexed
now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0,
                                               microsecond=0)
days = {}


def idx(d):
    if d not in days:
        days[d] = R._load_index(s3, d)
    return days[d]


def lev_of(f):
    body = s3.get_object(Bucket=config.R2_BUCKET, Key=f["key"])["Body"].read()
    if f.get("grid3059"):
        return np.array(Image.open(io.BytesIO(body)).convert("L"), np.uint8)
    arr = np.array(Image.open(io.BytesIO(body)).convert("RGBA"))
    cls = _prepare_source(arr, f["code"], f)
    return P.resample_mercator_image(cls, tuple(f["sw"]), tuple(f["ne"]))


def rate(lev):
    return pos_rain((lev.astype(np.float64) - 0.5) / LEV_MAX)


pairs = {}
for back in range(14 * 24):
    hour = now - dt.timedelta(hours=back)
    pool = (idx(hour.date())["frames"]
            + idx((hour - dt.timedelta(hours=1)).date())["frames"])
    best = {}
    for f in pool:
        t = dt.datetime.strptime(f["time"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc)
        gap = abs((t - hour).total_seconds())
        if gap <= 8 * 60 and (f["code"] not in best or gap < best[f["code"]][0]):
            best[f["code"]] = (gap, f)
    grids = {}
    # Cheap nordic grids first; skip a bone-dry hour before the expensive
    # palette classification of the meteolapa neighbours.
    for code in ("SE", "FI"):
        if code in best:
            try:
                grids[code] = lev_of(best[code][1])
            except Exception:
                pass
    if not any((g > 0).sum() >= 300 for g in grids.values()):
        continue
    for code in ("EE", "LV", "LT2"):
        if code in best:
            try:
                grids[code] = lev_of(best[code][1])
            except Exception:
                pass
    for a, b in (("SE", "FI"), ("SE", "EE"), ("SE", "LV"), ("SE", "LT2"),
                 ("FI", "EE"), ("FI", "LV")):
        if a not in grids or b not in grids:
            continue
        both = (grids[a] > 0) & (grids[b] > 0)
        n = int(both.sum())
        if n < 200:
            continue
        lr = np.log10(rate(grids[a][both])) - np.log10(rate(grids[b][both]))
        pairs.setdefault((a, b), []).append((n, lr))
        if n >= 1000:
            print(f"{hour:%d.%m %H}Z {a} vs {b}: {n} co-wet px, "
                  f"median ratio x{10 ** float(np.median(lr)):.2f}, "
                  f"{a} lev p50/p90 {np.percentile(grids[a][both], 50):.0f}/"
                  f"{np.percentile(grids[a][both], 90):.0f}, "
                  f"{b} lev p50/p90 {np.percentile(grids[b][both], 50):.0f}/"
                  f"{np.percentile(grids[b][both], 90):.0f}")

print("\n=== summary ===")
for (a, b), rows in pairs.items():
    all_lr = np.concatenate([lr for _, lr in rows])
    med = float(np.median(all_lr))
    print(f"{a} vs {b}: {sum(n for n, _ in rows)} px over {len(rows)} hours, "
          f"median rate ratio x{10 ** med:.2f} "
          f"(p25 x{10 ** float(np.percentile(all_lr, 25)):.2f}, "
          f"p75 x{10 ** float(np.percentile(all_lr, 75)):.2f}), "
          f"implied MP dBZ offset {16 * med:+.1f} dB")
if not pairs:
    print("no co-wet overlap found")
