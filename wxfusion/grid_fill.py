"""Background fill points for the fused map.

The meteogram is a single point; a map is thousands of them. Open-Meteo has no
gridded product, so a map built from it is one point request per cell — and the
free tier (10k weighted calls/day) will not carry a dense grid on top of the
station ingest. What it WILL carry is the headroom left over: enough extra
points, laid only where no station already sits, to turn the 404 clustered
stations into a roughly even field.

This module only decides WHERE those points go. The download (Open-Meteo, the
nine fields the map draws, one pass a day) and the store (Parquet in R2, kept
out of Postgres and out of the verification pipeline — a grid cell has no
observation to be scored against) live in jobs/run_grid_fill.py.

Placement rules, in order:
  * inside the EPSG:3059 render rectangle — a point the map never shows is
    budget spent for nothing;
  * at the finest even spacing whose count fits GRID_FILL_MAX_POINTS, so the
    grid is as dense as the day's budget allows and no denser;
  * dropped where a real station is within GRID_FILL_MIN_KM — the stations
    already cover those cells, and at the coast they cover them better.
"""
from __future__ import annotations

import logging
import os

from . import proj3059 as P

log = logging.getLogger(__name__)

# The most fill points a single daily pass may request. Sized to the free-tier
# headroom after the station ingest. Measured against the live network (~404
# active points): stations cost ~5,250 weighted calls/day (13 vars x 5 models x
# 2 runs), and a fill point on the map's nine fields at one pass a day is ~4.5.
# The spacing ladder then lands on 0.22 deg = ~869 points = ~3,900/day, so the
# day totals ~9,160 = 92% of the 10k cap — near the limit, 8% of margin left
# for the per-minute/hour throttle. That margin is also why the fill runs in
# its OWN hour, not inside a station-ingest run: 5,000/hour would otherwise be
# breached by the two together. Raise this only with a commercial key.
MAX_POINTS = int(os.environ.get("GRID_FILL_MAX_POINTS") or "900")
# How close a station has to be for its cell to be left to it. ~12 km ~ one
# 0.1-0.15 deg step, so a station suppresses roughly its own grid cell and no
# more.
MIN_STATION_KM = float(os.environ.get("GRID_FILL_MIN_KM") or "12")

# Candidate spacings, finest first. The builder walks these and keeps the first
# whose surviving count fits the budget, so the grid auto-coarsens on a day the
# station set (and therefore the suppression) shifts.
_STEPS_DEG = [0.10, 0.12, 0.15, 0.18, 0.20, 0.22, 0.25, 0.30, 0.35, 0.40]


def _domain_lonlat_bbox() -> tuple[float, float, float, float]:
    """(lon0, lon1, lat0, lat1) enclosing the EPSG:3059 render rectangle.

    The projection is not axis aligned, so a lon/lat box around it is a little
    larger than the rectangle; points outside the rectangle are dropped later
    by the exact test, this only bounds the candidate scan."""
    import numpy as np
    xs = np.linspace(P.X0, P.X1, 40)
    ys = np.linspace(P.Y0, P.Y1, 40)
    gx, gy = np.meshgrid(xs, ys)
    lon, lat = P.to_lonlat(gx.ravel(), gy.ravel())
    return float(lon.min()), float(lon.max()), float(lat.min()), float(lat.max())


def _inside_rect(lat, lon):
    """Boolean mask: which lon/lat land inside the EPSG:3059 rectangle."""
    x, y = P.to_xy(lon, lat)
    return (x >= P.X0) & (x <= P.X1) & (y >= P.Y0) & (y <= P.Y1)


def _candidates(step: float):
    """Even lon/lat lattice over the domain bbox, clipped to the rectangle."""
    import numpy as np
    lo0, lo1, la0, la1 = _domain_lonlat_bbox()
    lons = np.arange(lo0, lo1 + 1e-9, step)
    lats = np.arange(la0, la1 + 1e-9, step)
    LO, LA = np.meshgrid(lons, lats)
    LO, LA = LO.ravel(), LA.ravel()
    keep = _inside_rect(LA, LO)
    return LA[keep], LO[keep]


def _suppress_near_stations(lat, lon, stations, min_km: float):
    """Drop candidates within min_km of any station (distance in projected m)."""
    import numpy as np
    if not stations:
        return np.ones(lat.shape, bool)
    sx, sy = P.to_xy(
        np.array([s["lon"] for s in stations], float),
        np.array([s["lat"] for s in stations], float),
    )
    cx, cy = P.to_xy(lon, lat)
    try:
        from scipy.spatial import cKDTree
        tree = cKDTree(np.column_stack([sx, sy]))
        d, _ = tree.query(np.column_stack([cx, cy]), k=1)
    except Exception:  # scipy missing — O(n*m) fallback, fine for these sizes
        d = np.full(cx.shape, np.inf)
        for x0, y0 in zip(sx, sy):
            d = np.minimum(d, np.hypot(cx - x0, cy - y0))
    return d >= min_km * 1000.0


def build_fill_points(stations: list[dict],
                      max_points: int = MAX_POINTS,
                      min_station_km: float = MIN_STATION_KM) -> list[dict]:
    """Fill points as [{point_id, lat, lon}], gaps only, budget-capped.

    stations: dicts with lat/lon (the real network — its cells are left to it).
    Picks the finest spacing whose surviving count fits max_points.
    """
    import numpy as np
    chosen = None
    for step in _STEPS_DEG:
        lat, lon = _candidates(step)
        keep = _suppress_near_stations(lat, lon, stations, min_station_km)
        lat, lon = lat[keep], lon[keep]
        if len(lat) <= max_points:
            chosen = (step, lat, lon)
            break
    if chosen is None:  # even the coarsest overflows — take it and trim
        step = _STEPS_DEG[-1]
        lat, lon = _candidates(step)
        keep = _suppress_near_stations(lat, lon, stations, min_station_km)
        lat, lon = lat[keep][:max_points], lon[keep][:max_points]
        chosen = (step, lat, lon)

    step, lat, lon = chosen
    pts = [
        {"point_id": f"g:{la:.3f}:{lo:.3f}", "lat": round(float(la), 4),
         "lon": round(float(lo), 4)}
        for la, lo in zip(lat, lon)
    ]
    log.info("grid fill: %d points at %.2f deg (~%.0f km), min-station %.0f km",
             len(pts), step, step * 111.0, min_station_km)
    return pts
