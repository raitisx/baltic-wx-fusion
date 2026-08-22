"""Does FMI's AWS open-data bucket serve radar composites past the WFS ~7 d?

The WFS stored query returns nothing at 14 days back; FMI also mirrors radar
GeoTIFFs to public S3 (eu-west-1). Map the bucket layout, find the rr
composite for a 10-day-old timestamp, download one and print its raster
shape/dtype/scale so a backfill source can be wired. Throwaway probe.
"""
import datetime as dt
import io
import re
import sys

sys.path.insert(0, ".")

from wxfusion.http import session  # noqa: E402

s = session()
BUCKETS = ["fmi-opendata-radar-geotiff", "fmi-opendata-radar-volume-hdf5"]


def ls(bucket, prefix="", delim="/", keys=25):
    url = (f"https://{bucket}.s3.eu-west-1.amazonaws.com/?list-type=2"
           f"&max-keys={keys}&prefix={prefix}")
    if delim:
        url += f"&delimiter={delim}"
    r = s.get(url, timeout=45)
    pres = re.findall(r"<Prefix>([^<]+)</Prefix>", r.text)[1:]  # 1st = query
    keys_ = re.findall(r"<Key>([^<]+)</Key>", r.text)
    return r.status_code, pres, keys_


import rasterio  # noqa: E402  (workflow installs it; fail fast if not)

b = BUCKETS[0]
old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)
# Known layout (probe v1): {Y}/{m}/{d}/{site|finrad|finradfast}/
#   202608120000_fianj_cappi_600_dbzh_qc.tif etc., archive back to 2020.
for comp in ("finrad", "finradfast"):
    _, _, kk = ls(b, f"{old:%Y/%m/%d}/{comp}/2026", delim="", keys=60)
    prods = sorted({re.sub(r"^.*?\d{12}_", "", k) for k in kk})
    print(f"{comp}: {len(kk)} keys in first page, products {prods}")
    if kk:
        print("  first keys:", kk[:4])

# Describe one rr-looking composite (fall back to dbzh) from 10 days ago.
_, _, kk = ls(b, f"{old:%Y/%m/%d}/finrad/{old:%Y%m%d}12", delim="", keys=40)
pick = next((k for k in kk if re.search(r"_rr", k)), None) or \
    next((k for k in kk if "dbz" in k), None)
if pick:
    r = s.get(f"https://{b}.s3.eu-west-1.amazonaws.com/{pick}", timeout=120)
    print(f"sample {pick}: HTTP {r.status_code}, {len(r.content)} bytes")
    with rasterio.open(io.BytesIO(r.content)) as ds:
        v = ds.read(1)
        import numpy as np
        hi = np.iinfo(v.dtype).max if v.dtype.kind == "u" else np.inf
        wet = v[(v > 0) & (v < hi)]
        print(f"  shape {v.shape}, dtype {v.dtype}, crs {ds.crs}, "
              f"transform {ds.transform}, nodata {ds.nodata}")
        print(f"  min/max {v.min()}/{v.max()}, nonzero {(v > 0).sum()}, "
              f"wet percentiles "
              f"{np.percentile(wet, [10, 50, 90]).round(1).tolist() if wet.size else '-'}")
        print(f"  tags: {ds.tags()}")
else:
    print("no rr/dbz keys under finrad at 12Z, 10 days ago")

# How deep does finrad go back? Spot-check a few ages.
for back in (14, 30, 180, 365):
    d = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=back)
    _, _, kk = ls(b, f"{d:%Y/%m/%d}/finrad/{d:%Y%m%d}12", delim="", keys=10)
    print(f"finrad -{back}d: {len(kk)} keys at 12Z")
