"""Thin Postgres helper: batched idempotent upserts into the wx schema."""
from __future__ import annotations

import logging

import psycopg

from . import config

log = logging.getLogger(__name__)


def connect() -> psycopg.Connection:
    if not config.DB_URL:
        raise RuntimeError("SUPABASE_DB_URL is not set")
    return psycopg.connect(config.DB_URL, autocommit=False)


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
