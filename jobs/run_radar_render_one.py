"""Re-render a single radar hour with the current cleaning and store it.

Set RADAR_RENDER_TS to the hour to redraw (UTC, e.g. "2026-08-11T17:00"). It
force-renders that one composite through the full clean+beam-removal path and
updates only that hour's archive entry, so the page shows the fresh frame there
and nothing else is touched.
"""
import os
import sys
import logging

sys.path.insert(0, ".")
from wxfusion import scrape_maps  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> None:
    raw = os.environ.get("RADAR_RENDER_TS", "").strip()
    if not raw:
        raise SystemExit("set RADAR_RENDER_TS, e.g. 2026-08-11T17:00 "
                         "(comma-separate several)")
    # Sequential in one process, so each re-render reads the archive the last
    # one wrote — no lost updates across the several hours.
    for ts in [t.strip() for t in raw.split(",") if t.strip()]:
        scrape_maps.radar_render_one(ts)


if __name__ == "__main__":
    main()
