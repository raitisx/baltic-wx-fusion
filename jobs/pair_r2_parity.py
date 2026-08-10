"""Parity check: does pairing from the R2 archive match pairing from forecasts_h?

Read-only. Computes wx.pair_stats_daily's totals both ways over one window and
one instant — Postgres forecasts_h on one side, the R2 forecast archive via
DuckDB on the other, the observed side identical on both — and asserts they
agree (counts exact, sums within a relative tolerance). Writes nothing.

This is the gate the forecasts_h truncate waits on: only once the archive path
reproduces the skill numbers can pairing be moved off the hot table and the
table dropped.

Env:
  SUPABASE_DB_URL, R2_*   as usual
  PAIR_WINDOW_DAYS        pairing window to check (default 3, matching nightly)
"""
import logging
import os
import sys

sys.path.insert(0, ".")
from wxfusion import db, pairing_r2  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pair_r2_parity")


def main() -> int:
    days = int(os.environ.get("PAIR_WINDOW_DAYS", "3"))
    conn = db.connect()
    try:
        report = pairing_r2.parity(conn, days=days)
    finally:
        conn.close()
    log.info("PARITY OK: %d combinations, %d pairs, worst sum diff %.2e",
             report["pg_combos"], report["pg_total_pairs"], report["worst_rel_diff"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
