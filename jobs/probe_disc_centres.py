"""Estonia has a public GeoServer — ask it what layers it has.

The radar app's inline config names three of them:
    https://ilmgs.envir.ee/geoserver/ilm/wms   ilm:cmp_cap, ilm:nowcasting,
                                               ilm:uus_radar, ilm:ilm_alus_radar
    https://gslightning.envir.ee/geoserver/lightning/wms
    https://gsavalik.envir.ee/geoserver/baasandmed/wms
plus an S3 PNG layer id "EE-CMP-CAP" under .../EE-radar/, which reads like a
naming scheme with siblings.

GetCapabilities lists everything a GeoServer serves, so this asks all three —
WMS and WCS — for the full layer list, and reports anything whose name or
abstract mentions a radar or a site. WCS matters as much as WMS: a coverage
gives values, not a styled picture.
"""
import re
import sys

sys.path.insert(0, ".")

from wxfusion.http import session  # noqa: E402

s = session()
GS = {"ilm": "https://ilmgs.envir.ee/geoserver",
      "lightning": "https://gslightning.envir.ee/geoserver",
      "avalik": "https://gsavalik.envir.ee/geoserver"}
RADARISH = re.compile(r"radar|cap|dbz|sur|har|pikne|nowcast|opera|vol", re.I)


def caps(tag, url):
    try:
        r = s.get(url, timeout=90)
        print(f"\n--- {tag}: {r.status_code} {len(r.content):,}B  {url[:96]}")
        return r if r.ok else None
    except Exception as e:
        print(f"\n--- {tag}: FAIL {type(e).__name__}  {url[:96]}")
        return None


for name, base in GS.items():
    for svc, ver in (("WMS", "1.3.0"), ("WCS", "2.0.1"), ("WFS", "2.0.0")):
        r = caps(f"{name} {svc}",
                 f"{base}/ows?service={svc}&version={ver}"
                 f"&request=GetCapabilities")
        if r is None:
            continue
        if svc == "WMS":
            layers = re.findall(r"<Name>([^<]+)</Name>", r.text)
        elif svc == "WCS":
            layers = re.findall(r"<wcs:CoverageId>([^<]+)</wcs:CoverageId>",
                                r.text) or re.findall(
                r"<CoverageId>([^<]+)</CoverageId>", r.text)
        else:
            layers = re.findall(r"<Name>([^<]+)</Name>", r.text)
        layers = sorted(set(layers))
        print(f"    {len(layers)} names")
        hits = [n for n in layers if RADARISH.search(n)]
        print("    radar-ish:", ", ".join(hits[:60]) or "none")
        if name == "ilm" and svc == "WMS":
            print("    ALL:", ", ".join(layers[:200]))
        # titles/abstracts that mention a site by name
        for m in re.finditer(r"(?is)<(?:Title|Abstract)>([^<]{4,160})</", r.text):
            t = m.group(1)
            if re.search(r"S[uü]rgavere|Harku|radar", t, re.I):
                print("      says:", t.strip()[:150])

print("\n=== the S3 layer-id scheme: EE-CMP-CAP has siblings? ===")
import datetime as dt  # noqa: E402
now = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20)
now = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0)
base = "https://s3.pilw.io/rp-kemit-ilm04/EE-radar"
for lid in ("EE-CMP-CAP", "EE-HAR-CAP", "EE-SUR-CAP", "EE-SYR-CAP"):
    for pat in ("{b}/{l}/{t:%Y%m%d%H%M}.png", "{b}/{l}-{t:%Y%m%d%H%M}.png",
                "{b}/{l}_{t:%Y%m%d%H%M}.png",
                "{b}/{t:%Y/%m/%d}/{l}-{t:%Y%m%d%H%M}.png"):
        u = pat.format(b=base, l=lid, t=now)
        try:
            r = s.head(u, timeout=30, allow_redirects=True)
            if r.status_code != 403 or lid == "EE-CMP-CAP":
                print(f"  {r.status_code} {u[:110]}")
        except Exception as e:
            print(f"  FAIL {type(e).__name__} {u[:110]}")
