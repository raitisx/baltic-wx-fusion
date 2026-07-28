"""Forecast ingest: Open-Meteo multi-model extracts at all points.

Two destinations, on purpose:
  * wx.forecasts_h  — pivoted hot tier, rolling 60 days, what the page and the
                      pairing job query.
  * R2 Parquet      — every run, kept for a year plus, what old "why we didn't
                      fly" reports read from.

The archive write is best-effort: losing it must not cost us the ingest.
"""
import logging
import os
import sys

sys.path.insert(0, ".")
from wxfusion import archive, db  # noqa: E402
from wxfusion.ingest import openmeteo  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("run_forecasts")

KEEP_DAYS = int(os.environ.get("FORECAST_KEEP_DAYS", "60"))


def main() -> int:
    conn = db.connect()
    points = db.get_points(conn)
    rows = openmeteo.fetch(points)
    db.upsert_forecasts(conn, rows)

    run_time = max((r[1] for r in rows), default=None)
    if run_time is not None:
        try:
            archive.write_run(rows, run_time)
        except Exception:
            log.exception("archive write failed (hot tier is still updated)")

    try:
        db.prune_forecasts(conn, KEEP_DAYS)
    except Exception:
        log.exception("prune failed")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
