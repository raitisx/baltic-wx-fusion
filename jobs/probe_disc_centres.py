"""Single-radar sources, round two: drill the two that exist and find the rest.

Round one found them:
  SMHI  /api/version/latest.json lists AREAS PER SITE (angelholm, atvidaberg,
        balsta, hemse, hudiksvall, karlskrona, ...) beside "sweden" — so the
        national composite is not all they serve.
  FMI   the S3 mirror carries {site}/ folders (fikor, fivih, fipet, ...) of
        cappi_600_dbzh_qc, uint8 EPSG:3067 at 250 m on a 500 km box, and the
        grid centre IS the antenna (fikor lands on Korppoo to 400 m).

This resolves both into a usable list: every site, its products, its formats,
where it sits, and whether its range reaches our canvas. It also chases the
two still missing — Estonia (ilmateenistus draws a composite TMS layer, so
ask the TMS capabilities what other layers exist) and Latvia (LVGMC's map
page, for whatever it loads the radar from).
"""
import datetime as dt
import io
import json
import math
import re
import sys

sys.path.insert(0, ".")

import numpy as np  # noqa: E402

from wxfusion import proj3059 as P  # noqa: E402
from wxfusion.http import session  # noqa: E402
from wxfusion.radar_nordic import FMI_S3  # noqa: E402

s = session()
SMHI_API = "https://opendata-download-radar.smhi.se/api/version/latest"


def reach_km(lat, lon, range_km=250.0):
    """0 if the antenna's range covers part of our canvas, else how far short."""
    x, y = P.to_xy(lon, lat)
    dx = max(0.0, P.X0 - x, x - P.X1) / 1000.0
    dy = max(0.0, P.Y0 - y, y - P.Y1) / 1000.0
    return max(0.0, math.hypot(dx, dy) - range_km)


def note(lat, lon):
    short = reach_km(lat, lon)
    return "  <== ON OUR MAP" if short == 0 else f"  ({short:.0f} km short)"


print("=== SMHI single-site areas ===")
areas = json.loads(s.get(f"{SMHI_API}.json", timeout=60).text)["areas"]
print(f"{len(areas)} areas:", ", ".join(a["key"] for a in areas))
for a in areas:
    if a["key"] == "sweden":
        continue
    try:
        d = json.loads(s.get(a["link"], timeout=60).text)
        prods = [p["key"] for p in d.get("products", [])]
        print(f"  {a['key']:14s} products={prods}")
        if not prods:
            continue
        link = d["products"][0]["link"]
        day = json.loads(s.get(link.replace(".json", "")
                               + f"/{dt.date.today():%Y/%m/%d}", timeout=60).text)
        files = day.get("files") or []
        fmts = sorted({f["key"] for c in files for f in c.get("formats", [])})
        print(f"                 {len(files)} files today, formats {fmts}")
        tif = next((f["link"] for c in sorted(files,
                                              key=lambda f: f.get("valid", ""),
                                              reverse=True)
                    for f in c.get("formats", []) if f["key"] == "tif"), None)
        if tif:
            import rasterio
            from pyproj import Transformer
            with rasterio.open(io.BytesIO(s.get(tif, timeout=90).content)) as ds:
                v = ds.read(1)
                inv = Transformer.from_crs(ds.crs, "EPSG:4326", always_xy=True)
                cx = ds.transform.c + ds.width / 2 * ds.transform.a
                cy = ds.transform.f + ds.height / 2 * ds.transform.e
                lon, lat = inv.transform(cx, cy)
                cov = int(((v > 0) & (v < 255)).sum())
                print(f"                 {ds.width}x{ds.height} {v.dtype} "
                      f"{ds.crs} px={abs(ds.transform.a):.0f}m  centre "
                      f"{lat:.3f}N {lon:.3f}E  echo={cov} px" + note(lat, lon))
    except Exception as e:
        print(f"  {a['key']:14s} FAILED {type(e).__name__}: {e}")

print("\n=== FMI single-site folders ===")
day = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=3)).date()
r = s.get(f"{FMI_S3}/?list-type=2&delimiter=/&prefix={day:%Y/%m/%d}/", timeout=60)
sites = [p.rstrip("/").rsplit("/", 1)[-1]
         for p in re.findall(r"<Prefix>([^<]+)</Prefix>", r.text)
         if p.count("/") == 4]
print(f"{len(sites)} folders:", ", ".join(sites))
import rasterio  # noqa: E402
from pyproj import Transformer  # noqa: E402
for site in sites:
    if site.startswith("finrad"):
        continue
    rr = s.get(f"{FMI_S3}/?list-type=2&max-keys=1000"
               f"&prefix={day:%Y/%m/%d}/{site}/", timeout=60)
    keys = [k for k in re.findall(r"<Key>([^<]+)</Key>", rr.text)
            if "cappi_600_dbzh" in k]
    if not keys:
        print(f"  {site:8s} no cappi product")
        continue
    with rasterio.open(io.BytesIO(s.get(f"{FMI_S3}/{keys[-1]}",
                                        timeout=120).content)) as ds:
        v = ds.read(1)
        inv = Transformer.from_crs(ds.crs, "EPSG:4326", always_xy=True)
        cx = ds.transform.c + ds.width / 2 * ds.transform.a
        cy = ds.transform.f + ds.height / 2 * ds.transform.e
        lon, lat = inv.transform(cx, cy)
        span = ds.width * abs(ds.transform.a) / 2000.0
        print(f"  {site:8s} {len(keys):4d} sweeps  {ds.width}x{ds.height} "
              f"@{abs(ds.transform.a):.0f}m (r={span:.0f} km)  "
              f"{lat:.3f}N {lon:.3f}E  echo={int(((v > 0) & (v < 255)).sum())}"
              + note(lat, lon))

print("\n=== EE: what layers does the envir.ee tile server carry? ===")
for url in ("https://tiles.envir.ee/tm/tms/1.0.0/",
            "https://tiles.envir.ee/tm/tms/1.0.0",
            "https://tiles.envir.ee/tm/wmts/1.0.0/WMTSCapabilities.xml"):
    try:
        rr = s.get(url, timeout=60)
        print(f"  {rr.status_code} {len(rr.content):>8,d}B  {url}")
        if rr.ok:
            names = sorted(set(re.findall(r'href="([^"]+)"', rr.text)
                               + re.findall(r"<ows:Identifier>([^<]+)</", rr.text)
                               + re.findall(r'title="([^"]+)"', rr.text)))
            hits = [n for n in names if re.search(r"radar|sadu|ilm", n, re.I)]
            print("     layers:", ", ".join(hits[:40]) or f"{len(names)} names, none matched")
    except Exception as e:
        print(f"  FAIL {type(e).__name__} {url}")

print("\n=== LV: what does the LVGMC radar map load? ===")
for url in ("https://videscentrs.lvgmc.lv/karte/meteorologiska-radara-informacija",
            "https://videscentrs.lvgmc.lv/data/radars/radar_latest.json"):
    try:
        rr = s.get(url, timeout=60)
        print(f"  {rr.status_code} {len(rr.content):>8,d}B  {url}")
        if rr.ok and "html" in rr.headers.get("content-type", ""):
            hits = sorted({m for m in re.findall(
                r'["\']([^"\']*(?:radar|radar_)[^"\']{0,90})["\']', rr.text, re.I)})
            for h in hits[:25]:
                print("     ref:", h[:130])
    except Exception as e:
        print(f"  FAIL {type(e).__name__} {url}")
