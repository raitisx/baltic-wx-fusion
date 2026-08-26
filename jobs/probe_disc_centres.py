"""How far back do the per-radar archives actually go?

Backfilling 22.08 stored nothing: FMI listed 67 sweeps for each radar and
none landed in a slot, SMHI listed no volumes at all. Both need the same
question answered — what is IN the listing for a given day, by stamp.
"""
import datetime as dt
import sys

sys.path.insert(0, ".")

from wxfusion import radar_nordic as N  # noqa: E402

s = N.session()
today = dt.datetime.now(dt.timezone.utc).date()
days = [today - dt.timedelta(days=k) for k in range(0, 6)]

print("=== FMI per-radar cappi (S3 mirror), site fikor ===")
for d in days:
    ent = N._fmi_site_entries(s, d, "fikor")
    if ent:
        first, last = ent[0][0], ent[-1][0]
        gaps = [(ent[i + 1][0] - ent[i][0]).total_seconds() / 60
                for i in range(len(ent) - 1)]
        med = sorted(gaps)[len(gaps) // 2] if gaps else 0
        print(f"  {d}  {len(ent):4d} sweeps  {first:%H:%M}..{last:%H:%M}Z  "
              f"median gap {med:.0f} min")
    else:
        print(f"  {d}  none")

print("\n=== FMI: is it the listing or the pagination? (raw key count) ===")
for d in (days[0], days[-1]):
    r = s.get(f"{N.FMI_S3}/?list-type=2&max-keys=1000"
              f"&prefix={d:%Y/%m/%d}/fikor/", timeout=60)
    import re
    keys = re.findall(r"<Key>([^<]+)</Key>", r.text)
    trunc = "<IsTruncated>true</IsTruncated>" in r.text
    kinds = {}
    for k in keys:
        kinds[k.rsplit("_", 2)[-2] + "_" + k.rsplit("_", 1)[-1]] = \
            kinds.get(k.rsplit("_", 2)[-2] + "_" + k.rsplit("_", 1)[-1], 0) + 1
    print(f"  {d}  HTTP {r.status_code}  {len(keys)} keys  truncated={trunc}")
    for kind, n in sorted(kinds.items(), key=lambda x: -x[1])[:6]:
        print(f"       {n:4d}  ...{kind}")

print("\n=== SMHI per-radar volumes (qcvol), area hemse ===")
for d in days:
    url = f"{N.SMHI_SITE_API.format(site='hemse')}/{d:%Y/%m/%d}"
    try:
        r = s.get(url, timeout=45)
        files = (r.json().get("files") or []) if r.ok else []
        print(f"  {d}  HTTP {r.status_code}  {len(files)} files"
              + (f"  {files[0]['valid']}..{files[-1]['valid']}"
                 if files else ""))
        if not r.ok:
            print(f"       {r.text[:160]}")
    except Exception as e:
        print(f"  {d}  {type(e).__name__}: {e}")

print("\n=== SMHI: what does the product root say it holds? ===")
r = s.get(N.SMHI_SITE_API.format(site="hemse"), timeout=45)
print(f"  HTTP {r.status_code}")
if r.ok:
    j = r.json()
    print("  keys:", list(j)[:12])
    for k in ("periods", "validTime", "totalArchive", "archive"):
        if k in j:
            print(f"  {k}: {str(j[k])[:400]}")
