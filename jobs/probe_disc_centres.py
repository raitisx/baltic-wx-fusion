"""ilm__cmp_cap over WCS: does Estonia hand out radar VALUES?

Their GeoServer lists two coverages — ilm__cmp_cap (the composite CAPPI the
app draws) and ilm__nowcasting. A coverage is not a picture: GetCoverage
returns the grid itself, so if this works Estonia stops being a palette to
match and becomes numbers to classify, like SMHI and FMI. Worth knowing
exactly: the CRS and grid, whether there is a TIME axis, and what the values
mean.
"""
import io
import re
import sys

sys.path.insert(0, ".")

import numpy as np  # noqa: E402

from wxfusion.http import session  # noqa: E402

s = session()
OWS = "https://ilmgs.envir.ee/geoserver/ows"
COV = "ilm__cmp_cap"


def get(tag, url, show=0):
    try:
        r = s.get(url, timeout=90)
        print(f"\n--- {tag}: {r.status_code} {len(r.content):,}B  "
              f"{r.headers.get('content-type','?')[:40]}")
        print("    ", url[:150])
        if show and r.ok and "xml" in r.headers.get("content-type", ""):
            print("    ", re.sub(r"\s+", " ", r.text)[:show])
        return r
    except Exception as e:
        print(f"\n--- {tag}: FAIL {type(e).__name__}\n     {url[:150]}")
        return None


r = get("DescribeCoverage",
        f"{OWS}?service=WCS&version=2.0.1&request=DescribeCoverage"
        f"&coverageId={COV}", show=3000)

# WMS side: what does GetCapabilities say about its time dimension?
r2 = get("WMS caps (dimension)",
         f"{OWS}?service=WMS&version=1.3.0&request=GetCapabilities")
if r2 is not None and r2.ok:
    for m in re.finditer(r"(?is)<Layer[^>]*>.{0,4000}?</Layer>", r2.text):
        blob = m.group(0)
        if "cmp_cap" in blob:
            for d in re.finditer(r"(?is)<Dimension[^>]*>(.{0,400}?)</Dimension>",
                                 blob):
                print("    time dimension:", re.sub(r"\s+", " ",
                                                    d.group(0))[:400])
            break

print("\n=== GetCoverage: fetch the grid itself ===")
for fmt, ext in (("image/tiff", "tif"), ("application/gml+xml", "gml")):
    rr = get(f"GetCoverage {fmt}",
             f"{OWS}?service=WCS&version=2.0.1&request=GetCoverage"
             f"&coverageId={COV}&format={fmt}")
    if rr is None or not rr.ok or ext != "tif":
        continue
    try:
        import rasterio
        with rasterio.open(io.BytesIO(rr.content)) as ds:
            v = ds.read(1)
            print(f"    {ds.width}x{ds.height} {v.dtype} {ds.crs} "
                  f"px={abs(ds.transform.a):.0f}  nodata={ds.nodata}")
            u, c = np.unique(v, return_counts=True)
            print("    values:", [(int(a), int(b)) for a, b in
                                  list(zip(u, c))[:12]], "...", len(u),
                  "distinct")
            print("    band count:", ds.count, " tags:",
                  {k: val for k, val in list(ds.tags().items())[:8]})
    except Exception as e:
        print("    not readable:", type(e).__name__, e)

print("\n=== and the WMS picture, for comparison ===")
get("GetMap", f"{OWS}?service=WMS&version=1.1.1&request=GetMap"
               f"&layers=ilm:cmp_cap&styles=&srs=EPSG:3301"
               f"&bbox=300000,6350000,800000,6650000&width=500&height=300"
               f"&format=image/png&transparent=true")
