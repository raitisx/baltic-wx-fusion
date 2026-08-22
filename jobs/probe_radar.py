"""One-off: which radar feeds (LV/EE/LT/LT2) have stored sources per hour on a
given day, to see if the Latvian (LVGMC) feed was missing. Reads the R2 source
index directly with boto3 so it needs no project deps. Throwaway."""
import collections
import json
import os

import boto3

DAY = "2026/08/20"
BUCKET = os.environ["R2_BUCKET"]
s3 = boto3.client("s3",
    endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
    aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"])

key = f"maps/radar_src/{DAY}/index.json"
try:
    idx = json.loads(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())
except Exception as e:
    print(f"no index at {key}: {type(e).__name__} {e}")
    idx = {"frames": []}

frames = idx.get("frames", [])
print(f"{DAY}: {len(frames)} source overlays in the index")

by_hour = collections.defaultdict(set)
codes_total = collections.Counter()
for f in frames:
    t = f.get("time") or f.get("t") or ""
    code = f.get("code") or "?"
    by_hour[str(t)[:13]].add(code)
    codes_total[code] += 1

print("totals per code:", dict(codes_total))
print("\nper hour:")
for h in sorted(by_hour):
    cs = by_hour[h]
    print(f"  {h}: {sorted(cs)}  {'*** no LV ***' if 'LV' not in cs else ''}")

hours_no_lv = [h for h, cs in by_hour.items() if "LV" not in cs]
print(f"\nhours with a source but no LV: {len(hours_no_lv)} of {len(by_hour)}")
if frames:
    print("sample frame:", json.dumps(frames[0])[:300])
print("DONE")
