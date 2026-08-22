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

s3 = R.r2_client()
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
for back in range(48):
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
    if "SE" not in best:
        continue
    try:
        se = lev_of(best["SE"][1])
    except Exception as e:
        print(f"{hour:%d.%m %H}Z SE load failed: {e}")
        continue
    for code in ("FI", "EE", "LV", "LT2"):
        if code not in best:
            continue
        try:
            other = lev_of(best[code][1])
        except Exception:
            continue
        both = (se > 0) & (other > 0)
        n = int(both.sum())
        if n < 200:
            continue
        lr = np.log10(rate(se[both])) - np.log10(rate(other[both]))
        pairs.setdefault(code, []).append((n, lr))
        print(f"{hour:%d.%m %H}Z SE vs {code}: {n} co-wet px, "
              f"median ratio x{10 ** float(np.median(lr)):.2f}, "
              f"SE lev p50/p90 {np.percentile(se[both], 50):.0f}/"
              f"{np.percentile(se[both], 90):.0f}, "
              f"{code} lev p50/p90 {np.percentile(other[both], 50):.0f}/"
              f"{np.percentile(other[both], 90):.0f}")

print("\n=== summary ===")
for code, rows in pairs.items():
    all_lr = np.concatenate([lr for _, lr in rows])
    med = float(np.median(all_lr))
    print(f"SE vs {code}: {sum(n for n, _ in rows)} px over {len(rows)} hours, "
          f"median rate ratio x{10 ** med:.2f} "
          f"(p25 x{10 ** float(np.percentile(all_lr, 25)):.2f}, "
          f"p75 x{10 ** float(np.percentile(all_lr, 75)):.2f}), "
          f"implied MP dBZ offset {16 * med:+.1f} dB")
if not pairs:
    print("no co-wet overlap found in the last 48 h")
