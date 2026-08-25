"""Estonia, closer in: read the radar app's inline config verbatim.

The tiles URL and the S3 bucket both came from the radar page's own inline
JavaScript, so that config is where the app's data endpoints live — how it
lists available frames, what layers it can switch between, and whether any
of them is a single radar. This dumps the config around those anchors, lists
the WordPress REST routes (the site's own API surface), and knocks on the
WSO2 gateway's developer portal, which is where an APIM host publishes what
it actually serves.
"""
import json
import re
import sys

sys.path.insert(0, ".")

from wxfusion.http import session  # noqa: E402

s = session()
PAGE = "https://www.ilmateenistus.ee/ilm/ilmavaatlused/radar/"


def get(tag, url, show=0, **kw):
    try:
        r = s.get(url, timeout=45, **kw)
        print(f"  {tag:32s} {r.status_code} {len(r.content):>9,d}B  "
              f"{r.headers.get('content-type','?')[:24]}  {url[:88]}")
        if show and r.ok:
            print("       ", r.text[:show].replace("\n", " "))
        return r
    except Exception as e:
        print(f"  {tag:32s} FAIL {type(e).__name__}  {url[:88]}")
        return None


print("=== 1. the inline config, verbatim around each anchor ===")
r = get("radar page", PAGE)
if r is not None and r.ok:
    txt = r.text
    for anchor in ("cmp_cap", "EE-radar", "pilw.io", "ilmtiles",
                   "radarSettings", "layers", "timestamps"):
        for m in list(re.finditer(re.escape(anchor), txt))[:2]:
            a, b = max(0, m.start() - 700), min(len(txt), m.end() + 700)
            blob = re.sub(r"\s+", " ", txt[a:b])
            print(f"\n  --- {anchor} @ {m.start()} ---")
            print("   ", blob[:1400])

print("\n\n=== 2. the site's own REST routes ===")
r = get("wp-json index", "https://www.ilmateenistus.ee/wp-json/")
if r is not None and r.ok:
    try:
        routes = json.loads(r.text).get("routes", {})
    except Exception:
        routes = {}
    print(f"   {len(routes)} routes; namespaces:",
          ", ".join(sorted({k.strip('/').split('/')[0] for k in routes})[:30]))
    for k in sorted(routes):
        if re.search(r"radar|ilm|kaart|map|obs|layer|tile", k, re.I):
            print("    route:", k[:110])

print("\n=== 3. the APIM gateway's portals ===")
for path in ("devportal", "publisher", "store", "services", "api-docs",
             "apis", "t/carbon.super/apis"):
    get(f"/{path}", f"https://publicapi.envir.ee/{path}", show=180)
for host in ("https://api.envir.ee/", "https://apim.envir.ee/",
             "https://avaandmed.envir.ee/", "https://opendata.keskkonnaportaal.ee/"):
    get(host, host, show=180)
