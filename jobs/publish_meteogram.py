"""Publish per-point meteogram history JSON to R2.

Phase 1 of moving the historical meteogram off Postgres. Purely additive: it
reads wx.forecasts_h and wx.observations_h and writes one small JSON file per
point to the public bucket, in the exact shape wx_point_window and
wx_point_obs_window return. Nothing reads these files until the page is pointed
at them; nothing in Postgres or R2 is changed or deleted.

Runs every models pass (twice daily) and is also a backfill target for an
isolated on-demand run.

Env:
  SUPABASE_DB_URL        Postgres (read-only use here)
  R2_*                   bucket credentials
  METEOGRAM_KEEP_DAYS    rolling window published per point (default 21)
  METEOGRAM_HOT_DAYS     how far back to read the hot tables (default = keep)
  METEOGRAM_POINTS       optional comma list, to verify a handful of points
  METEOGRAM_VERIFY       "1" to fetch published files over the public URL and
                         confirm they equal the page's RPCs (default off; on in
                         the models pass). Skipped when METEOGRAM_POINTS limits
                         the run, since the sample may not have been written.
  METEOGRAM_PUBKEY       Supabase publishable key for the verify RPC calls
"""
import logging
import os
import sys

sys.path.insert(0, ".")
from wxfusion import db, meteogram_r2  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("publish_meteogram")

# The page ships this same publishable (anon) key in the clear; it only grants
# what the browser already has. A real value keeps the verify self-contained.
DEFAULT_PUBKEY = "sb_publishable_rzlQRVBrD-u8RhNRbX3yEw_5UtRy6j-"


def main() -> int:
    keep = int(os.environ.get("METEOGRAM_KEEP_DAYS", str(meteogram_r2.KEEP_DAYS)))
    hot_env = os.environ.get("METEOGRAM_HOT_DAYS", "")
    hot = int(hot_env) if hot_env else None
    only = [p.strip() for p in os.environ.get("METEOGRAM_POINTS", "").split(",")
            if p.strip()]

    conn = db.connect()
    try:
        meteogram_r2.publish(conn, keep_days=keep, hot_days=hot, only=only or None)
        # Verify the browser's exact read path (public R2 file == live RPC). Not
        # meaningful when only a subset was written, so skip if points limited.
        if os.environ.get("METEOGRAM_VERIFY") == "1" and not only:
            pubkey = os.environ.get("METEOGRAM_PUBKEY", DEFAULT_PUBKEY)
            meteogram_r2.verify(conn, pubkey)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
