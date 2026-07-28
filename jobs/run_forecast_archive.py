"""One-off / catch-up: export every run still in the hot table to R2 Parquet.

Normal ingest archives each run as it lands. This exists for the initial
migration and for filling gaps if the archive write failed on some run.
Idempotent — re-writing a run's file just overwrites identical content.
"""
import logging
import os
import sys

sys.path.insert(0, ".")
from wxfusion import archive, db  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("run_forecast_archive")

PARAMS = archive.PARAMS
COLS = ", ".join(PARAMS)


def main() -> int:
    only_missing = os.environ.get("ARCHIVE_ONLY_MISSING", "0") == "1"
    conn = db.connect()
    with conn.cursor() as cur:
        cur.execute("select distinct run_time from wx.forecasts_h order by run_time")
        runs = [r[0] for r in cur.fetchall()]
    log.info("archiving %d runs", len(runs))

    existing: set[str] = set()
    if only_missing:
        c = archive.r2_client()
        token = None
        while True:
            kw = {"Bucket": os.environ.get("R2_BUCKET", "baltic-wx"),
                  "Prefix": "archive/forecasts/"}
            if token:
                kw["ContinuationToken"] = token
            resp = c.list_objects_v2(**kw)
            existing.update(o["Key"] for o in resp.get("Contents", []))
            token = resp.get("NextContinuationToken")
            if not resp.get("IsTruncated"):
                break

    written = 0
    for run in runs:
        if only_missing and archive.key_for(run) in existing:
            continue
        with conn.cursor() as cur:
            cur.execute(
                f"select model, run_time, valid_time, point_id, {COLS} "
                "from wx.forecasts_h where run_time = %s",
                (run,),
            )
            rows = []
            for rec in cur.fetchall():
                model, run_time, valid_time, point_id = rec[:4]
                for name, value in zip(PARAMS, rec[4:]):
                    if value is not None:
                        rows.append((model, run_time, valid_time, point_id, name, float(value)))
        if not rows:
            continue
        archive.write_run(rows, run)
        written += 1

    conn.close()
    log.info("archived %d runs", written)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
