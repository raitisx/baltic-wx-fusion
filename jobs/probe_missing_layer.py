"""Why do whole layers vanish from some 22.08 frames? (user: 12:33 Riga has
no SE storm, 12:47/13:01 do)

Dump what the store holds per feed around 09:00-10:10 UTC 22.08, replay the
quarter matching for each slot, and try to load every chosen frame. Also
report which vtags the archive filed for those slots. Throwaway probe.
"""
import datetime as dt
import sys

sys.path.insert(0, ".")

import json  # noqa: E402

from wxfusion import config, radar_store as R  # noqa: E402

s3 = R.r2_client()
DAY = dt.date(2026, 8, 22)
idx = R._load_index(s3, DAY)
lo = dt.datetime(2026, 8, 22, 8, 50, tzinfo=dt.timezone.utc)
hi = dt.datetime(2026, 8, 22, 10, 10, tzinfo=dt.timezone.utc)

by_code = {}
for f in idx.get("frames", []):
    t = dt.datetime.strptime(f["time"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=dt.timezone.utc)
    if lo <= t <= hi:
        by_code.setdefault(f["code"], []).append((t, f["key"]))
import io  # noqa: E402

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

for code in sorted(by_code):
    parts = []
    for t, key in sorted(by_code[code]):
        try:
            body = s3.get_object(Bucket=config.R2_BUCKET,
                                 Key=key)["Body"].read()
            im = Image.open(io.BytesIO(body))
            a = np.array(im.convert("RGBA"))
            painted = int((a[..., 3] > 60).sum()) if im.mode != "L" else \
                int((np.array(im) > 0).sum())
        except Exception as e:
            painted = f"ERR {type(e).__name__}"
        parts.append(f"{t:%H:%M}={painted}")
    print(f"{code:4s}: " + " ".join(parts))
for code in ("SE", "FI", "EE", "LT", "LT2", "LV"):
    if code not in by_code:
        print(f"{code:4s}: NOTHING in window")

print("\n=== slot matching (two-ring) ===")
for hh, mm in ((9, 15), (9, 30), (9, 45), (10, 0)):
    slot = dt.datetime(2026, 8, 22, hh, mm, tzinfo=dt.timezone.utc)
    best = {}
    for f in idx["frames"]:
        t = dt.datetime.strptime(f["time"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc)
        gap = abs((t - slot).total_seconds())
        if gap > R.QUARTER_FILL_MIN * 60:
            continue
        rank = (gap > R.SLOT_TOL_MIN * 60, gap)
        cur = best.get(f["code"])
        if cur is None or rank < cur[0]:
            best[f["code"]] = (rank, t, f)
    line = []
    for code, (rank, t, f) in sorted(best.items()):
        try:
            n = len(s3.get_object(Bucket=config.R2_BUCKET,
                                  Key=f["key"])["Body"].read())
            ok = f"{n}B"
        except Exception as e:
            ok = f"LOAD FAIL {type(e).__name__}"
        line.append(f"{code}@{t:%H:%M}{'*' if rank[0] else ''}({ok})")
    print(f"{slot:%H:%M}: " + "  ".join(line))

print("\n=== archive entries ===")
arch = json.loads(s3.get_object(Bucket=config.R2_BUCKET,
                                Key=R.ARCHIVE_KEY)["Body"].read())
for k in sorted(arch.get("quarters", {})):
    if k.startswith("20260822T09"):
        print("quarters", k, "->", arch["quarters"][k])
for k in ("20260822T09", "20260822T10"):
    print("hours", k, "->", arch.get("hours", {}).get(k))
