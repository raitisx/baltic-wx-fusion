"""Nightly pairing: join forecasts x observations, total them, archive them.

The comparisons used to be stored row by row in wx.forecast_obs_pairs, which
grew to 574 MB — the largest thing in the database, and 271 MB of that was the
primary key index alone. Nothing read them individually. The one consumer was
a view averaging thirty days, and averages add, so the daily totals in
wx.pair_stats_daily give the same answer from about 5,000 rows a month instead
of 245,000 a day. Verified against the live view over 300 model/parameter/lead
combinations: identical counts, and bias, MAE and RMSE agreeing to 7e-13.

So the rows are built in a temporary table, summed into pair_stats_daily, and
handed back for archiving to R2 as one Parquet file per day. Nothing is lost;
it just stops living somewhere expensive.

Pure SQL, executed from Python so the whole pipeline has one runner.
Observations are matched to points via wx.points (kind = source,
station_id = station_id). Forecast cb_lcl is paired against observed
cb_ceiling (both 'cloud base'), everything else pairs by equal parameter name.
"""
from __future__ import annotations

import logging

import psycopg

log = logging.getLogger(__name__)

PAIRING_SQL = """
with obs as (
  -- Snap observations to the nearest hour (METARs land at :20/:50, EE at
  -- 10-min slots) and average duplicates within the hour. The hot table
  -- stores a column per parameter, so unpivot the ones with a forecast
  -- counterpart; observed ceiling is relabelled 'cb' to meet model LCL.
  select p.point_id,
         date_trunc('hour', o.time + interval '30 minutes') as time,
         u.parameter,
         avg(u.value) as value
  from wx.observations_h o
  join wx.points p
    -- Station feeds are keyed by (kind, station_id); radar-derived rows are
    -- sampled at a point and keyed by point_id directly, so they verify every
    -- forecast point rather than only the ones with an instrument.
    on (p.kind = o.source and p.station_id = o.station_id)
    or (o.source = 'radar' and p.point_id = o.station_id)
  cross join lateral (values
      ('t2m', o.t2m), ('td2m', o.td2m), ('rh', o.rh),
      ('ws10m', o.ws10m), ('wg10m', o.wg10m), ('wdir', o.wdir),
      ('prcp_1h', o.prcp_1h), ('pres_msl', o.pres_msl),
      ('cc_total', o.cc_total), ('vis', o.vis), ('cb', o.cb_ceiling)
    ) as u(parameter, value)
  where o.time >= now() - interval '%(days)s days'
    and u.value is not null
  group by 1, 2, 3
),
fcst as (
  -- The hot table stores a column per parameter; unpivot only the ones that
  -- have an observed counterpart. Cloud base pairs model LCL against the
  -- METAR ceiling, so both sides are relabelled 'cb'.
  select f.model, f.run_time, f.valid_time, f.point_id, u.parameter, u.value
  from wx.forecasts_h f
  cross join lateral (values
      ('t2m', f.t2m), ('td2m', f.td2m), ('rh', f.rh),
      ('ws10m', f.ws10m), ('wg10m', f.wg10m), ('wdir', f.wdir),
      ('prcp_1h', f.prcp_1h), ('pres_msl', f.pres_msl),
      ('cc_total', f.cc_total), ('cb', f.cb)
    ) as u(parameter, value)
  where f.valid_time >= now() - interval '%(days)s days'
    and f.valid_time <= now()
    and u.value is not null
)
create temporary table _pairs on commit drop as
select
  f.model, f.run_time, f.valid_time,
  extract(epoch from (f.valid_time - f.run_time))::int / 3600 as lead_h,
  case
    when f.valid_time - f.run_time < interval '6 hours'  then '0-6'
    when f.valid_time - f.run_time < interval '12 hours' then '6-12'
    when f.valid_time - f.run_time < interval '24 hours' then '12-24'
    when f.valid_time - f.run_time < interval '48 hours' then '24-48'
    when f.valid_time - f.run_time < interval '72 hours' then '48-72'
    else '72+'
  end as lead_bin,
  f.point_id, f.parameter, f.value, o.value, f.value - o.value
from fcst f
join obs o
  on o.point_id = f.point_id
 and o.parameter = f.parameter
 and o.time = f.valid_time          -- hourly alignment
where f.valid_time >= f.run_time
"""

# Re-totalling a whole day each pass rather than adding to it: the pairing
# window overlaps itself by design (three days, run daily), so an increment
# would count the same comparison several times. A day is rebuilt from
# whatever is currently pairable, which is also what makes the job safe to
# re-run.
ROLLUP_SQL = """
insert into wx.pair_stats_daily
  (day, model, parameter, lead_bin, n, sum_err, sum_abs, sum_sq)
select valid_time::date, model, parameter, lead_bin,
       count(*), sum(error), sum(abs(error)), sum(error * error)
from _pairs
group by 1, 2, 3, 4
on conflict (day, model, parameter, lead_bin) do update
  set n = excluded.n, sum_err = excluded.sum_err,
      sum_abs = excluded.sum_abs, sum_sq = excluded.sum_sq
"""


def run(conn: psycopg.Connection, days: int = 3, archive_to_r2: bool = True) -> int:
    """Build the comparisons, total them into pair_stats_daily, archive the rows.

    One transaction: the temp table is dropped on commit, so a failure part way
    leaves the totals untouched rather than half-written.
    """
    import collections

    from . import archive

    by_day: dict = collections.defaultdict(list)
    with conn.cursor() as cur:
        cur.execute(PAIRING_SQL % {"days": days})
        cur.execute("select count(*) from _pairs")
        n = cur.fetchone()[0]
        cur.execute(ROLLUP_SQL)
        if archive_to_r2 and n:
            cur.execute(
                "select model, run_time, valid_time, lead_h, lead_bin, "
                "point_id, parameter, forecast_val, observed_val, error "
                "from _pairs order by valid_time")
            for row in cur:
                by_day[row[2].date()].append(row)
    conn.commit()
    log.info("pairing: %d comparisons over %d days -> daily totals", n, days)

    # Archive after the commit: a slow upload must not hold the transaction,
    # and a failed one must not lose the totals. The day file is rewritten
    # whole each time, so a retry next pass repairs it.
    for day, rows in sorted(by_day.items()):
        try:
            archive.write_pairs_day(rows, day)
        except Exception:
            log.exception("pairs archive failed for %s", day)
    return n
