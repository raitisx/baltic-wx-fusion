"""Collect gauge-vs-radar colour pairs, and merge them into the running table.

Needs station coordinates, so run_stations has to have run at least once.
Results accumulate in R2 across runs — one pass over a week gets the ordering
and the light end; the heavy end arrives when heavy rain next falls over a
gauge, so this is worth running on a schedule rather than once.
"""
import logging
import os
import sys

sys.path.insert(0, ".")
from wxfusion import db, radar_calibrate  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

HOURS = int(os.environ.get("CAL_HOURS", "168"))


def main() -> int:
    conn = db.connect()
    pairs = radar_calibrate.collect(conn, hours_back=HOURS)
    conn.close()
    summary = radar_calibrate.merge_and_store(pairs)
    for feed, rows in sorted(summary.items()):
        print(f"\n{feed}: {len(rows)} colours")
        for e in rows[-8:]:
            print(f"   {e['rgb']:>14}  n={e['n']:<5} mean {e['mean']:.2f} mm/h  "
                  f"wet {100*e['wet_frac']:.0f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
