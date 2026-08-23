"""Does the polar-volume decoder put Sweden's radars in the right place?

The decoder is written (radar_nordic._decode_smhi_vol): read the 0.5 deg
sweep of an ODIM volume, sample it by the bearing and distance of each of our
pixels from the dish. Two things must be checked before it is trusted — where
each SMHI radar actually is (their /where group, which is also what
RADAR_SITES needs), and whether the sampled field lands where the national
composite puts the same rain.

So: read every site's position, decode the ones that reach our raster, and
send a picture of each beside the SE composite of the same minute.
"""
import base64
import datetime as dt
import io
import json
import math
import sys

sys.path.insert(0, ".")

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from wxfusion import proj3059 as P, radar_nordic as N  # noqa: E402
from wxfusion.http import session  # noqa: E402
from wxfusion.scrape_maps import draw_borders, ramp_lut  # noqa: E402

s = session()
API = "https://opendata-download-radar.smhi.se/api/version/latest"


def reach_km(lat, lon, range_km=240.0):
    x, y = P.to_xy(lon, lat)
    dx = max(0.0, P.X0 - x, x - P.X1) / 1000.0
    dy = max(0.0, P.Y0 - y, y - P.Y1) / 1000.0
    return max(0.0, math.hypot(dx, dy) - range_km)


def picture(lev, title):
    rgba = ramp_lut(False)[np.clip(lev, 0, 96).astype(np.uint8)]
    im = Image.fromarray(np.ascontiguousarray(rgba, np.uint8), "RGBA")
    draw_borders(im)
    bg = Image.new("RGB", im.size, (247, 245, 239))
    bg.paste(im, (0, 0), im)
    buf = io.BytesIO()
    bg.save(buf, "PNG", optimize=True)
    print(f"@@IMG:{title}:{base64.b64encode(buf.getvalue()).decode()}")


areas = [a["key"] for a in json.loads(s.get(f"{API}.json", timeout=60).text)
         ["areas"] if a["key"] != "sweden"]
print("SMHI sites:", ", ".join(areas))
found = {}
for site in areas:
    try:
        day = json.loads(s.get(f"{API}/area/{site}/product/qcvol/"
                               f"{dt.date.today():%Y/%m/%d}", timeout=60).text)
        files = day.get("files") or []
        if not files:
            print(f"  {site:14s} no files today")
            continue
        newest = max(files, key=lambda f: f.get("valid", ""))
        url = next(f["link"] for f in newest["formats"] if f["key"] == "h5")
        body = s.get(url, timeout=120).content
        lev, (lat, lon), name = N._decode_smhi_vol(body)
        short = reach_km(lat, lon)
        echo = int((lev > 0).sum())
        print(f"  {site:14s} {name:22s} {lat:.4f}N {lon:.4f}E  "
              f"{len(body) / 1e6:.1f} MB  valid {newest['valid']}  "
              f"echo on our raster={echo} px  "
              + ("<== ON OUR MAP" if short == 0 else f"({short:.0f} km short)"))
        if short == 0:
            found[site] = (name, lat, lon)
            picture(lev, f"site_{site}")
    except Exception as e:
        print(f"  {site:14s} FAILED {type(e).__name__}: {e}")

print("\nSE_SITES entries to add:")
for site, (name, lat, lon) in found.items():
    code = "SE" + "".join(c for c in name.upper() if c.isalpha())[:3]
    print(f'    "{code}": ("{site}", "SE {name.split("(")[0]}", '
          f'{lat:.3f}, {lon:.3f}),')

got = N.fetch_smhi()
if got:
    print(f"\nSE composite for comparison, {got[0]:%H:%M}Z, "
          f"{int((got[1] > 0).sum())} px echo on our raster")
    picture(got[1], "se_composite")
