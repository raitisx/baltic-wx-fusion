"""Estonia and Latvia: is there a per-radar source anywhere?

Sweden and Finland are separated now. Estonia's product is still a two-radar
composite (Harku + Surgavere) and Latvia's is a single radar already, so what
is left is to find whether Estonia publishes its radars individually — and,
while we are there, what Latvia actually serves, since meteolapa's LV frame
is a suspiciously small disc.

The Estonian weather service draws its radar from a tile server
(tiles.envir.ee, layer ilmateenistus-radar) and that layer is a composite, so
this looks past it: the open-data portals, the site's own API, and the
national open-data catalogues, for anything shaped like a single-site
product.
"""
import re
import sys

sys.path.insert(0, ".")

from wxfusion.http import session  # noqa: E402

s = session()


def get(tag, url, want=None):
    try:
        r = s.get(url, timeout=45)
        ct = r.headers.get("content-type", "?")[:30]
        print(f"  {tag:26s} {r.status_code} {len(r.content):>9,d}B  {ct}  {url[:95]}")
        if r.ok and want:
            hits = sorted({m for m in re.findall(want, r.text, re.I)})[:20]
            for h in hits:
                print("      ", (h if isinstance(h, str) else " ".join(h))[:120])
        return r
    except Exception as e:
        print(f"  {tag:26s} FAIL {type(e).__name__}  {url[:95]}")
        return None


RADARISH = r'["\']([^"\']*(?:radar|sadu|surgavere|harku|s%C3%BCrgavere)[^"\']{0,70})'

print("=== EE: open data portals and the service's own API ===")
get("envir opendata", "https://opendata.envir.ee/", RADARISH)
get("keskkonnaportaal", "https://avaandmed.keskkonnaportaal.ee/", RADARISH)
get("avaandmed.eesti.ee api",
    "https://avaandmed.eesti.ee/api/datasets/search?query=radar")
get("ilmateenistus wp-json", "https://www.ilmateenistus.ee/wp-json/")
get("ilmateenistus radar api",
    "https://www.ilmateenistus.ee/wp-json/ilm/v1/radar")
get("tms resource",
    "https://tiles.envir.ee/tm/tms/1.0.0/ilmateenistus-radar@LEST",
    r"<(?:BoundingBox|TileSet|Title)[^>]*>")
get("wms capabilities",
    "https://tiles.envir.ee/geoserver/wms?service=WMS&request=GetCapabilities",
    r"<Name>([^<]*radar[^<]*)</Name>")

print("\n=== LV: what LVGMC actually serves ===")
for tag, url in (
        ("videscentrs root", "https://videscentrs.lvgmc.lv/"),
        ("videscentrs radar page",
         "https://videscentrs.lvgmc.lv/karte/meteorologiska-radara-informacija"),
        ("videscentrs api", "https://videscentrs.lvgmc.lv/api/radar"),
        ("data.gov.lv search",
         "https://data.gov.lv/dati/api/3/action/package_search?q=radar"),
):
    get(tag, url, RADARISH)

print("\n=== meteolapa: how does IT get the Estonian frames? ===")
r = get("meteolapa radars page", "https://www.meteolapa.lv/radars",
        r'(?:code|title|name)"\s*:\s*"([^"]{2,30})"')
if r is not None and r.ok:
    for m in sorted({m for m in re.findall(r'"([A-Z]{2}\d?)"\s*[,:}]', r.text)}):
        print("       code token:", m)
