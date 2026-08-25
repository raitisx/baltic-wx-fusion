"""Which feed is actually off? Every pair, in their own overlaps.

Surgavere's crescent came out 1.22x against Lithuania and 0.39x against
Latvia on the same day — both cannot be right about Surgavere, so the answer
has to come from the whole ring of feeds rather than one pair. This walks
22.08 and, for every pair that overlaps, takes the pixels where BOTH are wet
and reports the median ratio of their rain rates.

Twice over, because range confounds it: once on all co-wet pixels, and once
on the NEAR FIELD only — pixels within 150 km of one of each feed's own
antennas — where every radar is at its most trustworthy. A feed that is out
of line in both is out of line for real.
"""
import datetime as dt
import itertools
import sys

sys.path.insert(0, ".")

import numpy as np  # noqa: E402

from wxfusion import proj3059 as P, radar_store as R  # noqa: E402
from wxfusion.scrape_maps import pos_rain  # noqa: E402

s3 = R.r2_client()
DAY = dt.date(2026, 8, 22)
LO, HI = 9, 17
CODES = ("EE", "LV", "LT2", "SE", "FI")
NEAR_KM = 150.0
yy, xx = np.mgrid[0:P.H, 0:P.W]

# near field: within NEAR_KM of any antenna the feed actually owns
NEAR = {}
for c in CODES:
    sites = R._site_pixels_for(c, on_canvas=True)
    if not sites:
        NEAR[c] = None
        print(f"{c:4s}: no antennas listed — near field not defined")
        continue
    d = np.full((P.H, P.W), np.inf, np.float32)
    for _, sx, sy in sites:
        d = np.minimum(d, np.hypot(xx - sx, yy - sy).astype(np.float32))
    NEAR[c] = d <= NEAR_KM
    print(f"{c:4s}: {len(sites)} antennas, near field {int(NEAR[c].sum()):6d} px"
          f"  ({', '.join(n for n, _, _ in sites)})")

idx = R._load_index(s3, DAY)
frames = {}
for f in idx.get("frames", []):
    t = dt.datetime.strptime(f["time"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=dt.timezone.utc)
    if LO <= t.hour < HI:
        frames.setdefault(f["code"], []).append((t, f))
print("\nframes in window:", {c: len(v) for c, v in sorted(frames.items())})


def nearest(code, when, tol=900):
    best = None
    for t, f in frames.get(code, []):
        gap = abs((t - when).total_seconds())
        if gap <= tol and (best is None or gap < best[0]):
            best = (gap, t, f)
    return best


def rate(lev, m):
    return pos_rain((lev[m].astype(np.float64) - 0.5) / 96)


all_px, near_px, own = {}, {}, {}
slots = 0
for hh in range(LO, HI):
    for mm in (0, 30):
        when = dt.datetime(DAY.year, DAY.month, DAY.day, hh, mm,
                           tzinfo=dt.timezone.utc)
        grids = {}
        for c in CODES:
            got = nearest(c, when)
            if not got:
                continue
            try:
                grids[c], _ = R._feed_grid(s3, c, got[2])
            except Exception as e:
                print(f"  {hh:02d}:{mm:02d} {c}: {type(e).__name__}")
        if len(grids) < 2:
            continue
        slots += 1
        for c, g in grids.items():
            if NEAR[c] is not None:
                w = g[NEAR[c]]
                w = w[w > 0]
                if w.size:
                    own.setdefault(c, []).append(float(np.median(w)))
        for a, b in itertools.combinations(sorted(grids), 2):
            ga, gb = grids[a], grids[b]
            wet = (ga > 0) & (gb > 0)
            if int(wet.sum()) >= 100:
                r = np.median(rate(ga, wet)) / max(1e-9, np.median(rate(gb, wet)))
                all_px.setdefault((a, b), []).append((int(wet.sum()), float(r)))
            if NEAR[a] is None or NEAR[b] is None:
                continue
            wn = wet & NEAR[a] & NEAR[b]
            if int(wn.sum()) >= 100:
                r = np.median(rate(ga, wn)) / max(1e-9, np.median(rate(gb, wn)))
                near_px.setdefault((a, b), []).append((int(wn.sum()), float(r)))

print(f"\n{slots} slots with two or more feeds\n")
print(f"{'pair':12s} {'all co-wet':>26s}   {'near field (<150 km both)':>30s}")
for pair in sorted(set(all_px) | set(near_px)):
    a, b = pair
    out = []
    for src in (all_px, near_px):
        v = src.get(pair)
        if not v:
            out.append(f"{'-':>26s}")
            continue
        n = sum(x[0] for x in v)
        r = float(np.median([x[1] for x in v]))
        out.append(f"{len(v):2d} frames {n:9,d} px {r:5.2f}x")
    print(f"{a:4s} / {b:4s} {out[0]}   {out[1]}")

print("\nmedian wet level in each feed's OWN near field (levels 0..96):")
for c in sorted(own):
    print(f"  {c:4s} {float(np.median(own[c])):5.1f}  ({len(own[c])} frames)")
