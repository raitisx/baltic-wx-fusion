"""Are the PERMANENT UAS geozones also published in the AIP?

The ED-269 file carries permanent and temporary zones together. If the
permanent ones are the AIP's own restricted/prohibited/danger areas, then
they have published names, limits and an AIRAC cycle behind them, and the
ED-269 record is a redistribution rather than a separate truth. Walk the
eAIP the user linked: find ENR 5.1 (prohibited, restricted, danger) and
anything naming UAS, and list what those sections actually hold.
"""
import re
import sys
from urllib.parse import urljoin

sys.path.insert(0, ".")

from wxfusion.http import session  # noqa: E402

s = session()
s.headers.update({"User-Agent": "Mozilla/5.0 (compatible; ttaero-meteo/1.0)"})
ROOT = ("https://ais.lgs.lv/eAIPfiles/2026_005_09-JUL-2026/data/"
        "2026-07-09/html/index.html")


def get(u):
    try:
        return s.get(u, timeout=60)
    except Exception as e:
        print(f"    {type(e).__name__}: {e}")
        return None


def text_of(html):
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"[ \t\xa0]+", " ", t)


print("=== eAIP index ===")
r = get(ROOT)
print(f"  HTTP {r.status_code if r else '-'}  {len(r.content) if r else 0:,} B")
if not r or not r.ok:
    raise SystemExit
# The index is usually a frameset; follow the menu/toc frames too.
frames = re.findall(r'(?:src|href)="([^"]+\.html?)"', r.text)
pages = {urljoin(ROOT, f) for f in frames}
print(f"  {len(pages)} linked pages in the index")

# Collect the whole link graph one level deeper, looking for ENR 5 and UAS.
seen, hits = set(), {}
todo = list(pages)[:12]
while todo:
    u = todo.pop(0)
    if u in seen:
        continue
    seen.add(u)
    rr = get(u)
    if rr is None or not rr.ok or "html" not in rr.headers.get("content-type", ""):
        continue
    for href in re.findall(r'href="([^"]+\.html?)"', rr.text):
        full = urljoin(u, href)
        name = full.rsplit("/", 1)[-1]
        if re.search(r"ENR-5\.[15]|UAS|RMZ|GEN-", name, re.I):
            hits[name] = full
        if full not in seen and len(seen) < 14:
            todo.append(full)

print("\n=== sections that could hold the zones ===")
for name, full in sorted(hits.items()):
    print("  ", name)

want = {n: u for n, u in hits.items() if re.search(r"ENR-5\.1|UAS", n, re.I)}
if not want:
    # fall back to the conventional eAIP file naming
    for guess in ("EV-ENR-5.1-en-GB.html", "EV-ENR-5.5-en-GB.html",
                  "EV-ENR-1.1-en-GB.html"):
        want[guess] = urljoin(ROOT, "eAIP/" + guess)

for name, full in sorted(want.items()):
    print(f"\n=== {name}\n    {full}")
    rr = get(full)
    if rr is None or not rr.ok:
        print(f"    HTTP {rr.status_code if rr else '-'}")
        continue
    body = text_of(rr.text)
    print(f"    HTTP {rr.status_code}  {len(rr.content):,} B")
    ids = re.findall(r"\bEV\s?[PRD]\s?-?\s?\d{1,3}\b", body)
    print(f"    area designators found: {len(ids)}  {sorted(set(ids))[:20]}")
    if re.search(r"UAS|unmanned|bezpilota", body, re.I):
        for m in re.finditer(r"(.{0,180}(?:UAS|unmanned).{0,220})", body, re.I):
            print("    ...", re.sub(r"\s+", " ", m.group(1)).strip()[:380])
            break
    print("    first 700 chars:", re.sub(r"\s+", " ", body).strip()[:700])
