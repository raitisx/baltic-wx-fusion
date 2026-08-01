"""MEPS backfill: near-analysis maps for hours that have already passed.

Two sources, and the second one is the reason history can be re-rendered at
all. `mepslatest` holds roughly the last day of runs — fine for repairing a
missed pass. `meps25epsarchive` holds years, one directory per day, and its
deterministic files carry every field we draw. Set MEPS_DAYS to walk back
through it; leave it at 0 for the ordinary same-day repair.

Each run contributes only its first few lead hours, so an hour is always drawn
from the freshest run that covers it. MEPS_FORCE (via BACKFILL_FORCE) is what
makes a re-render of already-archived hours actually happen.
"""
import datetime as dt
import logging
import os
import sys

sys.path.insert(0, ".")
from wxfusion import maps_meps  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("run_meps_backfill")

DAYS = int(os.environ.get("MEPS_DAYS", "0"))
RUNS = int(os.environ.get("MEPS_RUNS", "12"))


def main() -> int:
    if DAYS <= 0:
        maps_meps.backfill_runs(max_runs=RUNS)
        return 0

    today = dt.datetime.now(dt.timezone.utc).date()
    for back in range(DAYS, 0, -1):
        day = today - dt.timedelta(days=back)
        urls = maps_meps.archive_runs(day)
        if not urls:
            log.warning("%s: nothing in the MEPS archive", day)
            continue
        log.info("%s: %d archived runs", day, len(urls))
        maps_meps.backfill_runs(datasets=urls)
    # finish with whatever mepslatest still has, for today's hours
    maps_meps.backfill_runs(max_runs=RUNS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
