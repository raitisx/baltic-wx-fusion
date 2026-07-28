"""Archive observation days to R2 Parquet, then prune the hot tier.

One file per UTC day under archive/observations/YYYY/MM/. Recent days are
rewritten each pass because they are still filling in; older days are written
once. Pruning only ever touches days whose Parquet file is confirmed present
in R2 — the Estonian feed and the LVGMC operative endpoint are snapshot-only
upstream, so a deleted observation cannot be re-fetched.

Env:
  OBS_KEEP_DAYS      rolling window kept in Postgres (default 90)
  OBS_ARCHIVE_DAYS   how many recent days to (re)write (default 3)
  OBS_ARCHIVE_MAX    cap on backlog days written per pass (default 40), so the
                     first run over a year of history catches up across a few
                     passes instead of overrunning the job timeout
  OBS_ARCHIVE_ALL    "1" to write every day present in the table, no cap
  OBS_PRUNE          "0" to archive without deleting anything
"""
import datetime as dt
import logging
import os
import sys

sys.path.insert(0, ".")
from wxfusion import archive, db  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("run_obs_archive")

PARAMS = archive.OBS_PARAMS
COLS = ", ".join(PARAMS)


def days_present(conn) -> list[dt.date]:
    with conn.cursor() as cur:
        cur.execute(
            "select distinct (time at time zone 'UTC')::date as d "
            "from wx.observations_h order by d"
        )
        return [r[0] for r in cur.fetchall()]


def rows_for_day(conn, day: dt.date) -> list[tuple]:
    start = dt.datetime.combine(day, dt.time.min, tzinfo=dt.timezone.utc)
    end = start + dt.timedelta(days=1)
    with conn.cursor() as cur:
        cur.execute(
            f"select source, station_id, time, {COLS} from wx.observations_h "
            "where time >= %s and time < %s",
            (start, end),
        )
        rows = []
        for rec in cur.fetchall():
            source, station_id, time = rec[:3]
            for name, value in zip(PARAMS, rec[3:]):
                if value is not None:
                    rows.append((source, station_id, time, name, float(value)))
    return rows


def main() -> int:
    keep_days = int(os.environ.get("OBS_KEEP_DAYS", "90"))
    recent_days = int(os.environ.get("OBS_ARCHIVE_DAYS", "3"))
    archive_all = os.environ.get("OBS_ARCHIVE_ALL", "0") == "1"
    do_prune = os.environ.get("OBS_PRUNE", "1") == "1"

    conn = db.connect()
    all_days = days_present(conn)
    if not all_days:
        log.info("no observations to archive")
        conn.close()
        return 0

    existing = archive.list_keys("archive/observations/")
    today = dt.datetime.now(dt.timezone.utc).date()
    cutoff = today - dt.timedelta(days=recent_days)

    recent = [d for d in all_days if d >= cutoff]
    backlog = [d for d in all_days if d < cutoff and archive.obs_key_for(d) not in existing]
    if archive_all:
        targets = sorted(set(all_days))
    else:
        cap = int(os.environ.get("OBS_ARCHIVE_MAX", "40"))
        if len(backlog) > cap:
            log.info("backlog %d days, writing the oldest %d this pass", len(backlog), cap)
            backlog = backlog[:cap]
        targets = sorted(set(recent) | set(backlog))
    log.info("%d days present, %d to write", len(all_days), len(targets))

    written = 0
    for day in targets:
        rows = rows_for_day(conn, day)
        if rows and archive.write_observation_day(rows, day):
            existing.add(archive.obs_key_for(day))
            written += 1

    if do_prune:
        archived = {d for d in all_days if archive.obs_key_for(d) in existing}
        db.prune_observations(conn, keep_days, archived)

    conn.close()
    log.info("archived %d days", written)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
