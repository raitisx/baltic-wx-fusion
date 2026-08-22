"""Backfill past SMHI (SE) + FMI (FI) sweeps into the radar source store.

One warped level png per feed per 15-min slot; source GeoTIFFs are never
stored. Safe to re-run — already-covered slots are skipped. Follow with
run_radar_render.py RADAR_RENDER_FORCE=1 so the archived composites pick
up the new layers.
"""
import logging
import os
import sys

sys.path.insert(0, ".")

from wxfusion import radar_nordic, radar_store  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(levelname)s %(name)s: %(message)s")
SE_DAYS = int(os.environ.get("NORDIC_SE_DAYS", "14"))
FI_DAYS = int(os.environ.get("NORDIC_FI_DAYS", "7"))
SE_OVERWRITE = os.environ.get("NORDIC_SE_OVERWRITE", "0") == "1"

if __name__ == "__main__":
    s3 = radar_store.r2_client()
    radar_nordic.reindex(s3)   # heal any cancelled-run orphans first
    n = radar_nordic.backfill(s3, se_days=SE_DAYS, fi_days=FI_DAYS,
                              se_overwrite=SE_OVERWRITE)
    print(f"{n} frames backfilled")
