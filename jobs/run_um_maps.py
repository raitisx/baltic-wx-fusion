"""3-hourly scraped UM (meteo.pl) cloud/precip map frames. Advisory layer —
respects the politeness rules in wxfusion/scrape_maps.py."""
import logging
import sys

sys.path.insert(0, ".")
from wxfusion import scrape_maps  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> int:
    scrape_maps.um_run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
