"""Estonia per radar: the app names an S3 bucket and a "cmp_cap" tile layer.

Their radar page loads tiles from
    https://ilmtiles.envir.ee/tiles/ilm/cmp_cap/{TIME}/{z}/{x}/{-y}.png
and mentions
    https://s3.pilw.io/rp-kemit-ilm04/EE-radar/

"cmp" is composite — which implies siblings per radar — and an S3 prefix
called EE-radar is exactly where raw sweeps would live. This lists the bucket
and probes the tile server for per-radar layer names (Harku, Surgavere).
"""
import datetime as dt
import re
import sys

sys.path.insert(0, ".")

from wxfusion.http import session  # noqa: E402

s = session()
BUCKET = "https://s3.pilw.io/rp-kemit-ilm04"
TILES = "https://ilmtiles.envir.ee/tiles/ilm"


def get(tag, url, show=0):
    try:
        r = s.get(url, timeout=60)
        print(f"  {tag:30s} {r.status_code} {len(r.content):>10,d}B  "
              f"{r.headers.get('content-type','?')[:24]}  {url[:100]}")
        if show and r.ok:
            print("     ", r.text[:show].replace("\n", " "))
        return r
    except Exception as e:
        print(f"  {tag:30s} FAIL {type(e).__name__}  {url[:100]}")
        return None


print("=== the S3 bucket behind the Estonian radar app ===")
r = get("EE-radar prefixes",
        f"{BUCKET}?list-type=2&delimiter=/&prefix=EE-radar/")
if r is not None and r.ok:
    pres = re.findall(r"<Prefix>([^<]+)</Prefix>", r.text)
    keys = re.findall(r"<Key>([^<]+)</Key>", r.text)
    print("   prefixes:", ", ".join(sorted(set(pres))[:40]) or "none")
    print("   keys:", ", ".join(keys[:15]) or "none")
    for pre in sorted(set(p for p in pres if p.rstrip("/") != "EE-radar"))[:10]:
        rr = get(f"list {pre[:26]}",
                 f"{BUCKET}?list-type=2&max-keys=40&prefix={pre}")
        if rr is not None and rr.ok:
            ks = re.findall(r"<Key>([^<]+)</Key>", rr.text)
            print("      ", len(ks), "keys, e.g.", "; ".join(ks[-4:])[:220])
get("bucket root", f"{BUCKET}?list-type=2&max-keys=25&delimiter=/", show=600)

print("\n=== the tile server: is there a layer per radar? ===")
now = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=25)
stamp = now.replace(minute=now.minute - now.minute % 5, second=0,
                    microsecond=0).strftime("%Y%m%d%H%M")
for layer in ("cmp_cap", "har_cap", "sur_cap", "syr_cap", "harku_cap",
              "surgavere_cap", "cmp_dbz", "har_dbz", "sur_dbz"):
    get(layer, f"{TILES}/{layer}/{stamp}/6/37/21.png")
for probe in ("", "layers.json", "capabilities.json", "index.json",
              "cmp_cap/times.json", "cmp_cap"):
    get(f"meta '{probe}'", f"{TILES}/{probe}", show=300)
