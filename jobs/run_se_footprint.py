"""Rebuild the SE coverage footprint from a window of sweeps.

SMHI's "no data" value is per sweep, not a fixed footprint, so the map the
composite fades against has to be the UNION of what the product covers —
otherwise one sweep with radars down clips every render along its outline.
"""
import logging
import os
import sys

sys.path.insert(0, ".")

from wxfusion import radar_nordic, radar_store  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(levelname)s %(name)s: %(message)s")
HOURS = float(os.environ.get("SE_FOOTPRINT_HOURS", "24"))

if __name__ == "__main__":
    n = radar_nordic.rebuild_edge_map(radar_store.r2_client(), hours=HOURS)
    print(f"SE footprint: {n:,} px covered")
