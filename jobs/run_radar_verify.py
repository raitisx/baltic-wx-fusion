"""Sample the radar archive at every forecast point and store it as truth.

Rain gauges are sparse and answer "did it rain on this instrument"; the radar
answers "did it rain over this cell", which is the question a 25 km forecast
is actually making. Feeding it into wx.observations_h as source 'radar' lets
the existing pairing job score precipitation everywhere rather than at the
dozen sites that happen to have a gauge.

Idempotent: observations upsert on (source, station_id, time).
"""
import logging
import os
import sys

sys.path.insert(0, ".")
from wxfusion import db, radar_verify  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("run_radar_verify")


def main() -> int:
    hours = int(os.environ.get("RADAR_VERIFY_HOURS", "48"))
    conn = db.connect()
    points = db.get_points(conn)
    rows = radar_verify.observations(points, hours_back=hours)
    if rows:
        db.upsert_observations(conn, rows)
    else:
        log.warning("no radar observations produced")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
