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
  -- 10-min slots) and average duplicates within the hour. The hot table
  -- stores a column per parameter, so unpivot the ones with a forecast
  -- counterpart; observed ceiling is relabelled 'cb' to meet model LCL.
  select p.point_id,
         date_trunc('hour', o.time + interval '30 minutes') as time,
         u.parameter,
         avg(u.value) as value
  from wx.observations_h o
  join wx.points p
    on p.kind = o.source and p.station_id = o.station_id
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
