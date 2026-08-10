"""Pairing from the R2 forecast archive instead of wx.forecasts_h.

The nightly pairing joins forecasts against observations and totals the errors
into wx.pair_stats_daily. Its forecast side reads wx.forecasts_h, and to score a
long lead for a recent valid hour it needs the run that made that forecast up to
a week earlier — which is why forecasts_h has to keep a fortnight, and why it is
most of a 500 MB database.

Every one of those runs is already in R2 as Parquet (archive/forecasts). This
computes the same daily totals from there: the forecast side comes from the
archive via DuckDB, the observation side is prepared in Postgres exactly as the
live pairing does it (a few tens of thousands of rows), and the join and rollup
run in DuckDB. Nothing is loaded into Postgres, so it works even while the
database is jammed against its quota — which is the whole point, since it is what
lets forecasts_h be truncated.

This module is a read-only parity harness first: `parity()` computes the totals
both ways over the same window and the same instant and reports any difference,
so the archive path can be trusted to the same 7e-13 the R2 migration was, before
the nightly job is switched to it or forecasts_h is dropped.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import tempfile

from . import archive, config, db  # noqa: F401  (db used by callers)

log = logging.getLogger(__name__)

# The observed side, prepared in Postgres — byte-for-byte the obs CTE the live
# pairing uses (hour-snap + average, the station/radar point join, the same
# unpivot with observed ceiling relabelled 'cb'). Bound to an explicit instant
# so it and the forecast side share one window edge.
OBS_SQL = """
select p.point_id,
       date_trunc('hour', o.time + interval '30 minutes') as time,
       u.parameter, avg(u.value) as value
from wx.observations_h o
join wx.points p
  on (p.kind = o.source and p.station_id = o.station_id)
  or (o.source = 'radar' and p.point_id = o.station_id)
cross join lateral (values
    ('t2m', o.t2m), ('td2m', o.td2m), ('rh', o.rh),
    ('ws10m', o.ws10m), ('wg10m', o.wg10m), ('wdir', o.wdir),
    ('prcp_1h', o.prcp_1h), ('pres_msl', o.pres_msl),
    ('cc_total', o.cc_total), ('vis', o.vis), ('cb', o.cb_ceiling)
  ) as u(parameter, value)
where o.time >= %(now)s::timestamptz - make_interval(days => %(days)s)
  and u.value is not null
group by 1, 2, 3
"""

# The same rollup the live pairing produces, but read-only and bound to a fixed
# instant, straight out of forecasts_h — the reference the archive path is
# checked against.
PG_ROLLUP_SQL = """
with obs as (
""" + OBS_SQL + """
),
fcst as (
  select f.model, f.run_time, f.valid_time, f.point_id, u.parameter, u.value
  from wx.forecasts_h f
  cross join lateral (values
      ('t2m', f.t2m), ('td2m', f.td2m), ('rh', f.rh),
      ('ws10m', f.ws10m), ('wg10m', f.wg10m), ('wdir', f.wdir),
      ('prcp_1h', f.prcp_1h), ('pres_msl', f.pres_msl),
      ('cc_total', f.cc_total), ('cb', f.cb)
    ) as u(parameter, value)
  where f.valid_time >= %(now)s::timestamptz - make_interval(days => %(days)s)
    and f.valid_time <= %(now)s::timestamptz
    and u.value is not null
)
select f.valid_time::date as day, f.model, f.parameter,
  case
    when f.valid_time - f.run_time < interval '6 hours'  then '0-6'
    when f.valid_time - f.run_time < interval '12 hours' then '6-12'
    when f.valid_time - f.run_time < interval '24 hours' then '12-24'
    when f.valid_time - f.run_time < interval '48 hours' then '24-48'
    when f.valid_time - f.run_time < interval '72 hours' then '48-72'
    else '72+'
  end as lead_bin,
  count(*) as n, sum(f.value-o.value) as sum_err,
  sum(abs(f.value-o.value)) as sum_abs, sum((f.value-o.value)*(f.value-o.value)) as sum_sq
from fcst f
join obs o on o.point_id=f.point_id and o.parameter=f.parameter and o.time=f.valid_time
where f.valid_time >= f.run_time
group by 1, 2, 3, 4
"""

# The forecast parameters that have an observed counterpart — the same ten the
# live pairing unpivots (visibility is observed but never forecast, so it never
# pairs; cloud base is the model 'cb' column against the observed ceiling).
_FCST_PARAMS = ["t2m", "td2m", "rh", "ws10m", "wg10m", "wdir",
                "prcp_1h", "pres_msl", "cc_total", "cb"]


def _duck_rollup_sql(files: list[str], now: dt.datetime, days: int) -> str:
    cols = ", ".join(_FCST_PARAMS)
    files_lit = "[" + ", ".join(repr(f) for f in files) + "]"
    now_lit = "TIMESTAMPTZ '" + now.strftime("%Y-%m-%d %H:%M:%S") + "+00'"
    return f"""
with fcst as (
  select model, run_time, valid_time, point_id, parameter, value
  from (
    unpivot (
      select model, run_time, valid_time, point_id, {cols}
      from read_parquet({files_lit})
    ) on {cols} into name parameter value value
  )
  where value is not null
    and valid_time >= {now_lit} - interval {days} day
    and valid_time <= {now_lit}
)
select cast(f.valid_time as date) as day, f.model, f.parameter,
  case
    when f.valid_time - f.run_time < interval 6 hour  then '0-6'
    when f.valid_time - f.run_time < interval 12 hour then '6-12'
    when f.valid_time - f.run_time < interval 24 hour then '12-24'
    when f.valid_time - f.run_time < interval 48 hour then '24-48'
    when f.valid_time - f.run_time < interval 72 hour then '48-72'
    else '72+'
  end as lead_bin,
  count(*) as n, sum(f.value - o.value) as sum_err,
  sum(abs(f.value - o.value)) as sum_abs,
  sum((f.value - o.value) * (f.value - o.value)) as sum_sq
from fcst f
join obs o on o.point_id = f.point_id and o.parameter = f.parameter
          and o.time = f.valid_time
where f.valid_time >= f.run_time
group by 1, 2, 3, 4
"""


def _month_prefixes(lo: dt.datetime, hi: dt.datetime) -> list[str]:
    out, y, m = [], lo.year, lo.month
    while (y, m) <= (hi.year, hi.month):
        out.append(f"archive/forecasts/{y:04d}/{m:02d}/")
        m = 1 if m == 12 else m + 1
        y = y + 1 if m == 1 else y
    return out


def _run_time_of(key: str) -> dt.datetime | None:
    base = key.rsplit("/", 1)[-1].removesuffix(".parquet")  # YYYYMMDDTHHMM
    try:
        return dt.datetime.strptime(base, "%Y%m%dT%H%M").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def _download_runs(lo: dt.datetime, hi: dt.datetime, tmpdir: str) -> list[str]:
    """Every archived run file with run_time in [lo, hi], to a local dir."""
    s3 = archive.r2_client()
    keys = set()
    for pfx in _month_prefixes(lo, hi):
        keys |= archive.list_keys(pfx)
    files = []
    for k in sorted(keys):
        rt = _run_time_of(k)
        if rt is None or rt < lo or rt > hi:
            continue
        dst = os.path.join(tmpdir, k.rsplit("/", 1)[-1])
        s3.download_file(config.R2_BUCKET, k, dst)
        files.append(dst)
    return files


def _obs_arrow(conn, now: dt.datetime, days: int):
    import pyarrow as pa
    with conn.cursor() as cur:
        cur.execute(OBS_SQL, {"now": now, "days": days})
        rows = cur.fetchall()
    return pa.table({
        "point_id": pa.array([r[0] for r in rows], pa.string()),
        "time": pa.array([r[1] for r in rows], pa.timestamp("s", tz="UTC")),
        "parameter": pa.array([r[2] for r in rows], pa.string()),
        "value": pa.array([float(r[3]) for r in rows], pa.float64()),
    })


def _pg_totals(conn, now: dt.datetime, days: int) -> dict:
    with conn.cursor() as cur:
        cur.execute(PG_ROLLUP_SQL, {"now": now, "days": days})
        return {(r[0], r[1], r[2], r[3]): (r[4], float(r[5]), float(r[6]), float(r[7]))
                for r in cur}
    # conn left as-is; caller rolls back (read-only)


def compute_r2(conn, now: dt.datetime, days: int, lo: dt.datetime) -> dict:
    """Daily totals from the R2 archive. `lo` is the oldest run_time to pull —
    anchored to forecasts_h's own oldest run so the archive path scores exactly
    the run set the Postgres path does, isolating the forecast source."""
    import duckdb

    obs = _obs_arrow(conn, now, days)
    with tempfile.TemporaryDirectory() as tmp:
        files = _download_runs(lo, now, tmp)
        if not files:
            raise RuntimeError("no archived run files in window")
        con = duckdb.connect()
        con.register("obs", obs)
        rows = con.execute(_duck_rollup_sql(files, now, days)).fetchall()
        con.close()
    out = {}
    for day, model, param, lead_bin, n, se, sa, sq in rows:
        d = day if isinstance(day, dt.date) else dt.date.fromisoformat(str(day)[:10])
        out[(d, model, param, lead_bin)] = (int(n), float(se), float(sa), float(sq))
    return out


def parity(conn, days: int = 3, rel_tol: float = 1e-9) -> dict:
    """Compute the totals both ways over one window and instant, compare.

    Reads only; rolls back the Postgres side so nothing is written. Returns a
    report; raises AssertionError if the two disagree beyond counts-exact and
    sums within rel_tol.
    """
    with conn.cursor() as cur:
        cur.execute("select now(), min(run_time) from wx.forecasts_h")
        now, lo = cur.fetchone()
    if lo is None:
        raise RuntimeError("forecasts_h is empty; nothing to check parity against")

    pg = _pg_totals(conn, now, days)
    conn.rollback()  # PG_ROLLUP made no changes, but keep the txn clean
    r2 = compute_r2(conn, now, days, lo)
    conn.rollback()

    keys = set(pg) | set(r2)
    only_pg = sorted(set(pg) - set(r2))
    only_r2 = sorted(set(r2) - set(pg))
    n_mismatch, worst = 0, 0.0
    worst_key = None
    for k in set(pg) & set(r2):
        pn, pse, psa, psq = pg[k]
        rn, rse, rsa, rsq = r2[k]
        if pn != rn:
            n_mismatch += 1
            continue
        for a, b in ((pse, rse), (psa, rsa), (psq, rsq)):
            denom = max(abs(a), abs(b), 1.0)
            rel = abs(a - b) / denom
            if rel > worst:
                worst, worst_key = rel, k
    report = {
        "now": now.isoformat(), "days": days,
        "pg_combos": len(pg), "r2_combos": len(r2),
        "only_pg": len(only_pg), "only_r2": len(only_r2),
        "count_mismatches": n_mismatch, "worst_rel_diff": worst,
        "worst_key": worst_key,
        "pg_total_pairs": sum(v[0] for v in pg.values()),
        "r2_total_pairs": sum(v[0] for v in r2.values()),
    }
    log.info("parity: %s", report)
    if only_pg[:5]:
        log.info("parity: keys only in PG (sample): %s", only_pg[:5])
    if only_r2[:5]:
        log.info("parity: keys only in R2 (sample): %s", only_r2[:5])
    assert not only_pg and not only_r2, "key sets differ (see log)"
    assert n_mismatch == 0, f"{n_mismatch} lead-bin count mismatches"
    assert worst <= rel_tol, f"sums differ by {worst:.2e} at {worst_key}"
    return report
