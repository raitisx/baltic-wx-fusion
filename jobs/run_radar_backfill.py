"""One-off/occasional radar backfill: hourly composites as far back as the
sources still serve (~24-48 h). Idempotent."""
import logging
import os
import sys

sys.path.insert(0, ".")
from wxfusion import scrape_maps  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> int:
    hours = int(os.environ.get("BACKFILL_HOURS", "168"))
    scrape_maps.radar_backfill(hours_back=hours)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
