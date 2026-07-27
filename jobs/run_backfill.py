"""One-off historical backfill into wx.observations. Idempotent — safe to re-run.

Sources & depth:
  --lvgmc            LVGMC archive resource (trailing ~365 days, data.gov.lv)
  --metar-start/-end IEM METAR archive (years of history; fetched month by month)
  --lt-days N        meteo.lt per-station daily history (up to 10 years exists;
                     mind the 20k req/day API cap: requests = stations x days)

Examples (GitHub Actions inputs map to these flags):
  python jobs/run_backfill.py --lvgmc
  python jobs/run_backfill.py --metar-start 2024-01-01 --metar-end 2026-07-01
  python jobs/run_backfill.py --lt-days 90
"""
import argparse
import datetime as dt
import logging
import sys
import time

sys.path.insert(0, ".")
from wxfusion import db  # noqa: E402
from wxfusion.ingest import lvgmc, metar_iem, meteolt  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("backfill")


def backfill_lvgmc(conn) -> int:
    total = 0
    for batch in lvgmc.fetch_archive_batches():
        total += db.upsert_observations(conn, batch)
    log.info("lvgmc backfill done: %d rows offered", total)
    return total


def backfill_metar(conn, start: dt.date, end: dt.date) -> int:
    stations = [p["station_id"] for p in db.get_points(conn, "metar")]
    total = 0
    cur = dt.datetime(start.year, start.month, start.day, tzinfo=dt.timezone.utc)
    stop = dt.datetime(end.year, end.month, end.day, tzinfo=dt.timezone.utc)
    while cur < stop:
        nxt = min((cur.replace(day=1) + dt.timedelta(days=40)).replace(day=1), stop)
        rows = metar_iem.fetch(stations, cur, nxt)
        total += db.upsert_observations(conn, rows)
        log.info("metar backfill %s..%s: %d rows", cur.date(), nxt.date(), len(rows))
        cur = nxt
    return total


def backfill_lt(conn, days: int) -> int:
    stations = [s["code"] for s in meteolt.list_stations()]
    n_req = len(stations) * days
    if n_req > 18000:
        raise SystemExit(
            f"{n_req} requests would exceed the meteo.lt daily cap (20k). "
            "Lower --lt-days or run across multiple days."
        )
    total = 0
    today = dt.date.today()
    for d_off in range(1, days + 1):
        date = today - dt.timedelta(days=d_off)
        day_rows: list[tuple] = []
        for code in stations:
            try:
                day_rows.extend(meteolt.fetch_date(code, date))
            except Exception:
                log.warning("meteo.lt %s %s failed", code, date)
            time.sleep(0.4)  # stay far below 180 req/min
        total += db.upsert_observations(conn, day_rows)
        log.info("meteo.lt backfill %s: %d rows", date, len(day_rows))
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lvgmc", action="store_true", help="LVGMC ~365d archive")
    ap.add_argument("--metar-start", type=dt.date.fromisoformat)
    ap.add_argument("--metar-end", type=dt.date.fromisoformat)
    ap.add_argument("--lt-days", type=int, default=0)
    args = ap.parse_args()

    conn = db.connect()
    if args.lvgmc:
        backfill_lvgmc(conn)
    if args.metar_start:
        backfill_metar(conn, args.metar_start,
                       args.metar_end or dt.date.today())
    if args.lt_days:
        backfill_lt(conn, args.lt_days)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
