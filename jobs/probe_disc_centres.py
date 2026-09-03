"""Are the blank UM leads flapping, or is the tile stitch losing them?

Both answers turned out to be no. Three rounds four minutes apart against
the 03.09.2026 00Z run, every tile of the 3x3 grid measured separately:

    lead   35,18 36,18 37,18 35,19 36,19 37,19 35,20 36,20 37,20  stitched
       3      0%    0%    0%    0%    0%    0%    0%    0%    0%     EMPTY
       6      0%    0%    0%    0%    0%    0%    0%    0%    0%     EMPTY
      12      0%    0%    0%    0%    0%    0%    0%    0%    0%     EMPTY
       4     84%  100%  100%   95%  100%  100%  100%  100%   92%       97%

unchanged across all three rounds. So the leads are not unstable and the
stitch is faithful — every tile agrees, including the middle one a single
tile sample had read as 100% full twenty minutes earlier. That earlier
reading has not been reproduced since and remains unexplained; the honest
statement is that +3, +6 and +12 are blank at source and we do not know why
those leads.

Left here as the tool that settled it. To characterise the timescale, raise
ROUNDS and GAP_S — the earlier full/blank disagreement spanned twenty-odd
minutes, which four-minute rounds are too tight to catch.
"""
import datetime as dt
import sys
import time

sys.path.insert(0, ".")

import numpy as np  # noqa: E402

from wxfusion import scrape_maps as S  # noqa: E402
from wxfusion.http import session  # noqa: E402
from PIL import Image  # noqa: E402
import io  # noqa: E402

s = session()
now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0,
                                               microsecond=0, tzinfo=None)
LEADS = (3, 6, 12, 4)          # three suspects and one control
ROUNDS = 3
GAP_S = 240

x0, x1, y0, y1 = S._tile_range(S.ZOOM)


def tile(lead, xi, yi):
    """One tile, exactly as fetch_um_frame asks for it."""
    bb = S._tile_bbox_900913(S.ZOOM, xi, yi)
    params = {"service": "WMS", "version": "1.1.1", "request": "GetMap",
              "layers": "um:UM4_CLOUD", "styles": "",
              "bbox": ",".join(f"{v:.9f}" for v in bb),
              "width": 256, "height": 256, "srs": "EPSG:900913",
              "format": "image/png", "transparent": "true", "tiled": "true",
              "TIME": run.strftime("%Y-%m-%dT%H:%M:%S") + "Z",
              "DIM_FORECAST": lead}
    try:
        r = s.get(S.GWC, params=params, timeout=60)
    except Exception as e:
        return None, type(e).__name__
    if not r.ok or "image" not in r.headers.get("content-type", ""):
        return None, f"HTTP{r.status_code}"
    a = np.array(Image.open(io.BytesIO(r.content)).convert("RGBA"))[..., 3]
    return a, f"{100 * (a > 0).mean():.0f}%"


# Probe whichever run the scrape would settle on, not whichever is newest —
# the 12Z cycle is typically hours from being published when this runs, and
# an unpublished run is blank at every lead, which proves nothing.
run = now.replace(hour=0 if now.hour < 12 else 12)
for _ in range(4):
    if any((p := S.fetch_um_frame("um:UM4_CLOUD", run, pl)) is not None
           and np.array(p)[..., 3].any() for pl in (4, 9, 18, 2, 24, 36)):
        break
    print(f"run {run:%Y-%m-%dT%H}Z not published, stepping back")
    run -= dt.timedelta(hours=12)

print(f"\nrun {run:%Y-%m-%dT%H}Z, tiles x{x0}-{x1} y{y0}-{y1}, "
      f"{ROUNDS} rounds {GAP_S}s apart\n")
cells = [(xi, yi) for yi in range(y0, y1 + 1) for xi in range(x0, x1 + 1)]
hdr = "  ".join(f"{xi},{yi}" for xi, yi in cells)
print(f"{'round':>5} {'lead':>5}  {hdr}   stitched")

for rnd in range(1, ROUNDS + 1):
    for lead in LEADS:
        marks, any_px = [], False
        for xi, yi in cells:
            a, mark = tile(lead, xi, yi)
            marks.append(mark)
            if a is not None and a.any():
                any_px = True
        # What fetch_um_frame would conclude for the whole frame.
        img = S.fetch_um_frame("um:UM4_CLOUD", run, lead)
        if img is None:
            verdict = "no tiles"
        else:
            arr = np.array(img)
            verdict = ("EMPTY" if not arr[..., 3].any()
                       else f"{100 * (arr[..., 3] > 0).mean():.0f}%")
        cols = "  ".join(f"{m:>5}" for m in marks)
        print(f"{rnd:>5} {lead:>5}  {cols}   {verdict}"
              f"{'' if any_px == (verdict != 'EMPTY') else '   <- DISAGREES'}",
              flush=True)
    if rnd < ROUNDS:
        time.sleep(GAP_S)
