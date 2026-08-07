"""Cold forecast archive: one Parquet file per ingest run, stored in R2.

Postgres keeps a rolling hot window (see wx.forecasts_h and
public.wx_prune_forecasts) because that is what the page and the pairing job
query. Everything ever ingested also lands here, which is what a
"why we didn't fly" report for an old date reads from.

Layout: archive/forecasts/YYYY/MM/YYYYMMDDTHHMM.parquet
Columns: model, point_id, valid_time, then one column per canonical parameter.

Roughly 150-300 kB per run compressed, so a year of twice-daily runs is well
under 200 MB against R2's 10 GB free tier. Parquet rather than a custom binary
blob so the archive stays readable by pandas, DuckDB, or anything else later —
DuckDB can query these directly over HTTP without downloading the year.
"""
from __future__ import annotations

import datetime as dt
import io
import logging

import boto3

from . import config

log = logging.getLogger(__name__)

PARAMS = [
    "t2m", "td2m", "rh", "ws10m", "wg10m", "wdir", "prcp_1h", "pres_msl",
    "cc_low", "cc_mid", "cc_high", "cc_total", "cb_lcl", "cb", "vis",
]

# Observations carry a different set: no split cloud layers, but visibility,
# snow, the METAR rain flag, and cb_ceiling — the only directly observed
# cloud base anywhere in the system.
OBS_PARAMS = [
    "t2m", "td2m", "rh", "ws10m", "wg10m", "wdir", "prcp_1h", "pres_msl",
    "cc_total", "vis", "cb_ceiling", "snow", "wx_rain",
]


def r2_client():
    if not (config.R2_ACCOUNT_ID and config.R2_ACCESS_KEY_ID):
        raise RuntimeError("R2 credentials not configured")
    return boto3.client(
        "s3",
        endpoint_url=f"https://{config.R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=config.R2_ACCESS_KEY_ID,
        aws_secret_access_key=config.R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )


def key_for(run_time: dt.datetime) -> str:
    r = run_time.astimezone(dt.timezone.utc)
    return f"archive/forecasts/{r:%Y/%m}/{r:%Y%m%dT%H%M}.parquet"


def obs_key_for(day: dt.date) -> str:
    return f"archive/observations/{day:%Y/%m}/{day:%Y%m%d}.parquet"


def pairs_key_for(day: dt.date) -> str:
    return f"archive/pairs/{day:%Y/%m}/{day:%Y%m%d}.parquet"


def list_keys(prefix: str) -> set[str]:
    """Every archived object under a prefix — used to make pruning safe."""
    c = r2_client()
    keys: set[str] = set()
    token = None
    while True:
        kw = {"Bucket": config.R2_BUCKET, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        resp = c.list_objects_v2(**kw)
        keys.update(o["Key"] for o in resp.get("Contents", []))
        if not resp.get("IsTruncated"):
            return keys
        token = resp.get("NextContinuationToken")


def _table(rows: list[tuple]):
    """rows: (model, run_time, valid_time, point_id, parameter, value)"""
    import pyarrow as pa

    cells: dict[tuple, dict] = {}
    for model, _run, valid, point_id, parameter, value in rows:
        if parameter not in PARAMS:
            continue
        cells.setdefault((model, point_id, valid), {})[parameter] = value
    if not cells:
        return None

    keys = sorted(cells, key=lambda k: (k[0], k[1], k[2]))
    data = {
        "model": pa.array([k[0] for k in keys], pa.string()),
        "point_id": pa.array([k[1] for k in keys], pa.string()),
        "valid_time": pa.array([k[2] for k in keys], pa.timestamp("s", tz="UTC")),
    }
    for p in PARAMS:
        data[p] = pa.array([cells[k].get(p) for k in keys], pa.float32())
    return pa.table(data)


def write_run(rows: list[tuple], run_time: dt.datetime) -> str | None:
    """Write one ingest run to R2 as Parquet. Returns the key, or None."""
    import pyarrow.parquet as pq

    table = _table(rows)
    if table is None:
        log.warning("archive: nothing to write for run %s", run_time)
        return None

    buf = io.BytesIO()
    pq.write_table(
        table, buf,
        compression="zstd", compression_level=10,
        # model/point_id repeat heavily; dictionary encoding collapses them.
        use_dictionary=["model", "point_id"],
        version="2.6",
    )
    body = buf.getvalue()
    key = key_for(run_time)
    r2_client().put_object(
        Bucket=config.R2_BUCKET, Key=key, Body=body,
        ContentType="application/vnd.apache.parquet",
    )
    log.info("archive: %s (%d rows, %.0f kB)", key, table.num_rows, len(body) / 1024)
    return key


def _obs_table(rows: list[tuple]):
    """rows: (source, station_id, time, parameter, value)"""
    import pyarrow as pa

    cells: dict[tuple, dict] = {}
    for source, station_id, time, parameter, value in rows:
        if parameter not in OBS_PARAMS:
            continue
        cells.setdefault((source, station_id, time), {})[parameter] = value
    if not cells:
        return None

    keys = sorted(cells, key=lambda k: (k[0], k[1], k[2]))
    data = {
        "source": pa.array([k[0] for k in keys], pa.string()),
        "station_id": pa.array([k[1] for k in keys], pa.string()),
        "time": pa.array([k[2] for k in keys], pa.timestamp("s", tz="UTC")),
    }
    for p in OBS_PARAMS:
        data[p] = pa.array([cells[k].get(p) for k in keys], pa.float32())
    return pa.table(data)


def write_observation_day(rows: list[tuple], day: dt.date) -> str | None:
    """Write one UTC day of observations to R2 as Parquet. Returns the key."""
    import pyarrow.parquet as pq

    table = _obs_table(rows)
    if table is None:
        return None

    buf = io.BytesIO()
    pq.write_table(
        table, buf,
        compression="zstd", compression_level=10,
        use_dictionary=["source", "station_id"],
        version="2.6",
    )
    body = buf.getvalue()
    key = obs_key_for(day)
    r2_client().put_object(
        Bucket=config.R2_BUCKET, Key=key, Body=body,
        ContentType="application/vnd.apache.parquet",
    )
    log.info("archive: %s (%d rows, %.0f kB)", key, table.num_rows, len(body) / 1024)
    return key


# Every forecast set beside what actually happened, one file per UTC day.
#
# These used to live in Postgres, where 245,000 rows a day made
# wx.forecast_obs_pairs the largest object in the database — 574 MB, of which
# 271 MB was the primary key index alone. The only thing that ever read them
# was a view computing thirty-day averages, and averages add: the daily totals
# in wx.pair_stats_daily reproduce the skill figures exactly (checked against
# the live view over 300 model/parameter/lead combinations — counts identical,
# bias, MAE and RMSE agreeing to 7e-13, which is float rounding).
#
# So the totals stay in Postgres and the rows come here, where they are worth
# keeping: a day of them is a few hundred kilobytes compressed, and they are
# what you would read to ask why a particular model missed a particular
# morning at a particular station.
PAIR_COLS = ["model", "run_time", "valid_time", "lead_h", "lead_bin",
             "point_id", "parameter", "forecast_val", "observed_val", "error"]


def _pairs_table(rows: list[tuple]):
    """rows in PAIR_COLS order."""
    import pyarrow as pa

    if not rows:
        return None
    col = list(zip(*rows))
    return pa.table({
        "model": pa.array(col[0], pa.string()),
        "run_time": pa.array(col[1], pa.timestamp("s", tz="UTC")),
        "valid_time": pa.array(col[2], pa.timestamp("s", tz="UTC")),
        "lead_h": pa.array(col[3], pa.int16()),
        "lead_bin": pa.array(col[4], pa.string()),
        "point_id": pa.array(col[5], pa.string()),
        "parameter": pa.array(col[6], pa.string()),
        "forecast_val": pa.array(col[7], pa.float32()),
        "observed_val": pa.array(col[8], pa.float32()),
        "error": pa.array(col[9], pa.float32()),
    })


def write_pairs_day(rows: list[tuple], day: dt.date) -> str | None:
    """One UTC day of comparisons to R2 as Parquet. Returns the key."""
    import pyarrow.parquet as pq

    table = _pairs_table(rows)
    if table is None:
        return None
    buf = io.BytesIO()
    pq.write_table(
        table, buf,
        compression="zstd", compression_level=10,
        # Five models, six lead buckets, ten parameters and a few hundred
        # points, repeated across a quarter of a million rows — dictionary
        # encoding is most of why a day fits in a few hundred kB.
        use_dictionary=["model", "lead_bin", "point_id", "parameter"],
        version="2.6",
    )
    body = buf.getvalue()
    key = pairs_key_for(day)
    r2_client().put_object(
        Bucket=config.R2_BUCKET, Key=key, Body=body,
        ContentType="application/vnd.apache.parquet",
    )
    log.info("archive: %s (%d rows, %.0f kB)", key, table.num_rows, len(body) / 1024)
    return key
