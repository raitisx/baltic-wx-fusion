"""Background fill-grid downloader for the fused map.

Turns the free-tier headroom the station ingest leaves into an even field: a
grid of points laid where no station sits (wxfusion.grid_fill), fetched from
Open-Meteo for the nine fields the map draws, and parked in R2 as one Parquet
object the map render reads. Deliberately NOT in Postgres and NOT in
wx.points — a grid cell has no observation to score, so it must stay out of
both the station ingest (which fetches every active point at full 13-var cost,
twice a day) and the verification pipeline.

Runs ONCE a day, in its OWN hour. The station ingest already spends ~2,600
weighted Open-Meteo calls in each of its two hourly runs; this pass spends
~3,900 more, and 5,000/hour is the ceiling, so the two must not share an hour.
The day total lands near 9,200 of the 10,000/day cap — see grid_fill.MAX_POINTS.

Env:
  GRID_FILL_MAX_POINTS  cap on fill points (default 900 -> ~869 at 0.22 deg)
  GRID_FILL_MIN_KM      suppress fill within this many km of a station (12)
"""
import datetime as dt
import io
import logging
import sys

sys.path.insert(0, ".")
from wxfusion import archive, config, db, grid_fill  # noqa: E402
from wxfusion.ingest import openmeteo  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("run_grid_fill")

LATEST_KEY = "maps/grid_forecast/latest.parquet"


def _run_key(run_time: dt.datetime) -> str:
    r = run_time.astimezone(dt.timezone.utc)
    return f"maps/grid_forecast/{r:%Y%m%dT%H%M}.parquet"


def _write_parquet(rows: list[tuple]) -> int:
    """Pivot the flat forecast rows to the archive schema and put them in R2,
    both under a run-stamped key and the stable latest.parquet the map reads."""
    import pyarrow.parquet as pq

    table = archive._table(rows)
    if table is None:
        log.warning("grid fill: no rows to write")
        return 0
    buf = io.BytesIO()
    pq.write_table(
        table, buf, compression="zstd", compression_level=10,
        use_dictionary=["model", "point_id"], version="2.6",
    )
    body = buf.getvalue()
    s3 = archive.r2_client()
    now = dt.datetime.now(dt.timezone.utc)
    for key in (_run_key(now), LATEST_KEY):
        s3.put_object(
            Bucket=config.R2_BUCKET, Key=key, Body=body,
            ContentType="application/vnd.apache.parquet",
            # latest.parquet turns over daily; let the map render pick it up
            # without a week of CDN caching.
            CacheControl="public, max-age=3600",
        )
    log.info("grid fill: wrote %d rows -> %s (%.0f kB)",
             table.num_rows, LATEST_KEY, len(body) / 1024)
    return table.num_rows


def main() -> int:
    conn = db.connect()
    # Suppress fill near ANY active point — real stations and the old coarse
    # grid alike already carry those cells; the fill is only for the gaps.
    stations = db.get_points(conn)
    conn.close()

    pts = grid_fill.build_fill_points(stations)
    # A visible budget line: points x (9 vars x 5 models / 10), one pass.
    est = len(pts) * len(openmeteo.MAP_HOURLY_VARS) * len(config.OPENMETEO_MODELS) / 10.0
    log.info("grid fill: %d points, ~%.0f weighted Open-Meteo calls this pass",
             len(pts), est)

    rows = openmeteo.fetch(pts, hourly_vars=openmeteo.MAP_HOURLY_VARS)
    if not rows:
        log.error("grid fill: Open-Meteo returned nothing — leaving last map data in place")
        return 1
    _write_parquet(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
