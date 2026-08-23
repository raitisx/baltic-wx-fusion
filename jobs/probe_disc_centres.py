"""Where can we get SINGLE-RADAR products instead of national composites?

Both remaining questions need per-antenna data, not composites: equalising
radars against each other is meaningless while Ase's panel carries ten SMHI
antennas and Surgavere's carries Harku, and a range-graded correction needs
to know which antenna a pixel's distance is measured from.

So this walks the open archives looking for per-site products:

  FMI   the S3 mirror's day prefix has {site}/ folders beside finrad/ —
        list them, open one, and report grid, CRS, scaling and where its
        coverage centre lands.
  SMHI  walk the open-data radar API tree (versions -> areas -> products)
        and report every product that is not the national composite.
  EE    the Estonian weather service publishes radar pictures per radar on
        its own site; scrape the page for image URLs and probe what they are.
  LV/LT same for LVGMC and meteo.lt.

Prints findings only — nothing is stored. Throwaway probe.
"""
import datetime as dt
import io
import re
import sys

sys.path.insert(0, ".")

import numpy as np  # noqa: E402

from wxfusion.http import session  # noqa: E402
from wxfusion.radar_nordic import FMI_S3  # noqa: E402

s = session()


def show(tag, url, **kw):
    try:
        r = s.get(url, timeout=60, **kw)
        print(f"  {tag:22s} {r.status_code} {len(r.content):>9,d}B  "
              f"{r.headers.get('content-type', '?')[:40]}  {url[:110]}")
        return r
    except Exception as e:
        print(f"  {tag:22s} FAIL {type(e).__name__}: {e}  {url[:110]}")
        return None


def describe_tif(body, label):
    import rasterio
    try:
        with rasterio.open(io.BytesIO(body)) as ds:
            v = ds.read(1)
            uniq = np.unique(v)
            lon = lat = None
            try:
                from pyproj import Transformer
                inv = Transformer.from_crs(ds.crs, "EPSG:4326", always_xy=True)
                cx = ds.transform.c + ds.width / 2 * ds.transform.a
                cy = ds.transform.f + ds.height / 2 * ds.transform.e
                lon, lat = inv.transform(cx, cy)
            except Exception:
                pass
            print(f"    {label}: {ds.width}x{ds.height} {v.dtype} {ds.crs} "
                  f"px={abs(ds.transform.a):.0f}m  values {uniq[:6].tolist()}"
                  f"..{uniq[-3:].tolist()} ({len(uniq)} distinct)"
                  + (f"  grid centre {lat:.3f}N {lon:.3f}E" if lat else ""))
    except Exception as e:
        print(f"    {label}: not readable as a GeoTIFF ({type(e).__name__})")


print("=== FMI: per-site folders on the S3 mirror ===")
day = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=3)).date()
r = show("day prefixes", f"{FMI_S3}/?list-type=2&delimiter=/"
                         f"&prefix={day:%Y/%m/%d}/")
sites = re.findall(r"<Prefix>([^<]+)</Prefix>", r.text) if r else []
sites = [p for p in sites if p.count("/") == 4]
print("   folders:", ", ".join(p.rstrip("/").rsplit("/", 1)[-1] for p in sites)
      or "none")
for pre in sites:
    name = pre.rstrip("/").rsplit("/", 1)[-1]
    if name.startswith("finrad"):
        continue
    rr = s.get(f"{FMI_S3}/?list-type=2&max-keys=400&prefix={pre}", timeout=60)
    keys = re.findall(r"<Key>([^<]+)</Key>", rr.text)
    kinds = sorted({re.sub(r"\d{12}", "<stamp>", k.rsplit("/", 1)[-1])
                    for k in keys})
    print(f"   {name:16s} {len(keys):4d} keys  {kinds[:4]}")
    tif = next((k for k in keys if k.endswith(".tif")), None)
    if tif:
        b = s.get(f"{FMI_S3}/{tif}", timeout=120).content
        describe_tif(b, name)

print("\n=== SMHI: what the radar API offers besides the composite ===")
base = "https://opendata-download-radar.smhi.se/api"
r = show("versions", f"{base}.json")
if r is not None and r.ok:
    print("   ", r.text[:600])
for path in ("/version/latest.json", "/version/latest/area/sweden.json",
             "/version/latest/area/baltic.json"):
    rr = show(path, base + path)
    if rr is not None and rr.ok:
        print("   ", rr.text[:900])

print("\n=== EE: Estonian weather service radar pages ===")
for url in ("https://www.ilmateenistus.ee/ilm/ilmavaatlused/radaripildid/",
            "https://www.ilmateenistus.ee/en/weather/weather-observations/"
            "radar-images/",
            "https://publicapi.envir.ee/v1/combinedWeatherData"):
    rr = show(url.rsplit("/", 2)[-2][:22], url)
    if rr is not None and rr.ok and "html" in rr.headers.get("content-type", ""):
        imgs = sorted({m for m in re.findall(
            r'["\'](/?[^"\']*(?:radar|radaripildid|sur|har)[^"\']*\.(?:png|gif|jpg))',
            rr.text, re.I)})
        for i in imgs[:12]:
            print("     img:", i[:120])

print("\n=== LV / LT: national radar endpoints ===")
for tag, url in (("lvgmc videscentrs", "https://videscentrs.lvgmc.lv/"),
                 ("meteo.lv radars", "https://www.meteo.lv/radars/"),
                 ("meteo.lt api", "https://api.meteo.lt/v1/stations"),
                 ("meteo.lt radar", "https://www.meteo.lt/orai/radaras/")):
    rr = show(tag, url)
    if rr is not None and rr.ok and "html" in rr.headers.get("content-type", ""):
        hits = sorted({m for m in re.findall(
            r'["\']([^"\']*radar[^"\']{0,80})["\']', rr.text, re.I)})[:10]
        for h in hits:
            print("     ref:", h[:120])

print("\n=== LT: the composite URL names its product — try single sites ===")
t = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20)
t -= dt.timedelta(minutes=t.minute % 5)
stem = ("https://new.meteo.lt/meteo_jobs/radaru_informacija/"
        "Header_Radar-{p}-{ts}.png")
for prod in ("composite", "Laukuva", "laukuva", "Vilnius", "vilnius",
             "LAU", "VIL", "site-Laukuva", "Laukuva-composite"):
    u = stem.format(p=prod, ts=t.strftime("%Y%m%d%H%M"))
    try:
        r = s_head = session().head(u, allow_redirects=True, timeout=30)
        print(f"  {prod:18s} {r.status_code} "
              f"{r.headers.get('content-length', '?'):>9}B")
    except Exception as e:
        print(f"  {prod:18s} FAIL {type(e).__name__}")
