"""Nightly verification: compare, rank, snapshot, prune.

The cycle the whole weighting rests on:

  1. pair every past forecast hour against what actually happened
  2. total each day into wx.pair_stats_daily, and send the rows to R2
  3. snapshot today's ranking, so it survives the data it came from

The comparisons no longer accumulate in Postgres. They were 574 MB of it —
245,000 rows a day, read only by a view taking thirty-day averages, and
averages add. The daily totals give the same figures from about 5,000 rows a
month, and the rows themselves are better off in R2 where a day costs a few
megabytes and can still be read with DuckDB.

Forecasts themselves are pruned separately in run_forecasts, on a much shorter
window — they are bulky and disposable once scored. The comparisons are small
and are the valuable residue: keeping two months of them means the ranking can
be recomputed a different way later (weight the flying season more heavily,
judge cloud base by whether it crossed the limit rather than by average error)
without waiting two months for fresh data.
"""
import logging
import os
import sys

sys.path.insert(0, ".")
from wxfusion import db, pairing, pairing_r2  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("run_pairing")

PAIR_DAYS = int(os.environ.get("PAIR_WINDOW_DAYS", "3"))
PAIR_KEEP_DAYS = int(os.environ.get("PAIR_KEEP_DAYS", "60"))
# Where the forecast side comes from. 'r2' reads the archive via DuckDB and does
# not touch forecasts_h, which is what lets the hot table be dropped; 'hot' is
# the original path against forecasts_h, kept as a lever. Proven identical to
# 1e-11 by the pairing-r2 parity check before the switch.
PAIR_SOURCE = os.environ.get("PAIR_FCST_SOURCE", "r2")


def main() -> int:
    conn = db.connect()
    if PAIR_SOURCE == "hot":
        pairing.run(conn, days=PAIR_DAYS)
    else:
        pairing_r2.run(conn, days=PAIR_DAYS)

    # Snapshot before pruning: the ranking has to outlive its evidence.
    try:
        with conn.cursor() as cur:
            cur.execute("select public.wx_snapshot_skill()")
            log.info("skill snapshot: %d rows", cur.fetchone()[0])
        conn.commit()
    except Exception:
        conn.rollback()
        log.exception("skill snapshot failed")

    # Rebuild the weights the page reads. It used to aggregate the whole
    # comparison table live on every load — 55 MB scanned to produce 15 kB of
    # JSON — which was fine warm and a 500 whenever the table was busy or the
    # cache was cold, because anonymous requests are cut off at three seconds.
    # Weights move over days, so a snapshot refreshed here is not a compromise.
    try:
        with conn.cursor() as cur:
            cur.execute("select public.wx_refresh_weights()")
            log.info("weights refreshed: %s", cur.fetchone()[0])
        conn.commit()
    except Exception:
        conn.rollback()
        log.exception("weight refresh failed")

    # Vestigial: pairing stopped writing to that table, so on a migrated
    # database this finds nothing. Left in place so a run against a database
    # still holding old rows tidies them, and harmless once it is empty.
    try:
        with conn.cursor() as cur:
            cur.execute("select public.wx_prune_pairs(%s)", (PAIR_KEEP_DAYS,))
            n = cur.fetchone()[0]
            if n:
                log.info("pruned %d leftover comparisons older than %d days",
                         n, PAIR_KEEP_DAYS)
        conn.commit()
    except Exception:
        conn.rollback()
        log.exception("pair prune failed")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
