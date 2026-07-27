"""METAR via Iowa State IEM ASOS archive (free, live + full history).

Provides the only direct observed cloud base (ceiling). Also derives a binary
rain flag from present-weather codes, because European METAR precip amounts
are unreliable.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import logging

from .. import config
from ..http import session

log = logging.getLogger(__name__)

KT = 0.514444  # knots -> m/s
FT = 0.3048
MILE = 1609.34

RAIN_CODES = ("RA", "DZ", "SN", "SG", "PL", "GR", "GS", "UP", "TS")


def fetch(stations: list[str], start: dt.datetime, end: dt.datetime) -> list[tuple]:
    """Fetch METARs for [start, end) and return canonical rows."""
    params = {
        "station": stations,
        "data": ["tmpc", "dwpc", "relh", "drct", "sknt", "gust", "mslp",
                 "p01m", "vsby", "wxcodes",
                 "skyc1", "skyl1", "skyc2", "skyl2", "skyc3", "skyl3",
                 "skyc4", "skyl4"],
        "year1": start.year, "month1": start.month, "day1": start.day,
        "hour1": start.hour, "minute1": start.minute,
        "year2": end.year, "month2": end.month, "day2": end.day,
        "hour2": end.hour, "minute2": end.minute,
        "tz": "Etc/UTC", "format": "onlycomma", "latlon": "no",
        "missing": "empty", "trace": "0.0001", "direct": "no",
        "report_type": [3, 4],  # routine + specials
    }
    r = session().get(
        f"{config.IEM_BASE}/cgi-bin/request/asos.py", params=params, timeout=120
    )
    r.raise_for_status()
    rows: list[tuple] = []
    reader = csv.DictReader(io.StringIO(r.text))
    for rec in reader:
        icao = rec.get("station", "").strip()
        try:
            t = dt.datetime.strptime(rec["valid"], "%Y-%m-%d %H:%M").replace(
                tzinfo=dt.timezone.utc
            )
        except (KeyError, ValueError):
            continue

        def num(key: str) -> float | None:
            v = (rec.get(key) or "").strip()
            if v in ("", "M", "T"):
                return None
            try:
                return float(v)
            except ValueError:
                return None

        for key, param, scale in (
            ("tmpc", "t2m", 1.0), ("dwpc", "td2m", 1.0), ("relh", "rh", 1.0),
            ("drct", "wdir", 1.0), ("sknt", "ws10m", KT), ("gust", "wg10m", KT),
            ("mslp", "pres_msl", 1.0), ("p01m", "prcp_1h", 1.0),
            ("vsby", "vis", MILE),
        ):
            v = num(key)
            if v is not None:
                rows.append(("metar", icao, t, param, v * scale))

        # Ceiling: lowest BKN/OVC/VV layer, feet -> m AGL
        ceiling = None
        for i in (1, 2, 3, 4):
            cover = (rec.get(f"skyc{i}") or "").strip()
            lvl = num(f"skyl{i}")
            if cover in ("BKN", "OVC", "VV") and lvl is not None:
                m = lvl * FT
                ceiling = m if ceiling is None else min(ceiling, m)
        if ceiling is not None:
            rows.append(("metar", icao, t, "cb_ceiling", ceiling))

        # Binary precip flag from present weather
        wx = (rec.get("wxcodes") or "").strip()
        if wx:
            raining = any(code in wx for code in RAIN_CODES)
            rows.append(("metar", icao, t, "wx_rain", 1.0 if raining else 0.0))
    log.info("metar: %d canonical values for %d stations", len(rows), len(stations))
    return rows
