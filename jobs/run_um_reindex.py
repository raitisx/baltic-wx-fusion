"""Tidy the UM frame store, then rebuild its index from what is in the bucket.

Two steps, in this order:

  1. purge frames left under the old one-hour-late labelling. They are exact
     duplicates of correctly labelled frames, so nothing is lost, but while
     they sit in the bucket the index offers a copy that is an hour wrong.
  2. rebuild maps/um4/archive.json from bucket contents. Safe to re-run: it
     derives the whole index rather than amending it.

The index itself was introduced after frames had been accumulating, which is
why a rebuild is needed at all — earlier runs' PNGs were stranded.
"""
import logging
import sys

sys.path.insert(0, ".")
from wxfusion import scrape_maps  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> int:
    scrape_maps.um_purge_stale("um4")
    scrape_maps.um_reindex("um4")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
