"""Render the per-feed debug frames for one day (docs/radar_feeds.html)."""
import logging
import os
import sys

sys.path.insert(0, ".")

from wxfusion import radar_store  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(levelname)s %(name)s: %(message)s")
DAY = os.environ.get("RADAR_FEEDS_DAY") or None

if __name__ == "__main__":
    if not DAY:
        raise SystemExit("set RADAR_FEEDS_DAY=YYYYMMDD (UTC)")
    n = radar_store.render_feeds(DAY)
    print(f"{n} per-feed frames rendered for {DAY}")
