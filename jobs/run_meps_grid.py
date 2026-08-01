"""Fill and roll the MEPS grid archive.

    GRID_FROM / GRID_TO   YYYY-MM-DD, inclusive — fetch that range from MET's
                          long-term archive into R2
    GRID_DAYS             with no explicit range, keep the last N days topped
                          up from the archive (default 2)
    GRID_RENDER_DAYS      after fetching, re-render this many recent days into
                          frames (0 = none)
    GRID_PRUNE            "1" to roll the three retention windows forward

A year is about 4.5 hours of pure OPeNDAP reading, so it has to be run in
chunks — a month at a time is comfortable inside a job's timeout. Days already
present are skipped, so a re-run picks up where an interrupted one stopped.
"""
import datetime as dt
import logging
import os
import sys

sys.path.insert(0, ".")
from wxfusion import maps_meps, meps_grid  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("run_meps_grid")


def _day(name: str):
    v = os.environ.get(name, "").strip()
    return dt.date.fromisoformat(v) if v else None


def main() -> int:
    d0, d1 = _day("GRID_FROM"), _day("GRID_TO")
    if not (d0 and d1):
        # Routine pass: top up the last couple of days. The MET archive lags
        # the live catalog by a few hours, so yesterday is always complete
        # there and today is partial — checking both costs nothing because
        # hours already stored are skipped.
        back = int(os.environ.get("GRID_DAYS", "2"))
        today = dt.datetime.now(dt.timezone.utc).date()
        d0, d1 = today - dt.timedelta(days=back), today
    if d0 and d1:
        day = d0
        while day <= d1:
            urls = maps_meps.archive_runs(day)
            if not urls:
                log.warning("%s: not in the MET archive", day)
            for u in urls:
                run_h = int(u[-6:-4])
                hours = [dt.datetime.combine(day, dt.time(0), dt.timezone.utc)
                         + dt.timedelta(hours=run_h + k) for k in (1, 2, 3)]
                if all(meps_grid.have_hour(h) for h in hours):
                    continue
                try:
                    meps_grid.store_run(u)
                except Exception:
                    log.exception("%s failed", u.rsplit("/", 1)[-1])
            log.info("%s done", day)
            day += dt.timedelta(days=1)

    n = int(os.environ.get("GRID_RENDER_DAYS", "0"))
    if n > 0:
        now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0,
                                                       microsecond=0)
        drawn = 0
        for back in range(n * 24, 0, -1):
            hour = now - dt.timedelta(hours=back)
            if meps_grid.render_hour(hour):
                drawn += 1
        log.info("rendered %d hours from the grid", drawn)

    if os.environ.get("GRID_PRUNE", "") == "1":
        meps_grid.prune()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
