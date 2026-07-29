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
    "cc_low", "cc_mid", "cc_high", "cc_total", "cb_lcl", "cb",
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
