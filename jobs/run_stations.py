"""Station coordinates into wx.points.

The observation ingests key on station_id and never recorded where the station
was, so 240 rows of Latvian, Lithuanian and Estonian measurements had no
position — fine until something needed to know what the radar was painting
over the gauge. Cheap and idempotent; run it whenever a network adds a site.
"""
import logging
import sys

sys.path.insert(0, ".")
from wxfusion import db  # noqa: E402
from wxfusion.ingest import stations  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> int:
    conn = db.connect()
    n = stations.store(conn)
    conn.close()
    print(f"{n} stations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
