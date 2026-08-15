"""Debug: render the current radar composite two ways — RAW (no beam removal)
and CLEAN (the live pipeline) — and print both as base64 PNG into the job log,
so the frame can be inspected without R2/network access from the dev box.

Set RADAR_SNAP_TS=YYYY-MM-DDTHH:MM (UTC) to snapshot a specific hour instead of
the newest available frames.
"""
import base64
import datetime as dt
import io
import logging
import os
import sys

sys.path.insert(0, ".")
import numpy as np                       # noqa: E402
from PIL import Image                    # noqa: E402

from wxfusion import scrape_maps as sm   # noqa: E402
from wxfusion import proj3059 as P       # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("beam_snapshot")

ORDER = ("EE", "LT2", "LT", "LV")


def _raw_grid(url, s, code, o):
    """Classify + warp with NO beam removal at all (source or polar)."""
    body = sm.overlay_bytes(url, s)
    if body is None:
        return None
    img = Image.open(io.BytesIO(body)).convert("RGBA")
    if max(img.size) > sm.LT_MAXSIZE:
        sc = sm.LT_MAXSIZE / max(img.size)
        img = img.resize((int(img.width * sc), int(img.height * sc)), Image.NEAREST)
    cls = sm._classify_intensity(np.array(img), code)
    return P.resample_mercator_image(cls, o["sw"], o["ne"])


def _compose(overlays, target, cold, clean):
    canvas = Image.new("RGBA", (P.W, P.H), (0, 0, 0, 0))
    echo = np.zeros((P.H, P.W), bool)               # union of classified echo
    for code in ORDER:
        cands = [o for o in overlays if o["code"] == code]
        if not cands:
            continue
        best = min(cands, key=lambda o: abs((o["time"] - target).total_seconds()))
        if abs((best["time"] - target).total_seconds()) > 40 * 60:
            continue
        if clean:
            cls = sm._load_and_classify(best["url"], s, code, best)   # source repair
            if cls is None:
                continue
            grid = P.resample_mercator_image(cls, best["sw"], best["ne"])
            grid = sm._close_radial_spokes(sm._despeckle_intensity(
                sm._remove_beam_lines(sm._remove_beams_polar(sm._despeckle(grid)))))
        else:
            grid = _raw_grid(best["url"], s, code, best)
            if grid is None:
                continue
        if grid.shape == echo.shape:
            echo |= grid > 0
        canvas.alpha_composite(sm._recolor(grid, cold, code))
    canvas = sm._fade_edges(canvas)
    sm.draw_borders(canvas)
    return canvas, echo


def _diff_img(raw_echo, clean_echo):
    """removed (raw&~clean)=red, added (clean&~raw)=blue, kept=green, on cream."""
    h, w = raw_echo.shape
    img = np.full((h, w, 3), (251, 243, 222), np.uint8)
    kept = raw_echo & clean_echo
    removed = raw_echo & ~clean_echo
    added = clean_echo & ~raw_echo
    img[kept] = (150, 200, 150)
    img[removed] = (220, 30, 30)
    img[added] = (30, 60, 230)
    return Image.fromarray(img, "RGB")


def _emit(name, img, width=540):
    # cream backdrop so transparent areas are visible
    bg = Image.new("RGB", img.size, (251, 243, 222))
    bg.paste(img.convert("RGB"), (0, 0), img.split()[-1] if img.mode == "RGBA" else None)
    if width != img.width:
        bg = bg.resize((width, int(bg.height * width / bg.width)), Image.NEAREST)
    buf = io.BytesIO()
    bg.save(buf, "PNG", optimize=True)
    b = base64.b64encode(buf.getvalue()).decode()
    print(f"@@SNAP_BEGIN {name} {len(b)}")
    for i in range(0, len(b), 120):
        print(b[i:i + 120])
    print(f"@@SNAP_END {name}")


s = sm.session()
ts = os.environ.get("RADAR_SNAP_TS", "").strip()
if ts:
    target = dt.datetime.fromisoformat(ts)
    if target.tzinfo is None:
        target = target.replace(tzinfo=dt.timezone.utc)
    overlays = sm.fetch_overlays_window(target - dt.timedelta(minutes=40),
                                        target + dt.timedelta(minutes=40))
else:
    overlays = sm.parse_radar_overlays()
    target = max((o["time"] for o in overlays), default=dt.datetime.now(dt.timezone.utc))

log.info("snapshot target %s, %d overlays", target.isoformat(), len(overlays))
cold = sm._freezing_mask(target)
raw_canvas, raw_echo = _compose(overlays, target, cold, clean=False)
clean_canvas, clean_echo = _compose(overlays, target, cold, clean=True)
_emit("RAW", raw_canvas)
_emit("CLEAN", clean_canvas)
_emit("DIFF", _diff_img(raw_echo, clean_echo))
log.info("diff: %d removed, %d added, %d kept px",
         int((raw_echo & ~clean_echo).sum()), int((clean_echo & ~raw_echo).sum()),
         int((raw_echo & clean_echo).sum()))
log.info("snapshot done")
