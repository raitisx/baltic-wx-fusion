"""SMHI qcvol: what is inside one, and can we make a surface field from it?

Both single-radar sources are now located. FMI's is a drop-in — the same
uint8 DBZH GeoTIFF our composite decode already reads, one per site, 250 m,
250 km range. SMHI's is an ODIM polar VOLUME (qcvol, h5, 257 a day per site),
so it needs a decoder of our own: pick the lowest sweep, turn (range, azimuth)
into ground coordinates about the antenna, and warp that onto our raster.

This dumps one volume's structure — groups, elevations, quantities, the
scaling attributes and the antenna position — so that decoder can be written
against the real thing rather than against the ODIM spec.
"""
import datetime as dt
import io
import json
import sys

sys.path.insert(0, ".")

import numpy as np  # noqa: E402

from wxfusion.http import session  # noqa: E402

s = session()
API = "https://opendata-download-radar.smhi.se/api/version/latest"
SITE = "hemse"          # Gotland: the one whose range covers our Baltic

d = json.loads(s.get(f"{API}/area/{SITE}/product/qcvol.json", timeout=60).text)
link = d["products"][0]["link"] if "products" in d else None
day = json.loads(s.get(f"{API}/area/{SITE}/product/qcvol/"
                       f"{dt.date.today():%Y/%m/%d}", timeout=60).text)
files = day.get("files") or []
print(f"{SITE}: {len(files)} files today; newest =",
      files[-1].get("valid") if files else None)
url = next(f["link"] for f in files[-1]["formats"] if f["key"] == "h5")
body = s.get(url, timeout=120).content
print(f"volume is {len(body):,} bytes")

import h5py  # noqa: E402

with h5py.File(io.BytesIO(body), "r") as h:
    def walk(name, obj):
        if isinstance(obj, h5py.Dataset):
            print(f"  DATA {name:38s} {obj.shape} {obj.dtype}")
        elif name.count("/") < 2:
            print(f"  GRP  {name}")
        for k, v in obj.attrs.items():
            if name.count("/") < 3:
                v = v.decode() if isinstance(v, bytes) else v
                print(f"       {name}/{k} = {str(v)[:90]}")
    h.visititems(walk)
    for k, v in h["/what"].attrs.items():
        print("  what:", k, "=", (v.decode() if isinstance(v, bytes) else v))
    for k, v in h["/where"].attrs.items():
        print("  where:", k, "=", v)
    ds1 = h["/dataset1"]
    print("  dataset1/where:", dict(ds1["where"].attrs))
    print("  dataset1/what :", {k: (v.decode() if isinstance(v, bytes) else v)
                                for k, v in ds1["what"].attrs.items()})
    for key in sorted(ds1):
        if key.startswith("data"):
            g = ds1[key]
            q = g["what"].attrs.get("quantity")
            print(f"   {key}: quantity={q.decode() if isinstance(q, bytes) else q}"
                  f"  {dict(g['what'].attrs)}")
    arr = ds1["data1"]["data"][:]
    print("  data1 shape", arr.shape, arr.dtype, "min/max",
          int(arr.min()), int(arr.max()),
          "nonzero", int((arr > 0).sum()))
    print("  elevations:", [float(h[f"/dataset{i}"]["where"].attrs["elangle"])
                            for i in range(1, 1 + sum(1 for k in h
                                                      if k.startswith("dataset")))])
