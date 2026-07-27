"""Hourly forecast ingest: Open-Meteo multi-model extracts at all points."""
import logging
import sys

sys.path.insert(0, ".")
from wxfusion import db  # noqa: E402
from wxfusion.ingest import openmeteo  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> int:
    conn = db.connect()
    points = db.get_points(conn)
    rows = openmeteo.fetch(points)
    db.upsert_forecasts(conn, rows)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
