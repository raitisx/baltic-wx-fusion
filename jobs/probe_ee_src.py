"""Estonia's source overlays AS SERVED — no classify, no cleaning, no recolor.

The question is whether Surgavere is in the ORIGINAL product for the slots
where its half of Estonia goes missing, so this touches nothing: each stored
EE overlay is decoded, laid on a light background, and emitted verbatim with
the two antennas marked (H = Harku, S = Surgavere). Painted pixels are also
counted per antenna in the image's own pixel space, by nearest site, so the
numbers come from the source rather than from anything we draw.
"""
import base64
import datetime as dt
import io
import sys

sys.path.insert(0, ".")

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from wxfusion import config, radar_store as R  # noqa: E402
from wxfusion.scrape_maps import RADAR_SITES  # noqa: E402

s3 = R.r2_client()
DAY = dt.date(2026, 8, 22)
LO = dt.datetime(2026, 8, 22, 11, 55, tzinfo=dt.timezone.utc)
HI = dt.datetime(2026, 8, 22, 13, 20, tzinfo=dt.timezone.utc)
SITES = {n: (la, lo) for n, la, lo in RADAR_SITES if n.startswith("EE")}

idx = R._load_index(s3, DAY)
frames = []
for f in idx.get("frames", []):
    if f.get("code") != "EE":
        continue
    t = dt.datetime.strptime(f["time"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=dt.timezone.utc)
    if LO <= t <= HI:
        frames.append((t, f))

for t, f in sorted(frames):
    body = s3.get_object(Bucket=config.R2_BUCKET, Key=f["key"])["Body"].read()
    im = Image.open(io.BytesIO(body)).convert("RGBA")
    W, H = im.size
    (s_lat, s_lon), (n_lat, n_lon) = tuple(f["sw"]), tuple(f["ne"])

    def px(la, lo):
        return ((lo - s_lon) / (n_lon - s_lon) * W,
                (n_lat - la) / (n_lat - s_lat) * H)

    a = np.array(im)
    lit = a[..., 3] > 60
    yy, xx = np.mgrid[0:H, 0:W]
    d = {}
    for name, (la, lo) in SITES.items():
        sx, sy = px(la, lo)
        d[name] = np.hypot(xx - sx, yy - sy)
    names = list(d)
    near = np.stack([d[n] for n in names]).argmin(0)
    counts = {n: int((lit & (near == i)).sum()) for i, n in enumerate(names)}

    bg = Image.new("RGB", im.size, (247, 245, 239))
    bg.paste(im, (0, 0), im)
    dr = ImageDraw.Draw(bg)
    for name, (la, lo) in SITES.items():
        x, y = px(la, lo)
        r = 6
        dr.ellipse([x - r, y - r, x + r, y + r], outline=(200, 0, 0), width=3)
        dr.text((x + 9, y - 6), name.split()[1][0], fill=(160, 0, 0))
    if max(im.size) > 900:
        k = 900 / max(im.size)
        bg = bg.resize((int(W * k), int(H * k)), Image.LANCZOS)
    buf = io.BytesIO()
    bg.save(buf, "PNG", optimize=True)
    print(f"EE {t:%H:%M}Z  img={W}x{H}  lit={int(lit.sum())}  "
          + "  ".join(f"{n.split()[1]}={c}" for n, c in counts.items())
          + f"  {f.get('bytes')}B")
    print(f"@@IMG:EEsrc_{t:%H%M}:{base64.b64encode(buf.getvalue()).decode()}")
