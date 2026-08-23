"""What are the stored overlay bounds per feed, and do they stay put?

The LV debug panel at 22.08 09:55 draws a whole radar disc inside ~60 km of
Riga — the shape of a full-range image placed with the wrong bounds. This
prints, per feed, every stored frame's sw/ne and the implied km span, plus
the raw source image for the frames in question so the placement can be
judged against what LVGMC actually served. Throwaway probe.
"""
import base64
import datetime as dt
import io
import math
import sys

sys.path.insert(0, ".")

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from wxfusion import config, radar_store as R  # noqa: E402

s3 = R.r2_client()
DAY = dt.date(2026, 8, 22)
idx = R._load_index(s3, DAY)


def span_km(sw, ne):
    (la0, lo0), (la1, lo1) = sw, ne
    ns = (la1 - la0) * 111.32
    ew = (lo1 - lo0) * 111.32 * math.cos(math.radians((la0 + la1) / 2))
    return ew, ns


rows = {}
for f in idx.get("frames", []):
    t = dt.datetime.strptime(f["time"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=dt.timezone.utc)
    if not (dt.datetime(2026, 8, 22, 9, 0, tzinfo=dt.timezone.utc) <= t
            <= dt.datetime(2026, 8, 22, 13, 0, tzinfo=dt.timezone.utc)):
        continue
    rows.setdefault(f["code"], []).append((t, f))

for code in sorted(rows):
    print(f"--- {code} ---")
    for t, f in sorted(rows[code])[:10]:
        if f.get("grid3059"):
            print(f"  {t:%H:%M}  grid3059 (no bounds, already on our raster) "
                  f"{f.get('bytes')}B")
            continue
        sw, ne = tuple(f["sw"]), tuple(f["ne"])
        ew, ns = span_km(sw, ne)
        try:
            body = s3.get_object(Bucket=config.R2_BUCKET,
                                 Key=f["key"])["Body"].read()
            im = Image.open(io.BytesIO(body))
            wh = f"{im.size[0]}x{im.size[1]}"
            a = np.array(im.convert("RGBA"))
            painted = int((a[..., 3] > 60).sum())
        except Exception as e:
            wh, painted = f"ERR {e}", -1
        print(f"  {t:%H:%M}  sw={sw} ne={ne}  span={ew:.0f}x{ns:.0f} km  "
              f"img={wh}  painted={painted}  {f.get('bytes')}B")

# Raw source images for the LV frames around the odd panel, as served.
want = [dt.datetime(2026, 8, 22, h, m, tzinfo=dt.timezone.utc)
        for h, m in ((9, 55), (10, 45), (11, 45))]
lv = sorted(rows.get("LV", []))
for w in want:
    if not lv:
        break
    t, f = min(lv, key=lambda e: abs((e[0] - w).total_seconds()))
    body = s3.get_object(Bucket=config.R2_BUCKET, Key=f["key"])["Body"].read()
    im = Image.open(io.BytesIO(body)).convert("RGBA")
    bg = Image.new("RGB", im.size, (247, 245, 239))
    bg.paste(im, (0, 0), im)
    buf = io.BytesIO()
    bg.save(buf, "PNG", optimize=True)
    print(f"@@IMG:LVsrc_{t:%H%M}:{base64.b64encode(buf.getvalue()).decode()}")
