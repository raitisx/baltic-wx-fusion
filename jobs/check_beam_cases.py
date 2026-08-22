"""Regression check for the radar beam cleaning, against real recorded cases.

Every time the beam algorithm changes, run this (workflow: beam-cases.yml).
It replays each recorded case through the exact cleaning chain the composite
uses — stored source -> _prepare_source (classify + source-space beam passes +
ridge repair) -> reproject -> composite polar passes — and checks the outcome:

  kind="beam":    the recorded interference must be (mostly) REMOVED — leftover
                  echo in the recorded sector beyond the floor fails the case.
  kind="weather": the recorded real echo must SURVIVE — losing most of it to
                  the cleaning fails the case (the passes exist to remove
                  beams, not showers).

Sources are kept 365 days (SRC_KEEP_DAYS), so old cases stay replayable; a
case whose source has aged out is reported and skipped.

THE CASE BOOK — user-reported sightings and the ones dug out of the code's own
history. The repeat offenders all sit E-SE of RIGA between ~97 and ~151 deg:
the same bearings come back week after week, which smells like fixed RLAN/WiFi
interferers rather than weather-dependent artefacts, so any change to the
gates should be checked against this whole sector, not just the newest
sighting.
"""
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
from wxfusion.scrape_maps import (RADAR_SITES, _close_radial_spokes,  # noqa: E402
                                  _despeckle, _despeckle_intensity,
                                  _prepare_source, _remove_beam_lines,
                                  _remove_beams_polar)

CASES = [
    # --- interference cones (must be removed) ---------------------------------
    # 31.07 22:01 LV Riga: clean spike at 97.0-98.5 deg out to 244 km against
    # flanks at 194 (the case that set REACH_LIFT_KM=45).
    dict(kind="beam", t="2026-07-31T22:01", feed="LV", site="LV Riga",
         az=(95.0, 100.0), rng=(30, 300)),
    # 02.08 01:00 LV Riga: ray buried in rain around the antenna — in polar
    # space rain+ray were one blob, no shape to test (the case behind the
    # ridge/rebuild pass). Azimuth not recorded at the time; sector left wide.
    dict(kind="beam", t="2026-08-02T01:00", feed="LV", site="LV Riga",
         az=(90.0, 155.0), rng=(30, 300), note="azimuth approximate"),
    # 11.08 17:01 LV Riga: isolated dashed beam in a near-empty frame — trunk
    # fragments elong 22-55 at 0.5-2.5 deg (the case behind the hair gate).
    dict(kind="beam", t="2026-08-11T17:01", feed="LV", site="LV Riga",
         az=(90.0, 155.0), rng=(30, 300), note="azimuth approximate"),
    # 21.08 20:56 LV Riga: dashed cone ESE of Riga — condemned corridors at
    # 109-151 deg, fragments 48-123 km, every one a near-miss on the old gates
    # (the case behind the dash gate + the neighbour sweep).
    dict(kind="beam", t="2026-08-21T20:56", feed="LV", site="LV Riga",
         az=(105.0, 155.0), rng=(40, 200)),
    # --- real weather (must survive) ------------------------------------------
    # 17.08 15-17 UTC: rain cell E of Riga (~56.7N 26.5E) visible, gone,
    # visible again across three consecutive published frames. Diagnosed
    # 22.08: NOT deleted by the cleaning. The LV radar was near-blank those
    # hours (403 / 1 / 37 echo px in the whole frame), so the only witness was
    # Vilnius (LT2) at ~240 km — beyond LT's 180 km weak-echo fade. The source
    # itself weakened at 16:01 (66 px vs 234 at 15:01), and the fade finished
    # the job. These cases pin the cleaning chain: where LT2 has the echo, the
    # despeckle/beam passes must keep it.
    dict(kind="weather", t="2026-08-17T15:01", feed="LT2",
         bbox=(56.4, 57.1, 25.9, 27.3)),
    dict(kind="weather", t="2026-08-17T16:01", feed="LT2",
         bbox=(56.4, 57.1, 25.9, 27.3)),
]

# A beam case passes when the sector holds at most this much RAY-SHAPED echo
# afterwards — thin (<=3 deg), radially elongated components. Judging by raw
# sector count failed immediately: the 02.08 sector legitimately holds ~4000 px
# of rain, and the wide "azimuth approximate" sectors sweep up scattered real
# echo. Rain blobs are not rays; a surviving beam is.
BEAM_LEFTOVER_MAX_PX = 15
# A weather case passes when at least this fraction of the classified echo in
# the bbox survives the full chain.
WEATHER_SURVIVE_FRAC = 0.6

s3 = R.r2_client()


def merc_y(la):
    r = math.radians(la)
    return math.log(math.tan(r) + 1.0 / math.cos(r))


def load_case_sweep(t_iso, feed):
    target = dt.datetime.fromisoformat(t_iso).replace(tzinfo=dt.timezone.utc)
    frames = []
    for d in (target.date(), (target - dt.timedelta(hours=1)).date()):
        frames += R._load_index(s3, d).get("frames", [])
    best = None
    for f in frames:
        if f.get("code") != feed:
            continue
        t = dt.datetime.strptime(f["time"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc)
        gap = abs((t - target).total_seconds())
        if gap <= 40 * 60 and (best is None or gap < best[0]):
            best = (gap, t, f)
    return best


def run_chain(f):
    body = s3.get_object(Bucket=R.config.R2_BUCKET, Key=f["key"])["Body"].read()
    arr = np.array(Image.open(io.BytesIO(body)).convert("RGBA"))
    cls = _prepare_source(arr, f["code"], f)
    grid = P.resample_mercator_image(cls, tuple(f["sw"]), tuple(f["ne"]))
    comp = _close_radial_spokes(_despeckle_intensity(
        _remove_beam_lines(_remove_beams_polar(_despeckle(grid)))))
    return arr, cls, grid, comp


def _site_px(site):
    la0, lo0 = next((la, lo) for n, la, lo in RADAR_SITES if n == site)
    x0, y0 = P.to_xy(lo0, la0)
    return ((x0 - P.X0) / (P.X1 - P.X0) * P.W,
            (P.Y1 - y0) / (P.Y1 - P.Y0) * P.H)


def polar_sector_mask(site, az_lo, az_hi, r_lo, r_hi):
    sx, sy = _site_px(site)
    yy, xx = np.mgrid[0:P.H, 0:P.W]
    rng = np.hypot(xx - sx, yy - sy)             # ~1 km per px
    az = (np.degrees(np.arctan2(xx - sx, -(yy - sy)))) % 360.0   # 0=N, cw
    return (rng >= r_lo) & (rng <= r_hi) & (az >= az_lo) & (az <= az_hi)


def ray_leftover_px(comp, site, sector_mask):
    """Echo px inside the sector that belong to RAY-shaped components — thin in
    azimuth (<=3 deg), long and radially elongated. Rain blobs do not qualify;
    a surviving interference beam does. Same polar framing the remover uses."""
    from scipy import ndimage
    sx, sy = _site_px(site)
    lit = (comp > 0) & sector_mask
    if not lit.any():
        return 0
    yy, xx = np.mgrid[0:P.H, 0:P.W]
    rng = np.hypot(xx - sx, yy - sy)
    az = (np.degrees(np.arctan2(xx - sx, -(yy - sy)))) % 360.0
    N_AZ, R_MAX = 720, 320
    ab = np.clip((az / 360.0 * N_AZ).astype(int), 0, N_AZ - 1)
    rb = np.clip(rng.astype(int), 0, R_MAX)
    occ = np.zeros((N_AZ, R_MAX + 1), bool)
    occ[ab[lit], rb[lit]] = True
    closed = ndimage.binary_closing(occ, structure=np.ones((1, 15), bool))
    lab, n = ndimage.label(closed, structure=np.ones((3, 3)))
    bad = np.zeros((N_AZ, R_MAX + 1), bool)
    for i, sl in enumerate(ndimage.find_objects(lab), start=1):
        a_span = (sl[0].stop - sl[0].start) * 360.0 / N_AZ
        r_span = sl[1].stop - sl[1].start
        width_km = max(1e-6, (sl[1].start + sl[1].stop) / 2.0 * np.radians(a_span))
        if a_span <= 3.0 and r_span >= 25 and (r_span / width_km) >= 6.0:
            bad |= (lab == i)
    return int((lit & bad[ab, rb]).sum())


def grid_bbox_mask(bbox):
    latS, latN, lonW, lonE = bbox
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


failures = 0
for c in CASES:
    tag = f"{c['t']} {c['feed']} {c['kind']}"
    got = load_case_sweep(c["t"], c["feed"])
    if not got:
        print(f"SKIP  {tag}: no stored sweep within 40 min (source aged out?)")
        continue
    gap, t, f = got
    try:
        arr, cls, grid, comp = run_chain(f)
    except Exception as e:
        print(f"ERROR {tag}: {type(e).__name__}: {e}")
        failures += 1
        continue
    if c["kind"] == "beam":
        m = polar_sector_mask(c["site"], *c["az"], *c["rng"])
        before = int(((grid > 0) & m).sum())
        left = ray_leftover_px(comp, c["site"], m)
        ok = left <= BEAM_LEFTOVER_MAX_PX
        print(f"{'PASS' if ok else 'FAIL'}  {tag} (sweep {t:%d.%m %H:%M}): "
              f"sector {c['az'][0]:.0f}-{c['az'][1]:.0f} deg — "
              f"{before} px echo, {left} ray-shaped px left (max {BEAM_LEFTOVER_MAX_PX})"
              + (f" [{c['note']}]" if c.get("note") else ""))
    else:
        m = grid_bbox_mask(c["bbox"])
        before = int(((grid > 0) & m).sum())
        after = int(((comp > 0) & m).sum())
        if before == 0:
            print(f"NOTE  {tag} (sweep {t:%d.%m %H:%M}): source has no echo in "
                  f"the bbox — nothing for the cleaning to lose (upstream gap)")
            continue
        ok = after >= before * WEATHER_SURVIVE_FRAC
        print(f"{'PASS' if ok else 'FAIL'}  {tag} (sweep {t:%d.%m %H:%M}): "
              f"bbox {before} px classified -> {after} px after cleaning "
              f"(need >= {WEATHER_SURVIVE_FRAC:.0%})")
    if not ok:
        failures += 1

print(f"\n{failures} failure(s)")
raise SystemExit(1 if failures else 0)
