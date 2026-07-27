"""Radar composite ingest (EXPERIMENTAL — see wxfusion/radar_opera.py)."""
import logging
import sys

sys.path.insert(0, ".")
from wxfusion import radar_opera  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> int:
    radar_opera.archive_composite_series()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
