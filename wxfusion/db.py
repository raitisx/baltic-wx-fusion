"""Thin Postgres helper: batched idempotent upserts into the wx schema."""
from __future__ import annotations

import logging
import time

import psycopg

from . import config

log = logging.getLogger(__name__)

CONNECT_ATTEMPTS = 5
CONNECT_TIMEOUT = 15  # seconds per attempt (libpq default is ~2 min per host)
CONNECT_BACKOFF = (5, 15, 30, 60)  # seconds between attempts


def connect() -> psycopg.Connection:
    """Connect through the Supabase transaction pooler, with retries.

    The shared pooler occasionally refuses / blackholes connections for a
    minute or two. Without a retry a single blip kills the whole hourly run
    and we lose an ingest slot, so back off and try again instead.
    """
    if not config.DB_URL:
        raise RuntimeError("SUPABASE_DB_URL is not set")
    last: Exception | None = None
    for attempt in range(CONNECT_ATTEMPTS):
        try:
            # prepare_threshold=None: required through Supabase's transaction
            # pooler (server-side prepared statements are not supported there).
            return psycopg.connect(
                config.DB_URL,
                autocommit=False,
                prepare_threshold=None,
                connect_timeout=CONNECT_TIMEOUT,
            )
        except psycopg.OperationalError as exc:
            last = exc
            if attempt == CONNECT_ATTEMPTS - 1:
                break
            wait = CONNECT_BACKOFF[min(attempt, len(CONNECT_BACKOFF) - 1)]
            log.warning(
                "db connect attempt %d/%d failed (%s); retrying in %ds",
                attempt + 1, CONNECT_ATTEMPTS, exc.__class__.__name__, wait,
            )
            time.sleep(wait)
    raise RuntimeError(f"could not reach the Supabase pooler after {CONNECT_ATTEMPTS} attempts") from last


def upsert_observations(conn: psycopg.Connection, rows: list[tuple]) -> int:
    """rows: (source, station_id, time, parameter, value)"""
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            insert into wx.observations (source, station_id, time, parameter, value)
            values (%s, %s, %s, %s, %s)
            on conflict (source, station_id, time, parameter) do nothing
            """,
            rows,
        )
    conn.commit()
    log.info("observations: upserted %d rows", len(rows))
    return len(rows)


FORECAST_PARAMS = [
    "t2m", "td2m", "rh", "ws10m", "wg10m", "wdir", "prcp_1h", "pres_msl",
    "cc_low", "cc_mid", "cc_high", "cc_total", "cb_lcl",
]
_FH_COLS = ", ".join(FORECAST_PARAMS)
_FH_PLACEHOLDERS = ", ".join(["%s"] * (4 + len(FORECAST_PARAMS)))


def upsert_forecasts(conn: psycopg.Connection, rows: list[tuple]) -> int:
    """Pivot flat rows into the hot table.

    rows: (model, run_time, valid_time, point_id, parameter, value)

    Storing one row per value cost ~190 B each including the primary key
    index; pivoting the 13 canonical parameters into typed columns is ~13x
    smaller, which is what makes a 60-day hot window fit the free tier.
    Unrecognised parameters are dropped — the full fidelity copy is the
    Parquet run archive in R2.
    """
    if not rows:
        return 0
    pivot: dict[tuple, dict] = {}
    skipped = set()
    for model, run_time, valid_time, point_id, parameter, value in rows:
        if parameter not in FORECAST_PARAMS:
            skipped.add(parameter)
            continue
        pivot.setdefault((model, run_time, valid_time, point_id), {})[parameter] = value
    if skipped:
        log.debug("forecasts: parameters not in the hot schema: %s", sorted(skipped))

    payload = [
        key + tuple(vals.get(p) for p in FORECAST_PARAMS)
        for key, vals in pivot.items()
    ]
    with conn.cursor() as cur:
        cur.executemany(
            f"""
            insert into wx.forecasts_h (model, run_time, valid_time, point_id, {_FH_COLS})
            values ({_FH_PLACEHOLDERS})
            on conflict (model, run_time, valid_time, point_id) do nothing
            """,
            payload,
        )
    conn.commit()
    log.info("forecasts: %d values -> %d hot rows", len(rows), len(payload))
    return len(payload)


def prune_forecasts(conn: psycopg.Connection, keep_days: int = 60) -> int:
    """Drop hot rows past the rolling window. R2 keeps the full history."""
    with conn.cursor() as cur:
        cur.execute(
            "delete from wx.forecasts_h where run_time < now() - make_interval(days => %s)",
            (keep_days,),
        )
        n = cur.rowcount
    conn.commit()
    log.info("forecasts: pruned %d rows older than %d days", n, keep_days)
    return n


def get_points(conn: psycopg.Connection, kind: str | None = None) -> list[dict]:
    q = "select point_id, kind, station_id, name, lat, lon from wx.points where active"
    args: tuple = ()
    if kind:
        q += " and kind = %s"
        args = (kind,)
    with conn.cursor() as cur:
        cur.execute(q, args)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
