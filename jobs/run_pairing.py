"""Nightly verification: compare, rank, snapshot, prune.

The cycle the whole weighting rests on:

  1. pair every past forecast hour against what actually happened
  2. snapshot today's ranking, so it survives the data it came from
  3. drop comparisons older than the retention window

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
from wxfusion import db, pairing  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("run_pairing")

PAIR_DAYS = int(os.environ.get("PAIR_WINDOW_DAYS", "3"))
PAIR_KEEP_DAYS = int(os.environ.get("PAIR_KEEP_DAYS", "60"))


def main() -> int:
    conn = db.connect()
    pairing.run(conn, days=PAIR_DAYS)

    # Snapshot before pruning: the ranking has to outlive its evidence.
    try:
        with conn.cursor() as cur:
            cur.execute("select public.wx_snapshot_skill()")
            log.info("skill snapshot: %d rows", cur.fetchone()[0])
        conn.commit()
    except Exception:
        conn.rollback()
        log.exception("skill snapshot failed")

    try:
        with conn.cursor() as cur:
            cur.execute("select public.wx_prune_pairs(%s)", (PAIR_KEEP_DAYS,))
            log.info("pruned %d comparisons older than %d days",
                     cur.fetchone()[0], PAIR_KEEP_DAYS)
        conn.commit()
    except Exception:
        conn.rollback()
        log.exception("pair prune failed")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
