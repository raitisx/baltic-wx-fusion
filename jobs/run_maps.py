"""3-hourly MEPS flight-map rendering job."""
import logging
import sys

sys.path.insert(0, ".")
from wxfusion import maps_meps  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> int:
    maps_meps.render_run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
