"""Estonia's coloured area in the COMPOSITE frames the page serves.

The stage probe showed EE's own layer is intact at every slot (42-62k px),
so if Estonia still empties between two published frames the loss is in the
merge — or the two frames the user compared are not the two slots they look
like. This walks the archive slot by slot and reports, for each, which frame
file it points at, the sweep times that went into it, and how much coloured
echo the FRAME holds over Estonia proper.
"""
import datetime as dt
import io
import json
import sys

sys.path.insert(0, ".")

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from wxfusion import config, proj3059 as P, radar_store as R  # noqa: E402

s3 = R.r2_client()
EST = (57.5, 59.7, 21.8, 28.2)          # Estonia proper


def bbox_mask(b):
    latS, latN, lonW, lonE = b
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


M = bbox_mask(EST)
arch = json.loads(s3.get_object(Bucket=config.R2_BUCKET,
                                Key=R.ARCHIVE_KEY)["Body"].read())
idx = R._load_index(s3, dt.date(2026, 8, 22))

slot = dt.datetime(2026, 8, 22, 11, 45, tzinfo=dt.timezone.utc)
end = dt.datetime(2026, 8, 22, 13, 30, tzinfo=dt.timezone.utc)
while slot <= end:
    key = slot.strftime("%Y%m%dT%H%M")
    vtag = (arch.get("quarters", {}).get(key) if slot.minute
            else arch.get("hours", {}).get(slot.strftime("%Y%m%dT%H")))
    picks = {}
    for f in idx["frames"]:
        t = dt.datetime.strptime(f["time"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc)
        gap = abs((t - slot).total_seconds())
        if gap <= R.QUARTER_FILL_MIN * 60:
            picks.setdefault(f["code"], []).append((gap, t, f))
    chosen = {c: R._pick(v, R.SLOT_TOL_MIN * 60) for c, v in picks.items()}
    who = " ".join(f"{c}@{t:%H%M}" for c, (_, t, _) in sorted(chosen.items()))
    if not vtag:
        print(f"{slot:%H:%M}Z  NO FRAME IN ARCHIVE   picks: {who}")
        slot += dt.timedelta(minutes=15)
        continue
    try:
        b = s3.get_object(Bucket=config.R2_BUCKET,
                          Key=f"{R.FRAME_PREFIX}/{vtag}.png")["Body"].read()
        a = np.array(Image.open(io.BytesIO(b)).convert("RGB"))
        spread = a.max(2).astype(int) - a.min(2).astype(int)
        col = int(((spread > 25) & M).sum())
        print(f"{slot:%H:%M}Z -> {vtag}  Estonia coloured={col:6d}  picks: {who}")
    except Exception as e:
        print(f"{slot:%H:%M}Z -> {vtag}  FRAME MISSING: {type(e).__name__}")
    slot += dt.timedelta(minutes=15)
