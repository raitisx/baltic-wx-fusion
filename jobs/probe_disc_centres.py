"""Can we draw Latvian NOTAMs and drone geozones on the map?

Two questions, both about whether a machine-readable feed exists at all:
  - UAS geographical zones: EU 2019/947 Art 15 makes states publish these in
    a common digital format, and EASA points at EUROCAE ED-269 (JSON). LGS
    has a viewer that refreshes every 5 min, so something behind it is live.
  - NOTAM: normally behind a briefing login (ibs.lgs.lv), but ais.lgs.lv
    appears to serve plain "in force" listings without one.

This just knocks on the doors and reports what comes back.
"""
import json
import re
import sys

sys.path.insert(0, ".")

from wxfusion.http import session  # noqa: E402

s = session()
s.headers.update({"User-Agent": "Mozilla/5.0 (compatible; ttaero-meteo/1.0)"})

PAGES = [
    ("AIS root", "https://ais.lgs.lv/"),
    ("UAS geozones page", "https://ais.lgs.lv/page/UAS_geozones"),
    ("NOTAM in force for a day",
     "https://ais.lgs.lv/notam/displayfile/In force for a day"),
    ("NOTAM in force", "https://ais.lgs.lv/notam/displayfile/In force"),
    ("eUARV viewer", "https://airspace.lv/drones/en"),
    ("CAA geozones", "https://droni.caa.gov.lv/uas-geografiskas-zonas/"),
]

URL_RE = re.compile(r"""["'(]((?:https?:)?/[^"'()\s]{4,200})["')]""")
found = set()

for name, url in PAGES:
    print(f"\n=== {name}\n    {url}")
    try:
        r = s.get(url, timeout=60)
    except Exception as e:
        print(f"    {type(e).__name__}: {e}")
        continue
    ct = r.headers.get("content-type", "?")
    print(f"    HTTP {r.status_code}  {ct}  {len(r.content):,} bytes")
    if not r.ok:
        print("   ", r.text[:200].replace("\n", " "))
        continue
    txt = r.text
    if "json" in ct:
        try:
            j = json.loads(txt)
            print("    JSON keys:", list(j)[:15] if isinstance(j, dict)
                  else f"list of {len(j)}")
        except Exception as e:
            print("    not parseable:", e)
        continue
    if "html" not in ct and "xml" not in ct:
        print("    (binary/other, first bytes)", r.content[:60])
        continue
    # Every link and every URL-shaped string in the scripts.
    links = re.findall(r'(?:href|src|action)="([^"]+)"', txt)
    interesting = [u for u in links
                   if re.search(r"\.(json|geojson|kml|kmz|zip|xml|csv|txt)"
                                r"($|\?)", u, re.I)
                   or re.search(r"(geozone|uas|notam|download|api|export)",
                                u, re.I)]
    for u in list(dict.fromkeys(interesting))[:25]:
        print("    link:", u)
        found.add((url, u))
    api = [u for u in dict.fromkeys(URL_RE.findall(txt))
           if re.search(r"(api|geozone|zones|notam|json|wfs|wms|arcgis|"
                        r"features|layer)", u, re.I)]
    for u in api[:25]:
        print("    script url:", u)
        found.add((url, u))
    # A page with no markup worth reading: show its text, it may BE the data.
    body = re.sub(r"<[^>]+>", " ", txt)
    body = re.sub(r"\s+", " ", body).strip()
    if len(body) < 4000 and body:
        print("    text:", body[:600])
    elif "NOTAM" in txt.upper():
        m = re.search(r"([A-Z]\d{4}/\d{2}[\s\S]{0,400})", body)
        if m:
            print("    looks like NOTAM text:", m.group(1)[:400])

print("\n\n=== following the candidates that look like data ===")
from urllib.parse import urljoin  # noqa: E402

for base, u in sorted(found):
    full = urljoin(base, u)
    if not re.search(r"\.(json|geojson|kml|kmz|zip|xml)($|\?)|api|export|"
                     r"features", full, re.I):
        continue
    try:
        r = s.get(full, timeout=90)
    except Exception as e:
        print(f"  {full}\n    {type(e).__name__}: {e}")
        continue
    ct = r.headers.get("content-type", "?")
    print(f"  {full}\n    HTTP {r.status_code}  {ct}  {len(r.content):,} bytes")
    if r.ok and ("json" in ct or full.lower().endswith((".json", ".geojson"))):
        try:
            j = json.loads(r.text)
        except Exception as e:
            print("    not parseable:", e)
            continue
        if isinstance(j, dict):
            print("    keys:", list(j)[:15])
            # ED-269 shape: {"features":[{"geometry":..., "applicability":...}]}
            feats = j.get("features") or j.get("geozone") or []
            if isinstance(feats, list) and feats:
                print(f"    {len(feats)} features; first:")
                print("   ", json.dumps(feats[0], ensure_ascii=False)[:900])
        else:
            print(f"    list of {len(j)}; first:",
                  json.dumps(j[0], ensure_ascii=False)[:600] if j else "")
