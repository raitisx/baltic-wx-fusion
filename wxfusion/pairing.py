"""Nightly pairing: join forecasts x observations into wx.forecast_obs_pairs.

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
  -- 10-min slots) and average duplicates within the hour.
  select p.point_id,
         date_trunc('hour', o.time + interval '30 minutes') as time,
         case when o.parameter = 'cb_ceiling' then 'cb' else o.parameter end as parameter,
         avg(o.value) as value
  from wx.observations o
  join wx.points p
    on p.kind = o.source and p.station_id = o.station_id
  where o.time >= now() - interval '%(days)s days'
    and o.parameter in ('t2m','td2m','rh','ws10m','wg10m','wdir',
                        'prcp_1h','pres_msl','cc_total','vis','cb_ceiling')
  group by 1, 2, 3
),
fcst as (
  select model, run_time, valid_time, point_id,
         case when parameter = 'cb_lcl' then 'cb' else parameter end as parameter,
         value
  from wx.forecasts
  where valid_time >= now() - interval '%(days)s days'
    and valid_time <= now()
)
insert into wx.forecast_obs_pairs
  (model, run_time, valid_time, lead_h, lead_bin, point_id, parameter,
   forecast_val, observed_val, error)
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
on conflict (model, run_time, valid_time, point_id, parameter) do nothing
"""


def run(conn: psycopg.Connection, days: int = 3) -> int:
    with conn.cursor() as cur:
        cur.execute(PAIRING_SQL % {"days": days})
        n = cur.rowcount
    conn.commit()
    log.info("pairing: inserted %d new pairs (window %d days)", n, days)
    return n
