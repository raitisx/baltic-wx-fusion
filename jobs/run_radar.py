"""Radar jobs: OPERA point-series archive + meteolapa composite for the map viewer."""
import logging
import sys

sys.path.insert(0, ".")
from wxfusion import radar_opera, scrape_maps  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("run_radar")


def main() -> int:
    ok = 0
    try:
        scrape_maps.radar_composite()
        ok += 1
    except Exception:
        log.exception("meteolapa radar composite failed")
    try:
        radar_opera.archive_composite_series()
        ok += 1
    except Exception:
        log.exception("OPERA series archive failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
