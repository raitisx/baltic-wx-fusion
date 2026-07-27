"""Forecast ingest: Open-Meteo multi-model station-point extracts.

One request per batch of points fetches all configured models at once
(Open-Meteo suffixes variables with the model id). Full grids intentionally
NOT ingested in Phase 1 — see README.

run_time caveat: Open-Meteo does not expose the underlying model run time, so
we stamp run_time = retrieval hour (UTC). That is what the forecast "knew" at
retrieval, which is exactly the decision-time semantics the "why we didn't
fly" report needs. True model cycle times can be added later via the
raw-GRIB path.
"""
from __future__ import annotations

import datetime as dt
import logging

from .. import config
from ..http import session

log = logging.getLogger(__name__)

HOURLY_VARS = {
    "temperature_2m": "t2m",
    "dew_point_2m": "td2m",
    "relative_humidity_2m": "rh",
    "precipitation": "prcp_1h",
    "wind_speed_10m": "ws10m",
    "wind_gusts_10m": "wg10m",
    "wind_direction_10m": "wdir",
    "pressure_msl": "pres_msl",
    "cloud_cover": "cc_total",
    "cloud_cover_low": "cc_low",
    "cloud_cover_mid": "cc_mid",
    "cloud_cover_high": "cc_high",
}


def lcl_height(t2m: float, td2m: float) -> float:
    """Lifted condensation level in m AGL (Espy approximation)."""
    return max(0.0, 125.0 * (t2m - td2m))


def fetch(points: list[dict]) -> list[tuple]:
    """points: dicts with point_id/lat/lon. Returns wx.forecasts rows."""
    if not points:
        return []
    run_time = dt.datetime.now(dt.timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )
    s = session()
    rows: list[tuple] = []
    # Open-Meteo supports comma-separated multi-location requests.
    lat = ",".join(f"{p['lat']:.4f}" for p in points)
    lon = ",".join(f"{p['lon']:.4f}" for p in points)
    r = s.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat,
            "longitude": lon,
            "hourly": ",".join(HOURLY_VARS),
            "models": ",".join(config.OPENMETEO_MODELS.values()),
            "forecast_days": config.FORECAST_DAYS,
            "windspeed_unit": "ms",
            "timezone": "UTC",
        },
        timeout=120,
    )
    r.raise_for_status()
    payload = r.json()
    locations = payload if isinstance(payload, list) else [payload]

    for point, loc in zip(points, locations):
        hourly = loc.get("hourly", {})
        times = [
            dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone.utc)
            for t in hourly.get("time", [])
        ]
        for our_model, om_model in config.OPENMETEO_MODELS.items():
            # collect per-variable arrays for this model
            series: dict[str, list] = {}
            for om_var, param in HOURLY_VARS.items():
                arr = hourly.get(f"{om_var}_{om_model}") or hourly.get(om_var)
                if arr:
                    series[param] = arr
            for i, valid in enumerate(times):
                t2m = td2m = None
                for param, arr in series.items():
                    v = arr[i] if i < len(arr) else None
                    if v is None:
                        continue
                    rows.append(
                        (our_model, run_time, valid, point["point_id"], param, float(v))
                    )
                    if param == "t2m":
                        t2m = float(v)
                    elif param == "td2m":
                        td2m = float(v)
                # Derived cloud base (LCL) — P1 proxy, see requirements doc.
                if t2m is not None and td2m is not None:
                    rows.append(
                        (our_model, run_time, valid, point["point_id"],
                         "cb_lcl", lcl_height(t2m, td2m))
                    )
    log.info("openmeteo: %d forecast rows (%d points, %d models)",
             len(rows), len(points), len(config.OPENMETEO_MODELS))
    return rows
