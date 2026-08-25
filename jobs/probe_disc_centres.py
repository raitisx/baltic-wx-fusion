"""How does Surgavere classify, judged only where Surgavere alone can see?

The crescent is the attributable part of a composite: inside one dish's range
and outside the other's. So this reads the Estonian product over a rainy
window and asks three things of Surgavere's crescent —

  1. what classes it puts there, against Harku's crescent on the same frames,
  2. whether the classes fall off with range (the thing the range-graded
     sensitivity is meant to correct), by 50 km rings from the dish,
  3. how it compares with a NEIGHBOUR watching the same pixels from its own
     side — Latvia and Lithuania cover much of Surgavere's crescent, Finland
     covers Harku's — since one radar can only be called hot or cold against
     another.

Levels are the shared 0..96 ramp (6 classes x 16), so a level difference of
16 is one whole class.
"""
import base64
import datetime as dt
import io
import sys

sys.path.insert(0, ".")

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from wxfusion import proj3059 as P, radar_store as R  # noqa: E402
from wxfusion.scrape_maps import (SUB, draw_borders, lev_class,  # noqa: E402
                                  pos_rain, ramp_lut)

s3 = R.r2_client()
DAY = dt.date(2026, 8, 22)     # the storm day: 24.08 was dry, 7 wet px a frame
LO, HI = 9, 17          # UTC hours to walk
yy, xx = np.mgrid[0:P.H, 0:P.W]

sites = R._site_pixels_for("EE", on_canvas=True)
D = {n: np.hypot(xx - sx, yy - sy) for n, sx, sy in sites}
CRES = {}
for n in D:
    others = [v for k, v in D.items() if k != n]
    CRES[n] = (D[n] <= R.SPLIT_RANGE_KM) & np.all(
        [o > R.SPLIT_RANGE_KM for o in others], axis=0)
for n, m in CRES.items():
    print(f"{n:14s} crescent {int(m.sum()):6d} px")

idx = R._load_index(s3, DAY)
frames = {}
for f in idx.get("frames", []):
    t = dt.datetime.strptime(f["time"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=dt.timezone.utc)
    if LO <= t.hour < HI:
        frames.setdefault(f["code"], []).append((t, f))


def nearest(code, when, tol=900):
    best = None
    for t, f in frames.get(code, []):
        gap = abs((t - when).total_seconds())
        if gap <= tol and (best is None or gap < best[0]):
            best = (gap, t, f)
    return best


def stats(lev, mask):
    v = lev[mask]
    wet = v[v > 0]
    if not wet.size:
        return None
    cls = lev_class(wet)
    hist = np.bincount(cls, minlength=7)[1:7]
    return wet.size, float(np.median(wet)), hist


print(f"\n=== {DAY} {LO:02d}-{HI:02d}Z, Estonia by crescent ===")
print(f"{'slot':6s} {'Surgavere wet/med/classes':44s} {'Harku wet/med/classes'}")
rings, ring_lab = {}, [(0, 50), (50, 100), (100, 150), (150, 200), (200, 250)]
pairs = {}
shot = None
for hh in range(LO, HI):
    for mm in (0, 30):
        when = dt.datetime(DAY.year, DAY.month, DAY.day, hh, mm,
                           tzinfo=dt.timezone.utc)
        got = nearest("EE", when)
        if not got:
            continue
        _, t, f = got
        try:
            lev, _ = R._feed_grid(s3, "EE", f)
        except Exception as e:
            print(f"{hh:02d}:{mm:02d}  load failed {type(e).__name__}")
            continue
        line = []
        for n in ("EE Surgavere", "EE Harku"):
            st = stats(lev, CRES[n])
            line.append("-" if st is None else
                        f"{st[0]:6d} med{st[1]:5.1f} " + "/".join(
                            str(int(c)) for c in st[2]))
        print(f"{hh:02d}:{mm:02d}  {line[0]:44s} {line[1]}")
        # range rings inside Surgavere's crescent
        m = CRES["EE Surgavere"]
        for lo_, hi_ in ring_lab:
            r = m & (D["EE Surgavere"] >= lo_) & (D["EE Surgavere"] < hi_)
            v = lev[r]
            wet = v[v > 0]
            if wet.size:
                rings.setdefault((lo_, hi_), []).append(
                    (wet.size, float(np.median(wet)), int(r.sum())))
        # neighbours over the same crescent
        for code, who in (("LV", "EE Surgavere"), ("LT2", "EE Surgavere"),
                          ("FI", "EE Harku")):
            g2 = nearest(code, when)
            if not g2:
                continue
            try:
                lev2, _ = R._feed_grid(s3, code, g2[2])
            except Exception:
                continue
            both = CRES[who] & (lev > 0) & (lev2 > 0)
            if int(both.sum()) < 100:
                continue
            a = pos_rain((lev[both].astype(float) - 0.5) / 96)
            b = pos_rain((lev2[both].astype(float) - 0.5) / 96)
            pairs.setdefault((who, code), []).append(
                (int(both.sum()), float(np.median(a) / max(1e-6,
                                                           np.median(b)))))
        if shot is None and stats(lev, CRES["EE Surgavere"]):
            shot = (t, lev.copy())

print("\n=== inside Surgavere's crescent, by range from the dish ===")
for key in ring_lab:
    v = rings.get(key)
    if not v:
        print(f"  {key[0]:3d}-{key[1]:3d} km: nothing")
        continue
    wet = sum(x[0] for x in v)
    area = v[0][2]
    med = float(np.median([x[1] for x in v]))
    print(f"  {key[0]:3d}-{key[1]:3d} km: {area:6d} px of crescent, "
          f"wet {wet / len(v):8.0f} px/frame ({100 * wet / len(v) / max(1, area):4.1f}%), "
          f"median level {med:5.1f}  (class {int(lev_class(med))})")

print("\n=== against a neighbour watching the same pixels ===")
for (who, code), v in sorted(pairs.items()):
    n = sum(x[0] for x in v)
    ratio = float(np.median([x[1] for x in v]))
    print(f"  {who:14s} vs {code:4s}: {len(v)} frames, {n:,} co-wet px, "
          f"median rate ratio {ratio:5.2f}x  "
          f"({'EE reads high' if ratio > 1.15 else 'EE reads low' if ratio < 0.87 else 'in line'})")

if shot is not None:
    t, lev = shot
    rgba = ramp_lut(False)[np.clip(lev, 0, 96).astype(np.uint8)]
    rgba = rgba.copy()
    for n, col in (("EE Surgavere", (255, 90, 90)), ("EE Harku", (90, 160, 255))):
        edge = CRES[n] ^ (np.roll(CRES[n], 1, 0) & np.roll(CRES[n], 1, 1))
        rgba[edge] = (*col, 255)
    im = Image.fromarray(np.ascontiguousarray(rgba, np.uint8), "RGBA")
    draw_borders(im)
    bg = Image.new("RGB", im.size, (247, 245, 239))
    bg.paste(im, (0, 0), im)
    buf = io.BytesIO()
    bg.save(buf, "PNG", optimize=True)
    print(f"\nframe shown: {t:%H:%M}Z")
    print("@@IMG:crescents:" + base64.b64encode(buf.getvalue()).decode())
