"""Read-only radar beam-detector diagnosis.

Set RADAR_DIAG_TS to the frame to examine (UTC, e.g. "2026-08-11T17:00") and
run. It fetches that frame's source overlays from meteolapa and logs, per feed,
whether each bearing tripped the beam thresholds — so a missed beam shows up as
a near-miss table rather than silence. Writes nothing to R2 or the archive.
"""
import os
import logging

from wxfusion import scrape_maps

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> None:
    ts = os.environ.get("RADAR_DIAG_TS", "").strip()
    if not ts:
        raise SystemExit("set RADAR_DIAG_TS, e.g. 2026-08-11T17:00")
    os.environ.setdefault("RADAR_BEAM_DEBUG", "1")
    scrape_maps.beam_diag(ts)


if __name__ == "__main__":
    main()
