"""Rebuild the UM archive index from the frames already in the bucket.

Needed once, because the index was introduced after frames had been
accumulating — earlier runs' PNGs were stranded. Safe to re-run: it derives
the whole index from bucket contents rather than amending it.
"""
import logging
import sys

sys.path.insert(0, ".")
from wxfusion import scrape_maps  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> int:
    scrape_maps.um_reindex("um4")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
