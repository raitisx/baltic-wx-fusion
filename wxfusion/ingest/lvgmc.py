"""Latvia: LVGMC hourly observations via data.gov.lv CKAN datastore (CC0).

Dataset 'hidrometeorologiskie-noverojumi':
- operative resource: last hour, all stations
- archive resource (faktiskie): trailing 365 days (use for backfill)

Record shape: STATION_ID, ABBREVIATION, DATETIME, VALUE.
NOTE ON TIME: DATETIME carries no zone marker. It is assumed UTC here —
VERIFY against a known event (e.g. compare RIGASLU HTDRY with EVRA METAR)
after the first day of collection, and flip ASSUME_UTC if needed.
"""
from __future__ import annotations

import datetime as dt
import logging

from .. import config
from ..http import session

log = logging.getLogger(__name__)

CKAN = "https://data.gov.lv/dati/api/3/action/datastore_search"
ASSUME_UTC = True

# LVGMC abbreviation -> (canonical parameter, scale)
PARAM_MAP = {
    "HTDRY": ("t2m", 1.0),        # hourly avg air temperature
    "HRLH": ("rh", 1.0),
    "HWNDS": ("ws10m", 1.0),
    "WPGST": ("wg10m", 1.0),      # max gusts in observation window
    "HWDAV": ("wdir", 1.0),
    "HPRAB": ("prcp_1h", 1.0),    # hourly precip total
    "HPRSL": ("pres_msl", 1.0),
    "VSBAV": ("vis", 1.0),        # already meters
    "CCTMX": ("cc_total", 12.5),  # oktas 0-8 -> %
    "HSNOW": ("snow", 1.0),
}


def _parse_time(s: str) -> dt.datetime:
    t = dt.datetime.fromisoformat(s)
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc if ASSUME_UTC else None)
    return t


def _fetch_resource(resource_id: str, limit: int, offset: int = 0) -> list[dict]:
    r = session().get(
        CKAN,
        params={"resource_id": resource_id, "limit": limit, "offset": offset},
        timeout=60,
    )
    r.raise_for_status()
    d = r.json()
    if not d.get("success"):
        raise RuntimeError(f"CKAN error: {d}")
    return d["result"]["records"]


def fetch_operative() -> list[tuple]:
    """All stations, last hour (~a few thousand records)."""
    rows: list[tuple] = []
    offset, page = 0, 10000
    while True:
        records = _fetch_resource(config.LVGMC_RES_OPERATIVE, page, offset)
        if not records:
            break
        for rec in records:
            mapped = PARAM_MAP.get(rec.get("ABBREVIATION", ""))
            if not mapped or rec.get("VALUE") is None:
                continue
            param, scale = mapped
            rows.append(
                ("lvgmc", rec["STATION_ID"], _parse_time(rec["DATETIME"]),
                 param, float(rec["VALUE"]) * scale)
            )
        if len(records) < page:
            break
        offset += page
    log.info("lvgmc: %d canonical values", len(rows))
    return rows


def fetch_stations() -> list[dict]:
    return _fetch_resource(config.LVGMC_RES_STATIONS, 1000)
