"""Estonia, harder: what does their radar app actually call?

The tile server carries one layer (cmp_cap) and the S3 bucket refuses to
list, but the app must ask something for the list of available frames — and
publicapi.envir.ee answers (404s with XML, so the host is real). This pulls
the radar page's own scripts and reads the endpoints out of them, enumerates
that API, tries the bucket under its other S3 spellings, and walks the tile
server from the root rather than from /tiles/ilm.
"""
import re
import sys

sys.path.insert(0, ".")

from wxfusion.http import session  # noqa: E402

s = session()
RADAR_PAGE = "https://www.ilmateenistus.ee/ilm/ilmavaatlused/radar/"


def get(tag, url, show=0, headers=None):
    try:
        r = s.get(url, timeout=45, headers=headers or {})
        ct = r.headers.get("content-type", "?")[:26]
        print(f"  {tag:34s} {r.status_code} {len(r.content):>9,d}B {ct}  {url[:92]}")
        if show and r.ok:
            print("       ", r.text[:show].replace("\n", " ")[:show])
        return r
    except Exception as e:
        print(f"  {tag:34s} FAIL {type(e).__name__}  {url[:92]}")
        return None


print("=== 1. the radar page's own scripts, read for endpoints ===")
r = get("radar page", RADAR_PAGE)
srcs = []
if r is not None and r.ok:
    srcs = [u if u.startswith("http") else "https://www.ilmateenistus.ee" + u
            for u in re.findall(r'<script[^>]+src="([^"]+)"', r.text)]
    # inline config too
    for m in re.finditer(r"(?s)<script[^>]*>(.{0,20000}?)</script>", r.text):
        blob = m.group(1)
        if re.search(r"radar|cmp_cap|tiles", blob, re.I):
            for hit in sorted({h for h in re.findall(
                    r'["\'](/[a-z0-9_\-/\.]{4,80}|https?://[^"\'\s]{8,110})["\']',
                    blob, re.I)
                    if re.search(r"radar|tile|api|json|time|ilm", h, re.I)}):
                print("     inline:", hit[:110])
print(f"   {len(srcs)} script files")
for u in srcs:
    rr = s.get(u, timeout=45)
    if not rr.ok:
        continue
    hits = sorted({h for h in re.findall(
        r'["\'](/[a-z0-9_\-/\.]{4,90}|https?://[^"\'\s]{8,120})["\']',
        rr.text, re.I)
        if re.search(r"radar|cmp_cap|tiles|timestamp|/api/|\.json", h, re.I)})
    if hits:
        print(f"   {u.rsplit('/', 1)[-1][:44]}:")
        for h in hits[:25]:
            print("       ", h[:115])

print("\n=== 2. publicapi.envir.ee — the host answers, so enumerate it ===")
for path in ("", "v1", "v1/", "swagger", "swagger/index.html", "openapi.json",
             "swagger/v1/swagger.json", "v1/radar", "v1/radars",
             "v1/radarData", "v1/precipitation", "api", "api/v1/radar"):
    get(f"publicapi /{path}", f"https://publicapi.envir.ee/{path}", show=220)

print("\n=== 3. the bucket, under its other spellings ===")
for tag, url in (
        ("path-style v1", "https://s3.pilw.io/rp-kemit-ilm04?prefix=EE-radar/"
                          "&max-keys=40"),
        ("vhost-style", "https://rp-kemit-ilm04.s3.pilw.io/?list-type=2"
                        "&prefix=EE-radar/&max-keys=40"),
        ("vhost root", "https://rp-kemit-ilm04.s3.pilw.io/"),
        ("prefix as page", "https://s3.pilw.io/rp-kemit-ilm04/EE-radar/"),
):
    get(tag, url, show=300)

print("\n=== 4. the tile server from its root ===")
for path in ("", "tiles/", "tiles/ilm/", "tiles/radar/", "tiles/ilm2/"):
    rr = get(f"ilmtiles /{path}", f"https://ilmtiles.envir.ee/{path}")
    if rr is not None and rr.ok:
        names = sorted(set(re.findall(r'<a href="([^"?][^"]*)"', rr.text)))
        print("       entries:", ", ".join(n[:30] for n in names[:40]))

print("\n=== 5. the national open-data portal's search API ===")
for url in ("https://avaandmed.eesti.ee/api/datasets?limit=5&q=radar",
            "https://avaandmed.eesti.ee/api/public/datasets?q=radar",
            "https://avaandmed.eesti.ee/api/datasets/search?query=radar"):
    get("avaandmed", url, show=300,
        headers={"Accept": "application/json"})
