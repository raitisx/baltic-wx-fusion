"""ilm__cmp_cap works. Now: what are the numbers, and can we ask for a slice?

DescribeCoverage says "Komposiit radari pilt", ImageMosaic, EPSG:3301,
2844x2844 at 360 m, float32, nodata 65535, TIME every 5 minutes over the last
~2 days. The full grid is 37 MB, which is no way to fetch a frame — so this
checks the two things that decide whether Estonia can be read numerically:
what the values mean, and whether a bbox + time subset works.
"""
import base64
import io
import sys

sys.path.insert(0, ".")

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from wxfusion.http import session  # noqa: E402

s = session()
OWS = "https://ilmgs.envir.ee/geoserver/ows"
COV = "ilm__cmp_cap"
# our canvas in LEST-97 (EPSG:3301) — the Baltic bbox reprojected
from pyproj import Transformer  # noqa: E402

from wxfusion import proj3059 as P  # noqa: E402
fwd = Transformer.from_crs("EPSG:3059", "EPSG:3301", always_xy=True)
xs, ys = [], []
for x in (P.X0, P.X1):
    for y in (P.Y0, P.Y1):
        a, b = fwd.transform(x, y)
        xs.append(a)
        ys.append(b)
X0, X1, Y0, Y1 = min(xs), max(xs), min(ys), max(ys)
print(f"our canvas in EPSG:3301: X {X0:.0f}..{X1:.0f}  Y {Y0:.0f}..{Y1:.0f}")


def fetch(tag, url):
    try:
        r = s.get(url, timeout=120)
        print(f"\n--- {tag}: {r.status_code} {len(r.content):,}B "
              f"{r.headers.get('content-type','?')[:28]}")
        if not r.ok:
            import re as _re
            body = _re.sub(r"\s+", " ", r.text)
            for m in _re.finditer(r'exceptionCode="([^"]+)"|locator="([^"]+)"'
                                  r'|<ows:ExceptionText>([^<]+)<', body):
                print("     !", next(g for g in m.groups() if g)[:160])
            return None
        return r.content
    except Exception as e:
        print(f"\n--- {tag}: FAIL {type(e).__name__}")
        return None


import rasterio  # noqa: E402

TIME = "2026-08-25T02:50:00.000Z"
# The envelope reads axisLabels="Y X" with lowerCorner "5992826 40407", so
# their X is the NORTHING and their Y the easting — the opposite way round
# from the request I first sent, which is why every bbox came back as an
# empty intersection. Try it both ways and keep whichever answers.
AX = [("X", "Y", "swapped: X=northing"), ("Y", "X", "plain: X=easting")]
print("\n=== which axis labels does it accept? ===")
for ax, ay, note in AX:
    # ax gets the NORTHING range, ay the easting range
    u = (f"{OWS}?service=WCS&version=2.0.1&request=GetCoverage"
         f"&coverageId={COV}&format=image/tiff"
         f"&subset={ax}({Y0:.0f},{Y1:.0f})&subset={ay}({X0:.0f},{X1:.0f})")
    body = fetch(f"{note}", u)
    if body:
        with rasterio.open(io.BytesIO(body)) as ds:
            print(f"    OK {ds.width}x{ds.height} {ds.crs} "
                  f"px={abs(ds.transform.a):.0f} m")
        AXIS = (ax, ay)
        break
else:
    AXIS = ("X", "Y")

print("\n=== and with a time slice ===")
for tf in ('"{t}"', '{t}', '"2026-08-25T02:50:00Z"'):
    t = tf.format(t=TIME)
    body = fetch(f"time {t}",
                 f"{OWS}?service=WCS&version=2.0.1&request=GetCoverage"
                 f"&coverageId={COV}&format=image/tiff"
                 f"&subset={AXIS[0]}({Y0:.0f},{Y1:.0f})"
                 f"&subset={AXIS[1]}({X0:.0f},{X1:.0f})"
                 f"&subset=time({t})")
    if body:
        with rasterio.open(io.BytesIO(body)) as ds:
            v = ds.read(1)
            good = v[(v > -9000) & (v < 60000)]
            wet = good[good > 0]
            print(f"    OK {ds.width}x{ds.height} px={abs(ds.transform.a):.0f} m"
                  f"  valid {good.size:,}  wet {wet.size:,}")
            if wet.size:
                import numpy as _np
                print("    wet percentiles 5/50/95/99:",
                      [f"{x:.2f}" for x in
                       _np.percentile(wet, [5, 50, 95, 99])],
                      f" max {wet.max():.2f}")
        break

for tag, url in (
    ("subset bbox",
     f"{OWS}?service=WCS&version=2.0.1&request=GetCoverage&coverageId={COV}"
     f"&format=image/tiff"
     f"&subset=X({X0:.0f},{X1:.0f})&subset=Y({Y0:.0f},{Y1:.0f})"),
    ("subset bbox + time",
     f"{OWS}?service=WCS&version=2.0.1&request=GetCoverage&coverageId={COV}"
     f"&format=image/tiff"
     f"&subset=X({X0:.0f},{X1:.0f})&subset=Y({Y0:.0f},{Y1:.0f})"
     f"&subset=time(\"{TIME}\")"),
):
    body = fetch(tag, url)
    if not body:
        continue
    with rasterio.open(io.BytesIO(body)) as ds:
        v = ds.read(1)
        good = v[(v > -9000) & (v < 60000)]
        print(f"    {ds.width}x{ds.height} {v.dtype} {ds.crs} "
              f"px={abs(ds.transform.a):.0f} m")
        if good.size:
            wet = good[good > 0]
            print(f"    valid {good.size:,} px  min {good.min():.2f} "
                  f"max {good.max():.2f}  zeros {(good == 0).sum():,}")
            if wet.size:
                q = np.percentile(wet, [5, 25, 50, 75, 95, 99])
                print("    non-zero percentiles 5/25/50/75/95/99:",
                      [f"{x:.2f}" for x in q], f"  n={wet.size:,}")
        if tag.endswith("time"):
            g = np.clip((v - 0) / max(1e-3, float(good.max() if good.size else 1)),
                        0, 1)
            g = np.where((v > -9000) & (v < 60000), g, 0)
            im = Image.fromarray((255 - g * 255).astype(np.uint8), "L")
            im.thumbnail((760, 760))
            buf = io.BytesIO()
            im.save(buf, "PNG", optimize=True)
            print("@@IMG:ee_wcs:" + base64.b64encode(buf.getvalue()).decode())
