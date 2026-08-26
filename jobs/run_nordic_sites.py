"""Per-radar SE/FI frames for one past UTC day (docs/radar_feeds.html).

The single radars have only been fetched live since 23.08.2026; this fills
an earlier day in from the two archives so its antenna rows are not empty.
"""
import datetime as dt
import logging
import os
import sys

sys.path.insert(0, ".")

from wxfusion import radar_nordic, radar_store  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(levelname)s %(name)s: %(message)s")
DAY = os.environ.get("NORDIC_SITES_DAY") or None
FI = os.environ.get("NORDIC_SITES_FI", "1") != "0"
SE = os.environ.get("NORDIC_SITES_SE", "0") != "0"
OVERWRITE = os.environ.get("NORDIC_SITES_OVERWRITE") == "1"

if __name__ == "__main__":
    if not DAY:
        raise SystemExit("set NORDIC_SITES_DAY=YYYYMMDD (UTC)")
    day = dt.datetime.strptime(DAY, "%Y%m%d").date()
    n = radar_nordic.backfill_sites(radar_store.r2_client(), day,
                                    fi=FI, se=SE, overwrite=OVERWRITE)
    print(f"{n} per-radar frames stored for {day} "
          f"(FI={'on' if FI else 'off'}, SE={'on' if SE else 'off'})")
