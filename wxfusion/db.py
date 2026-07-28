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


def upsert_forecasts(conn: psycopg.Connection, rows: list[tuple]) -> int:
    """rows: (model, run_time, valid_time, point_id, parameter, value)"""
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            insert into wx.forecasts (model, run_time, valid_time, point_id, parameter, value)
            values (%s, %s, %s, %s, %s, %s)
            on conflict (model, run_time, valid_time, point_id, parameter) do nothing
            """,
            rows,
        )
    conn.commit()
    log.info("forecasts: upserted %d rows", len(rows))
    return len(rows)


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
