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


for b in BUCKETS:
    code, pres, keys = ls(b)
    print(f"{b}: HTTP {code}, top prefixes {pres[:12]}, keys {keys[:5]}")

b = BUCKETS[0]
old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)
for probe in (f"{old:%Y/%m/%d}/", f"{old:%Y}/{old:%m}/{old:%d}/",
              f"{old:%Y%m%d}/", f"{old:%Y}/"):
    code, pres, keys = ls(b, probe)
    print(f"  prefix {probe!r}: HTTP {code}, prefixes {pres[:10]}, "
          f"keys {keys[:8]}")
    if pres or keys:
        # Drill one level further to see the products.
        nxt = pres[0] if pres else None
        if nxt:
            code2, pres2, keys2 = ls(b, nxt)
            print(f"    -> {nxt!r}: prefixes {pres2[:10]}, keys {keys2[:8]}")
            if pres2:
                code3, pres3, keys3 = ls(b, pres2[0], delim="")
                print(f"       -> {pres2[0]!r}: keys {keys3[:12]}")
        break

# If a composite rr key shows up anywhere above, fetch and describe it.
found = []
for prefix in (f"{old:%Y/%m/%d}/",):
    _, pres, _ = ls(b, prefix)
    for p in pres:
        _, _, kk = ls(b, p, delim="", keys=40)
        found += [k for k in kk if re.search(r"rr|RR", k)]
if found:
    import rasterio
    key = found[len(found) // 2]
    r = s.get(f"https://{b}.s3.eu-west-1.amazonaws.com/{key}", timeout=120)
    print(f"sample {key}: HTTP {r.status_code}, {len(r.content)} bytes")
    with rasterio.open(io.BytesIO(r.content)) as ds:
        v = ds.read(1)
        print(f"  shape {v.shape}, dtype {v.dtype}, crs {ds.crs}, "
              f"min/max {v.min()}/{v.max()}, "
              f"nonzero {(v > 0).sum()}")
else:
    print("no rr-looking keys found at the 10-day-old date")
