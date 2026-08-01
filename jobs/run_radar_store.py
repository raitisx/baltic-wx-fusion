"""Keep the radar sources as served.

Runs on the hourly tick. Cheap — three fetches an hour, 256 kB — and it is
what makes a styling change cost seconds instead of a scrape of the whole
archive.
"""
import logging, os, sys
sys.path.insert(0, ".")
from wxfusion import radar_store  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
HOURS = int(os.environ.get("RADAR_STORE_HOURS", "6"))


def main() -> int:
    n = radar_store.store_sources(hours_back=HOURS)
    print(f"{n} source overlays stored")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
