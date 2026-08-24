"""Estonia's raw volumes: the catalogue names them, so where are the files?

The dataset page (keskkonnaportaal.ee/et/avaandmed/meteoroloogiliste-
radarite-andmestik) lists "Radari toorandmed (VOL)" but links only to a
description page and to two document-list views on their DHS portal, and the
obvious hosts (kaia.envir.ee, opendata.envir.ee, s3.envir.ee) do not resolve.
So this reads the description page and both DHS views for the actual service:
a download link, an FTP or S3 host, or the sentence that says the data is
handed out on request.
"""
import re
import sys

sys.path.insert(0, ".")

from wxfusion.http import session  # noqa: E402

s = session()
VIEWS = ("b92201f4-8b48-4d3a-b410-30c8ce4016d5",
         "c2ea43b7-09c4-4afb-b955-aa5862e46c0f")


def strip_tags(html):
    txt = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    txt = re.sub(r"(?s)<[^>]+>", " ", txt)
    return re.sub(r"\s+", " ", txt).strip()


def get(tag, url):
    try:
        r = s.get(url, timeout=60)
        print(f"\n--- {tag}: {r.status_code} {len(r.content):,}B  {url[:110]}")
        return r
    except Exception as e:
        print(f"\n--- {tag}: FAIL {type(e).__name__}  {url[:110]}")
        return None


r = get("radar data description",
        "https://keskkonnaportaal.ee/et/avaandmed/radariandmestik/"
        "radariandmete-kirjeldus")
if r is not None and r.ok:
    txt = strip_tags(r.text)
    # the paragraphs that mention volumes, formats or how to get them
    for m in re.finditer(r"[^.]{0,240}(?:VOL|ODIM|HDF5|h5|toorandm|allalaadi"
                         r"|FTP|API|teenus|litsents|päring)[^.]{0,240}\.",
                         txt, re.I):
        print("   ", m.group(0).strip()[:300])
    for u in sorted({u for u in re.findall(r'https?://[^"\'\s<>]{8,140}',
                                           r.text)
                     if re.search(r"radar|vol|data|andme|ftp|s3|api", u, re.I)}):
        print("    url:", u[:130])

for v in VIEWS:
    r = get(f"DHS view {v[:8]}",
            f"https://avaandmed.keskkonnaportaal.ee/dhs/Active/"
            f"documentList.aspx?ViewId={v}")
    if r is None or not r.ok:
        continue
    txt = strip_tags(r.text)
    print("    text:", txt[:400])
    for u in sorted({u for u in re.findall(r'(?:href|src)="([^"]{6,160})"',
                                           r.text)
                     if re.search(r"radar|vol|download|fail|doc", u, re.I)})[:20]:
        print("    link:", u[:130])

# The service's own radar page may name the file host in its map config.
r = get("ilmateenistus radar app",
        "https://www.ilmateenistus.ee/ilm/ilmavaatlused/radar/")
if r is not None and r.ok:
    for u in sorted({u for u in re.findall(r'https?[:\\/]{2,4}[^"\'\s<>]{8,140}',
                                           r.text)
                     if re.search(r"radar|tiles|api|data", u, re.I)})[:25]:
        print("    url:", u.replace("\\/", "/")[:130])
