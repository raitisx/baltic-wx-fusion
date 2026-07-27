"""MEPS backfill: near-analysis maps for already-passed hours from past runs
still on thredds (~last day). Idempotent."""
import logging
import os
import sys

sys.path.insert(0, ".")
from wxfusion import maps_meps  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> int:
    maps_meps.backfill_runs(max_runs=int(os.environ.get("MEPS_RUNS", "12")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
