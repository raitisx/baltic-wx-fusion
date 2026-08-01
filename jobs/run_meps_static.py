"""Build the fixed assets the edge renderer draws with.

The reprojection from MEPS's grid to ours and the border outline never change,
so they are computed once here and read by the Deno function, which has neither
pyproj nor a way to rasterise a polyline. Re-run only if the target grid or the
border file changes.
"""
import logging
import sys

sys.path.insert(0, ".")
from wxfusion import meps_grid  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> int:
    meps_grid.build_static()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
