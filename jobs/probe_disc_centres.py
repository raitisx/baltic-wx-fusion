"""Round two: pin the geozone endpoints, and see what NOTAMs carry geometry.

Round one found that ais.lgs.lv serves the in-force NOTAM list without a
login, that the geozone page embeds drz.lv/api/v1/..., and that the eUARV
viewer is an ArcGIS JS app — which means its layers are ArcGIS REST services
and can be asked for GeoJSON. This chases both, and counts how many NOTAMs
actually carry something we could draw.
"""
import json
import re
import sys
from urllib.parse import urljoin

sys.path.insert(0, ".")

from wxfusion.http import session  # noqa: E402

s = session()
s.headers.update({"User-Agent": "Mozilla/5.0 (compatible; ttaero-meteo/1.0)"})


def get(url, **kw):
    try:
        return s.get(url, timeout=60, **kw)
    except Exception as e:
        print(f"    {type(e).__name__}: {e}")
        return None


print("=== drz.lv API ===")
for path in ("/api/v1/export-history/iframe-eng", "/api/v1/export-history",
             "/api/v1/export-history/list", "/api/v1/export", "/api/v1",
             "/api/v1/zones", "/api/v1/geozones", "/api/v1/ed269"):
    url = "https://drz.lv" + path
    r = get(url)
    if r is None:
        continue
    ct = r.headers.get("content-type", "?")
    print(f"  {path}\n    HTTP {r.status_code}  {ct}  {len(r.content):,} B")
    if r.ok and len(r.content) < 6000:
        body = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.text)).strip()
        print("    text:", body[:700])
        for u in set(re.findall(r'(?:href|src)="([^"]+)"', r.text)):
            print("    link:", u)

print("\n=== eUARV: find the app script, then the map services ===")
r = get("https://airspace.lv/drones/en")
scripts = re.findall(r'<script[^>]+src="([^"]+)"', r.text) if r else []
print("  scripts:", scripts)
inline = re.findall(r"<script[^>]*>([\s\S]{40,})?</script>", r.text) if r else []
for blob in inline[:4]:
    if blob and re.search(r"(rest/services|FeatureServer|MapServer|layer)",
                          blob, re.I):
        print("  inline hit:", re.sub(r"\s+", " ", blob)[:500])
svc = set()
for src in scripts:
    if "arcgis_js_api" in src:
        continue                      # the API itself, 4 MB, not our config
    full = urljoin("https://airspace.lv/drones/en", src)
    rr = get(full)
    if rr is None or not rr.ok:
        continue
    print(f"  {full}  {len(rr.content):,} B")
    for m in re.findall(r'https?://[^"\'\s,)]+/(?:rest/services|arcgis)[^"\'\s,)]*',
                        rr.text):
        svc.add(m)
    for m in re.findall(r'["\'](/[A-Za-z0-9_/.-]*rest/services[^"\']*)["\']',
                        rr.text):
        svc.add(urljoin("https://airspace.lv/", m))
print("  candidate services:", sorted(svc)[:20] or "none found in page scripts")

# The ArcGIS server usually sits on the same host under /arcgis or /server.
for guess in ("https://airspace.lv/arcgis/rest/services?f=json",
              "https://airspace.lv/server/rest/services?f=json",
              "https://gis.lgs.lv/arcgis/rest/services?f=json",
              "https://maps.lgs.lv/arcgis/rest/services?f=json"):
    r = get(guess)
    if r is None:
        continue
    print(f"  {guess}\n    HTTP {r.status_code}  "
          f"{r.headers.get('content-type','?')}  {len(r.content):,} B")
    if r.ok and "json" in r.headers.get("content-type", ""):
        try:
            j = r.json()
            print("    folders:", j.get("folders"))
            print("    services:", [x.get("name") for x in j.get("services", [])][:20])
        except Exception as e:
            print("    ", e)
for u in sorted(svc):
    r = get(u + ("&" if "?" in u else "?") + "f=json")
    if r is not None and r.ok:
        print(f"  {u}\n    ", r.text[:400])

print("\n=== what do the NOTAMs actually carry? ===")
r = get("https://ais.lgs.lv/notam/displayfile/In force for a day")
if r and r.ok:
    txt = re.sub(r"&nbsp;", " ", r.text)
    txt = re.sub(r"<[^>]+>", "\n", txt)
    txt = re.sub(r"[ \t]+", " ", txt)
    ids = re.findall(r"\b([A-Z]\d{4}/\d{2})\b", txt)
    print(f"  {len(ids)} NOTAM ids, {len(set(ids))} distinct")
    # split into records on the id
    parts = re.split(r"\n(?=[A-Z]\d{4}/\d{2}\b)", txt)
    recs = [p for p in parts if re.match(r"[A-Z]\d{4}/\d{2}\b", p.strip())]
    print(f"  {len(recs)} records parsed")
    withq = sum(1 for p in recs if re.search(r"\bQ\)", p))
    coords = re.compile(r"\d{6}[NS]\d{7}[EW]")
    withxy = sum(1 for p in recs if coords.search(p))
    poly = sum(1 for p in recs if len(coords.findall(p)) >= 3)
    rad = sum(1 for p in recs if re.search(r"RADIUS|RADIUSS|\bNM\b", p))
    print(f"  with a Q) line          {withq}")
    print(f"  with any coordinate     {withxy}")
    print(f"  with 3+ (a polygon)     {poly}")
    print(f"  mentioning a radius/NM  {rad}")
    print("\n  --- three records verbatim ---")
    for p in recs[:3]:
        print("  " + re.sub(r"\n+", "\n  ", p.strip())[:700] + "\n")
