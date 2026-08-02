"""Where the national weather stations actually are.

The observation ingests key everything on station_id and never asked where the
station was, which was fine until something needed to look up what the radar
was painting over the gauge. wx.points had coordinates for METAR, grid cells
and hand-picked points; the three national networks — 34 Latvian, 52
Lithuanian, 154 Estonian — were 240 rows of measurements with no position.

Each source publishes its own list, in its own shape:

  LVGMC          data.gov.lv CKAN, a stations resource alongside the
                 observations. LATITUDE/LONGITUDE are DDMMSS integers
                 (564756 = 56 deg 47' 56"), but GEOGR1/GEOGR2 carry the same
                 thing in decimal degrees, lon first. Use those.
  meteo.lt       /v1/stations, decimal degrees, nested under "coordinates".
  ilmateenistus  the observations XML itself carries latitude/longitude on
                 every station element, so no second request.

Written into wx.points with kind matching observations_h.source, which is what
lets the two be joined.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

from .. import config
from ..http import session

log = logging.getLogger(__name__)


def _lvgmc() -> list[dict]:
    rows, offset = [], 0
    while True:
        r = session().get(
            "https://data.gov.lv/dati/api/3/action/datastore_search",
            params={"resource_id": config.LVGMC_RES_STATIONS,
                    "limit": 1000, "offset": offset}, timeout=60)
        r.raise_for_status()
        res = r.json()["result"]
        for rec in res["records"]:
            lat, lon = rec.get("GEOGR2"), rec.get("GEOGR1")
            if lat is None or lon is None:
                continue
            rows.append({
                "kind": "lvgmc", "station_id": str(rec["STATION_ID"]).strip(),
                "name": (rec.get("NAME") or "").strip(),
                "lat": float(lat), "lon": float(lon),
                "elevation_m": float(rec["ELEVATION"]) if rec.get("ELEVATION") not in (None, "") else None,
            })
        offset += len(res["records"])
        if offset >= res["total"] or not res["records"]:
            break
    return rows


def _meteolt() -> list[dict]:
    r = session().get(f"{config.METEOLT_BASE}/stations", timeout=40)
    r.raise_for_status()
    out = []
    for st in r.json():
        c = st.get("coordinates") or {}
        if c.get("latitude") is None:
            continue
        out.append({"kind": "lt", "station_id": st["code"],
                    "name": st.get("name") or st["code"],
                    "lat": float(c["latitude"]), "lon": float(c["longitude"]),
                    "elevation_m": None})
    return out


def _ilmateenistus() -> list[dict]:
    r = session().get(config.EE_OBS_URL, timeout=40)
    r.raise_for_status()
    out = []
    for st in ET.fromstring(r.content).findall(".//station"):
        lat, lon = st.findtext("latitude"), st.findtext("longitude")
        # .strip(), because the observation ingest strips and the join is on
        # this string. The feed ships at least one name with a trailing space
        # ("Tuulemäe "), and without this that station's readings can never be
        # matched to a position — silently, and only for that station.
        name = (st.findtext("name") or "").strip()
        if not (lat and lon and name):
            continue
        out.append({"kind": "ee", "station_id": name, "name": name,
                    "lat": float(lat), "lon": float(lon), "elevation_m": None})
    return out


def all_stations() -> list[dict]:
    """Every national station we ingest, with coordinates. One bad source does
    not take the other two down — a partial refresh beats none."""
    out = []
    for name, fn in (("lvgmc", _lvgmc), ("meteo.lt", _meteolt),
                     ("ilmateenistus", _ilmateenistus)):
        try:
            rows = fn()
            log.info("stations: %s -> %d", name, len(rows))
            out.extend(rows)
        except Exception:
            log.exception("stations: %s failed", name)
    return out


UPSERT = """
insert into wx.points (point_id, kind, station_id, name, lat, lon, elevation_m, active)
values (%(point_id)s, %(kind)s, %(station_id)s, %(name)s, %(lat)s, %(lon)s,
        %(elevation_m)s, true)
on conflict (point_id) do update set
  lat = excluded.lat, lon = excluded.lon, name = excluded.name,
  elevation_m = coalesce(excluded.elevation_m, wx.points.elevation_m)
"""


def store(conn) -> int:
    rows = all_stations()
    n = 0
    with conn.cursor() as cur:
        for r in rows:
            # point_id has to be unique across networks; two of them could
            # plausibly name a station the same thing.
            r = {**r, "point_id": f"{r['kind']}:{r['station_id']}"}
            cur.execute(UPSERT, r)
            n += 1
    conn.commit()
    log.info("stations: %d rows upserted into wx.points", n)
    return n
