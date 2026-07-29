"""GFS map frames for the part of the week MEPS cannot reach.

MEPS stops at +66 h, so days 3-7 of the week view had no map. GFS carries a
gridded cloud ceiling to +384 h, which is the same quantity the meteograms
plot, so the long-range frames mean what the short-range ones do.
"""
import logging
import os
import sys

sys.path.insert(0, ".")
from wxfusion import maps_gfs  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> int:
    maps_gfs.render_run(
        start_lead=int(os.environ.get("GFS_START_LEAD", "36")),
        end_lead=int(os.environ.get("GFS_END_LEAD", "168")),
        step=int(os.environ.get("GFS_STEP", "6")),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
