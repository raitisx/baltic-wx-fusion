"""Lithuania: meteo.lt API observations (CC BY-SA 4.0, attribution required).

Data: https://api.meteo.lt — 180 req/min, 20 000 req/day. 10 years of history
available per station via /observations/{YYYY-MM-DD}, so backfill is possible.
"""
from __future__ import annotations

import datetime as dt
import logging

from .. import config
from ..http import session

log = logging.getLogger(__name__)

PARAM_MAP = {
    "airTemperature": ("t2m", 1.0),
    "relativeHumidity": ("rh", 1.0),
    "windSpeed": ("ws10m", 1.0),
    "windGust": ("wg10m", 1.0),
    "windDirection": ("wdir", 1.0),
    "cloudCover": ("cc_total", 1.0),  # already %
    "seaLevelPressure": ("pres_msl", 1.0),
    "precipitation": ("prcp_1h", 1.0),
}


def list_stations() -> list[dict]:
    r = session().get(f"{config.METEOLT_BASE}/stations", timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_latest(station_code: str) -> list[tuple]:
    """Return canonical observation rows for the last ~24 h of one station."""
    s = session()
    r = s.get(
        f"{config.METEOLT_BASE}/stations/{station_code}/observations/latest",
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    rows: list[tuple] = []
    for ob in data.get("observations", []):
        t = dt.datetime.strptime(
            ob["observationTimeUtc"], "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=dt.timezone.utc)
        for src_key, (param, scale) in PARAM_MAP.items():
            v = ob.get(src_key)
            if v is not None:
                rows.append(("lt", station_code, t, param, float(v) * scale))
    return rows


def fetch_date(station_code: str, date: dt.date) -> list[tuple]:
    """Historical day (backfill). Same shape as fetch_latest."""
    s = session()
    r = s.get(
        f"{config.METEOLT_BASE}/stations/{station_code}/observations/{date.isoformat()}",
        timeout=30,
    )
    r.raise_for_status()
    rows: list[tuple] = []
    for ob in r.json().get("observations", []):
        t = dt.datetime.strptime(
            ob["observationTimeUtc"], "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=dt.timezone.utc)
        for src_key, (param, scale) in PARAM_MAP.items():
            v = ob.get(src_key)
            if v is not None:
                rows.append(("lt", station_code, t, param, float(v) * scale))
    return rows
