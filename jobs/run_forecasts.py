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

# Forecasts only need to survive in Postgres until the hour they describe has
# passed and been scored. Five days covers the longest lead the ingest asks
# for (four) plus a day of slack, and every run is in R2 for good.
#
# What this costs: the meteogram browses two weeks back, and past hours with
# no row here fall back to an Open-Meteo hindcast — today's models describing
# last Tuesday rather than what was actually forecast then. Good enough to
# look at, not evidence. The evidence is in R2 and reading it from the page
# would need an edge function.
#
# Do not lower this without checking the archive is complete first. On 07.08
# it was not: 36 of 64 runs had no file, because the writer named one file
# after one of the five run times an ingest carries. See the note above the
# archive call below.
KEEP_DAYS = int(os.environ.get("FORECAST_KEEP_DAYS", "5"))


def main() -> int:
    conn = db.connect()
    points = db.get_points(conn)
    rows = openmeteo.fetch(points)
    db.upsert_forecasts(conn, rows)

    # One file per RUN, not one per ingest. This used to take the newest
    # run_time across all five models and write everything under that single
    # key — but the models run on different cycles (MEPS 3-hourly, the rest
    # 6-hourly), so an ingest carries several run times and only one of them
    # ever got a file. Measured on 07.08: 36 of the 64 runs sitting in
    # Postgres had no object in R2 at all, and the rows were inside a
    # neighbouring file under a name nothing would look for. Pruning the hot
    # tier against that archive would have lost them.
    by_run: dict = {}
    for r in rows:
        by_run.setdefault(r[1], []).append(r)
    for run_time, run_rows in sorted(by_run.items()):
        try:
            archive.write_run(run_rows, run_time)
        except Exception:
            log.exception("archive write failed for %s (hot tier is still updated)",
                          run_time)

    try:
        db.prune_forecasts(conn, KEEP_DAYS)
    except Exception:
        log.exception("prune failed")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
