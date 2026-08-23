"""Estonia's own radar volumes: where does KAIA actually serve them?

The open-data catalogue lists "Meteoroloogiliste radarite andmestik" with a
"Radari toorandmed (VOL)" resource — raw polar volumes, which is exactly the
per-radar source Estonia was missing, and the same ODIM shape the SMHI
decoder already reads. This walks the dataset page for the download or
service URL behind it, then tries to list and open one volume.
"""
import re
import sys

sys.path.insert(0, ".")

from wxfusion.http import session  # noqa: E402

s = session()
PAGE = ("https://keskkonnaportaal.ee/et/avaandmed/"
        "meteoroloogiliste-radarite-andmestik")


def get(tag, url, pat=None, limit=25):
    try:
        r = s.get(url, timeout=60)
        print(f"  {tag:24s} {r.status_code} {len(r.content):>9,d}B  "
              f"{r.headers.get('content-type', '?')[:28]}  {url[:95]}")
        if r.ok and pat:
            for h in sorted({m if isinstance(m, str) else " ".join(m)
                             for m in re.findall(pat, r.text, re.I)})[:limit]:
                print("      ", h[:140])
        return r
    except Exception as e:
        print(f"  {tag:24s} FAIL {type(e).__name__}  {url[:95]}")
        return None


print("=== the dataset page ===")
r = get("radar dataset", PAGE, r'href="([^"]{6,160})"')

print("\n=== anything that looks like a file service ===")
if r is not None and r.ok:
    urls = {u for u in re.findall(r'https?://[^"\'\s<>]{10,150}', r.text)
            if re.search(r"radar|vol|kaia|opendata|avaandmed|s3|ftp|thredds",
                         u, re.I)}
    for u in sorted(urls)[:30]:
        print("      candidate:", u[:140])

print("\n=== likely hosts, probed directly ===")
for tag, url in (
        ("kaia", "https://kaia.envir.ee/"),
        ("kaia opendata", "https://kaia.envir.ee/opendata/"),
        ("kaia radar", "https://kaia.envir.ee/opendata/radar/"),
        ("opendata.envir list", "https://opendata.envir.ee/?list-type=2"),
        ("envir s3", "https://s3.envir.ee/"),
        ("ilmateenistus opendata", "https://opendata.ilmateenistus.ee/"),
        ("ftp-ish", "https://www.ilmateenistus.ee/wp-json/wp/v2/pages?search=radar"),
):
    get(tag, url, r'(?:href|Key|title)[">=]{1,3}([^"<,]{4,120})', limit=15)
