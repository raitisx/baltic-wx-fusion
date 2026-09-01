"""Does the ED-269 geozone file actually download, and what is in it?

The listing page is public and names the current file, but the API root
answers 401 — so the question is whether the file itself is open, and
whether it carries what a map layer needs: geometry, vertical limits, and
an applicability window that says when a zone is ACTIVE.
"""
import json
import re
import sys

sys.path.insert(0, ".")

from wxfusion.http import session  # noqa: E402

s = session()
s.headers.update({"User-Agent": "Mozilla/5.0 (compatible; ttaero-meteo/1.0)"})

idx = s.get("https://drz.lv/api/v1/export-history/iframe-eng", timeout=60)
names = re.findall(r"(UASZoneVersion_[A-Za-z0-9_]+\.json)", idx.text)
print("listing names the file(s):", names)

base = "https://drz.lv/api/v1/export-history/UASZoneVersion"
tries = [base]
tries += [f"{base}/{n}" for n in names]
tries += [f"https://drz.lv/api/v1/export-history/{n}" for n in names]

doc = None
for u in tries:
    try:
        r = s.get(u, timeout=120)
    except Exception as e:
        print(f"  {u}\n    {type(e).__name__}: {e}")
        continue
    ct = r.headers.get("content-type", "?")
    print(f"  {u}\n    HTTP {r.status_code}  {ct}  {len(r.content):,} bytes")
    if r.ok and len(r.content) > 200:
        try:
            doc = r.json()
            print("    ^ parsed as JSON")
            break
        except Exception:
            body = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.text)).strip()
            print("    not JSON:", body[:300])

if doc is None:
    raise SystemExit("\nno JSON retrieved")

print("\n=== the document ===")
print("top-level keys:", list(doc) if isinstance(doc, dict) else type(doc))
feats = doc.get("features") if isinstance(doc, dict) else None
if not isinstance(feats, list):
    print(json.dumps(doc, ensure_ascii=False)[:1500])
    raise SystemExit
print(f"{len(feats)} zones")
kinds, restr, perm = {}, {}, {"permanent": 0, "timed": 0}
for f in feats:
    kinds[f.get("type")] = kinds.get(f.get("type"), 0) + 1
    restr[f.get("restriction")] = restr.get(f.get("restriction"), 0) + 1
    ap = f.get("applicability") or []
    if any((a or {}).get("permanent") == "YES" for a in ap):
        perm["permanent"] += 1
    else:
        perm["timed"] += 1
print("type:       ", kinds)
print("restriction:", restr)
print("timing:     ", perm)

geo = {}
for f in feats:
    for g in f.get("geometry") or []:
        hp = g.get("horizontalProjection") or {}
        geo[hp.get("type")] = geo.get(hp.get("type"), 0) + 1
print("geometry:   ", geo)

print("\n--- one zone verbatim ---")
print(json.dumps(feats[0], ensure_ascii=False, indent=1)[:1800])
timed = next((f for f in feats
              if not any((a or {}).get("permanent") == "YES"
                         for a in (f.get("applicability") or []))), None)
if timed:
    print("\n--- one TIMED zone (the ones worth showing as active/not) ---")
    print(json.dumps(timed, ensure_ascii=False, indent=1)[:1800])
