"""One-off: move the comparisons already in Postgres out to R2, then free them.

Everything new is written straight to R2 by the pairing job. This is for the
2.69 million rows that accumulated before that changed.

A day at a time, and the order matters: write the Parquet file, confirm it is
in the bucket, and only then delete that day's rows. A comparison can always be
rebuilt from a forecast and an observation, but only while both are still in
their own retention windows, so it is not worth relying on.

Env:
  PAIRS_ARCHIVE_MAX   days per pass (default 40), so a long backlog catches up
                      across a few runs instead of overrunning the job timeout
  PAIRS_PRUNE         "0" to write the files without deleting anything
"""
import datetime as dt
import logging
import os
import sys

sys.path.insert(0, ".")
from wxfusion import archive, config, db  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("run_pairs_archive")

COLS = ("model, run_time, valid_time, lead_h, lead_bin, "
        "point_id, parameter, forecast_val, observed_val, error")


def main() -> int:
    max_days = int(os.environ.get("PAIRS_ARCHIVE_MAX", "40"))
    prune = os.environ.get("PAIRS_PRUNE", "1") == "1"
    conn = db.connect()

    with conn.cursor() as cur:
        cur.execute("select distinct valid_time::date from wx.forecast_obs_pairs "
                    "order by 1")
        days = [r[0] for r in cur.fetchall()]
    if not days:
        log.info("nothing left to archive")
        return 0
    log.info("%d days to archive, doing up to %d", len(days), max_days)

    done = 0
    for day in days[:max_days]:
        with conn.cursor() as cur:
            cur.execute(f"select {COLS} from wx.forecast_obs_pairs "
                        "where valid_time >= %s and valid_time < %s "
                        "order by valid_time",
                        (day, day + dt.timedelta(days=1)))
            rows = cur.fetchall()
        if not rows:
            continue
        key = archive.write_pairs_day(rows, day)
        if not key:
            log.warning("%s produced no file; leaving the rows alone", day)
            continue
        # Confirm before deleting. put_object having returned is not the same
        # as the object being listable, and this is the only copy.
        head = archive.r2_client().head_object(Bucket=config.R2_BUCKET, Key=key)
        if head["ContentLength"] < 1:
            log.warning("%s uploaded empty; leaving the rows alone", day)
            continue
        if prune:
            with conn.cursor() as cur:
                cur.execute("delete from wx.forecast_obs_pairs "
                            "where valid_time >= %s and valid_time < %s",
                            (day, day + dt.timedelta(days=1)))
                log.info("%s: %d rows archived and removed", day, cur.rowcount)
            conn.commit()
        done += 1

    if prune and done:
        # Postgres does not give the disk back on delete — the space is marked
        # reusable and the file stays the size it was. This is the step that
        # actually shrinks the database, and it locks the table while it runs.
        with conn.cursor() as cur:
            cur.execute("commit")           # VACUUM FULL cannot run in a block
            cur.execute("vacuum full wx.forecast_obs_pairs")
        log.info("vacuumed; space returned")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
