"""Hourly observation ingest: METAR + LVGMC + meteo.lt + EE.

Each source is isolated — one failing feed never blocks the others.
Exit code is non-zero only if ALL sources fail.
"""
import datetime as dt
import logging
import sys

sys.path.insert(0, ".")
from wxfusion import db  # noqa: E402
from wxfusion.ingest import ee_xml, lvgmc, metar_iem, meteolt  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("run_observations")


def main() -> int:
    conn = db.connect()
    metar_points = [p["station_id"] for p in db.get_points(conn, "metar")]
    now = dt.datetime.now(dt.timezone.utc)
    failures = []

    # METAR: trailing 3 h window (idempotent upsert dedupes overlap)
    try:
        rows = metar_iem.fetch(metar_points, now - dt.timedelta(hours=3), now)
        db.upsert_observations(conn, rows)
    except Exception:
        log.exception("metar ingest failed")
        failures.append("metar")

    # LVGMC operative (last hour, all LV stations)
    try:
        db.upsert_observations(conn, lvgmc.fetch_operative())
    except Exception:
        log.exception("lvgmc ingest failed")
        failures.append("lvgmc")

    # meteo.lt: all stations' latest (~24 h each, dedupes on conflict)
    try:
        rows = []
        for st in meteolt.list_stations():
            try:
                rows.extend(meteolt.fetch_latest(st["code"]))
            except Exception:
                log.warning("meteo.lt station %s failed", st["code"])
        db.upsert_observations(conn, rows)
    except Exception:
        log.exception("meteo.lt ingest failed")
        failures.append("lt")

    # EE XML snapshot
    try:
        rows, _stations = ee_xml.fetch()
        db.upsert_observations(conn, rows)
    except Exception:
        log.exception("ee ingest failed")
        failures.append("ee")

    conn.close()
    if failures:
        log.error("failed sources: %s", failures)
    return 1 if len(failures) == 4 else 0


if __name__ == "__main__":
    raise SystemExit(main())
