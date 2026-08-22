"""How far back do the SMHI and FMI open radar archives reach?

Answers whether the SE/FI feeds can be backfilled into the radar store the
way the meteolapa feeds were: for a ladder of past dates, list what each
service still serves and test-download one frame to prove the files resolve.
Throwaway probe, dispatched via missing-probe.yml.
"""
import datetime as dt
import re
import sys

sys.path.insert(0, ".")

from wxfusion.http import session  # noqa: E402
from wxfusion.radar_nordic import FMI_WFS, SMHI_LIST  # noqa: E402

s = session()
now = dt.datetime.now(dt.timezone.utc)

print("=== SMHI (dated listing) ===")
for back in (1, 3, 7, 14, 30, 90, 180, 365, 730):
    day = (now - dt.timedelta(days=back)).date()
    try:
        r = s.get(SMHI_LIST.format(d=day), timeout=45)
        files = r.json().get("files") or [] if r.ok else []
        tifs = [f for f in files
                if any(x.get("key") == "tif" for x in f.get("formats", []))]
        span = (f"{files[0]['valid']} .. {files[-1]['valid']}"
                if files else "-")
        print(f"  -{back:>3}d {day}: HTTP {r.status_code}, "
              f"{len(files)} files ({len(tifs)} with tif)  {span}")
        if back >= 30 and tifs and back in (30, 365, 730):
            url = next(x["link"] for x in tifs[0]["formats"]
                       if x.get("key") == "tif")
            b = s.get(url, timeout=60)
            print(f"        sample tif: HTTP {b.status_code}, "
                  f"{len(b.content)} bytes")
    except Exception as e:
        print(f"  -{back:>3}d {day}: {type(e).__name__}: {e}")

print("=== FMI (WFS starttime/endtime) ===")
for back in (1, 3, 7, 14, 30, 90):
    t0 = now - dt.timedelta(days=back)
    q = (f"{FMI_WFS}&starttime={t0:%Y-%m-%dT%H:00:00Z}"
         f"&endtime={t0 + dt.timedelta(hours=1):%Y-%m-%dT%H:00:00Z}")
    try:
        r = s.get(q, timeout=60)
        urls = re.findall(
            r"<gml:fileReference>\s*([^<]+?)\s*</gml:fileReference>", r.text)
        stamps = re.findall(r"<gml:timePosition>([^<]+)</gml:timePosition>",
                            r.text)
        print(f"  -{back:>3}d {t0:%Y-%m-%d %H}Z: HTTP {r.status_code}, "
              f"{len(urls)} frames  "
              f"({stamps[0] if stamps else '-'} .. "
              f"{stamps[-1] if stamps else '-'})")
        if urls and back in (3, 14, 30):
            b = s.get(urls[0].replace("&amp;", "&"), timeout=120)
            print(f"        sample tif: HTTP {b.status_code}, "
                  f"{len(b.content)} bytes")
    except Exception as e:
        print(f"  -{back:>3}d: {type(e).__name__}: {e}")
