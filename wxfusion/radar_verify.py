"""Turn the radar archive into point observations, so precipitation gets
verified against what actually fell rather than against a handful of gauges.

Why bother: rain gauges are sparse — a dozen METAR sites and a scatter of
national stations — and they answer "did it rain on this instrument", which
for a 25 km cell is nearly a coin flip. The radar composite already covers
every cell, hourly, and is archived months deep. Sampling it at each forecast
point turns "did it rain here" into something measurable everywhere, which is
what makes the precipitation weights mean anything.

The composites are stored as the recoloured green scale (see
scrape_maps.GREEN_STEPS), so classes are recovered by nearest-colour match
rather than by re-deriving dBZ. That is lossy in absolute terms but ordered
and consistent, which is all a skill score needs.

Sampling is over a neighbourhood, not a single pixel. Radar echo is noisy at
1 km and a point forecast is really a statement about the area around it, so
a 5 km box with "did anything rain in it" is both fairer to the model and
closer to the operational question.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import logging

import numpy as np

from . import config
from .http import session
from .scrape_maps import GREEN_STEPS, r2_client

log = logging.getLogger(__name__)

# Class -> representative rate in mm/h. The composite's six steps are ordered
# but not calibrated, so these are the conventional mid-points of the usual
# light/moderate/heavy radar bands. Good enough to score models against each
# other; not a gauge replacement.
CLASS_RATE = {0: 0.0, 1: 0.2, 2: 0.7, 3: 2.0, 4: 5.0, 5: 12.0, 6: 30.0}
BOX_KM = 5  # neighbourhood half-width in pixels (1 px = 1 km)
RAIN_CLASS = 1  # class at or above which we call it rain


def _palette() -> np.ndarray:
    return np.array([[0, 0, 0, 0]] + [list(c) for c in GREEN_STEPS], dtype=int)


# Nearest-colour alone is not enough: the frames have country borders baked
# on (halo #f7f1e2, core #55503f), and a plain argmin maps the core onto the
# step-4 green — which would report moderate rain along every coastline and
# border, at exactly the coastal stations we care about. Real echo is written
# straight from the palette and survives PNG quantisation exactly (measured:
# distance 0), while the border colours sit 69 and 78 away. So demand a close
# match and treat anything else as background.
MAX_COLOUR_DIST = 20


def classify(arr: np.ndarray) -> np.ndarray:
    """Recoloured RGBA frame -> intensity class 0..6 per pixel."""
    pal = _palette()
    a = arr[..., 3].astype(int)
    rgb = arr[..., :3].astype(int)
    d = ((rgb[:, :, None, :] - pal[None, None, :, :3]) ** 2).sum(axis=3)
    cls = d.argmin(axis=2).astype(np.uint8)
    nearest = np.take_along_axis(d, cls[:, :, None], axis=2)[:, :, 0]
    cls[nearest > MAX_COLOUR_DIST ** 2] = 0
    cls[a <= 40] = 0
    return cls


def sample(cls: np.ndarray, px: int, py: int) -> tuple[int, float]:
    """(max class, rainy fraction) in the BOX_KM neighbourhood of a pixel."""
    h, w = cls.shape
    y0, y1 = max(0, py - BOX_KM), min(h, py + BOX_KM + 1)
    x0, x1 = max(0, px - BOX_KM), min(w, px + BOX_KM + 1)
    box = cls[y0:y1, x0:x1]
    if box.size == 0:
        return 0, 0.0
    return int(box.max()), float((box >= RAIN_CLASS).mean())


def archived_hours() -> dict:
    s3 = r2_client()
    try:
        arch = json.loads(s3.get_object(
            Bucket=config.R2_BUCKET, Key="maps/radar/archive.json")["Body"].read())
    except Exception:
        return {}
    return arch.get("hours", {})


def observations(points: list[dict], hours_back: int = 168,
                 base: str | None = None) -> list[tuple]:
    """Radar-derived observation rows for every archived hour in the window.

    Returns rows shaped for db.upsert_observations, with source 'radar' and
    station_id set to the point_id so a point verifies against the radar over
    itself.
    """
    from . import proj3059 as P
    from PIL import Image

    base = base or config.MAPS_BASE
    hours = archived_hours()
    if not hours:
        log.warning("radar verify: archive index is empty")
        return []

    pts = [p for p in points if p.get("lat") is not None]
    px_of = {}
    for p in pts:
        x, y = P.to_px([p["lat"]], [p["lon"]])
        ix, iy = int(round(float(x[0]))), int(round(float(y[0])))
        if 0 <= ix < P.W and 0 <= iy < P.H:
            px_of[p["point_id"]] = (ix, iy)
    log.info("radar verify: %d of %d points fall inside the radar grid",
             len(px_of), len(pts))

    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    wanted = [(now - dt.timedelta(hours=k)).strftime("%Y%m%dT%H")
              for k in range(1, hours_back + 1)]
    rows: list[tuple] = []
    used = 0
    for tag in wanted:
        vtag = hours.get(tag)
        if not vtag:
            continue
        url = f"{base}/maps/radar/frames3059/{vtag}.png"
        try:
            r = session().get(url, timeout=90)
            r.raise_for_status()
            arr = np.array(Image.open(io.BytesIO(r.content)).convert("RGBA"))
        except Exception:
            log.warning("radar verify: could not read %s", url)
            continue
        cls = classify(arr)
        t = dt.datetime.strptime(tag, "%Y%m%dT%H").replace(tzinfo=dt.timezone.utc)
        for pid, (ix, iy) in px_of.items():
            mx, frac = sample(cls, ix, iy)
            rows.append(("radar", pid, t, "prcp_1h", CLASS_RATE[mx]))
            rows.append(("radar", pid, t, "wx_rain", 1.0 if mx >= RAIN_CLASS else 0.0))
        used += 1
        if used % 24 == 0:
            log.info("radar verify: %d hours sampled", used)

    log.info("radar verify: %d hours -> %d observation values", used, len(rows))
    return rows
