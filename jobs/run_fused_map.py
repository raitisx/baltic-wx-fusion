"""Render the 1:1 fused map — the meteogram's blend, on the grid.

Runs after the fill grid is refreshed (same daily workflow), reads the station
forecasts + fill grid + skill weights + static geography, and writes the fused
map frames to R2 under maps/fused/. See wxfusion/fused_map.py.
"""
import logging
import sys

sys.path.insert(0, ".")
from wxfusion import fused_map  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("run_fused_map")


def main() -> int:
    fused_map.render_run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
