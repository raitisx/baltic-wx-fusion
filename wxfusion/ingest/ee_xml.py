"""Estonia: Keskkonnaagentuur real-time XML feed.

Attribution required: credit Keskkonnaagentuur + link to ilmateenistus.ee.
The feed is REAL-TIME ONLY (last reading per station) — run every 10-60 min
and archive; there is no public bulk-history endpoint.

Feed values: temperature °C, wind m/s (windspeedmax = gust), precipitation mm
(accumulation over the last hour at :00 pulls), pressure hPa, humidity %,
visibility km.
"""
from __future__ import annotations

import datetime as dt
import logging
import xml.etree.ElementTree as ET

from .. import config
from ..http import session

log = logging.getLogger(__name__)

FIELD_MAP = {
    "airtemperature": ("t2m", 1.0),
    "relativehumidity": ("rh", 1.0),
    "windspeed": ("ws10m", 1.0),
    "windspeedmax": ("wg10m", 1.0),
    "winddirection": ("wdir", 1.0),
    "precipitations": ("prcp_1h", 1.0),
    "airpressure": ("pres_msl", 1.0),
    "visibility": ("vis", 1000.0),  # km -> m
}


def fetch() -> tuple[list[tuple], list[dict]]:
    """Return (observation rows, station metadata seen).

    Station id = station name from the feed (stable identifier used by EE).
    """
    r = session().get(config.EE_OBS_URL, timeout=60)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    ts = int(root.attrib.get("timestamp", "0"))
    # Feed timestamp is Unix epoch; round to nearest 10 min slot.
    t = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
    t = t.replace(minute=t.minute - t.minute % 10, second=0, microsecond=0)

    rows: list[tuple] = []
    stations: list[dict] = []
    for st in root.iter("station"):
        name = (st.findtext("name") or "").strip()
        if not name:
            continue
        lat, lon = st.findtext("latitude"), st.findtext("longitude")
        stations.append(
            {"station_id": name, "lat": float(lat) if lat else None,
             "lon": float(lon) if lon else None}
        )
        for field, (param, scale) in FIELD_MAP.items():
            raw = (st.findtext(field) or "").strip()
            if raw:
                try:
                    rows.append(("ee", name, t, param, float(raw) * scale))
                except ValueError:
                    pass
    log.info("ee: %d stations, %d values @ %s", len(stations), len(rows), t)
    return rows, stations
