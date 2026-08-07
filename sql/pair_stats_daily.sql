-- Daily verification totals, so the raw comparisons can live in R2 instead.
--
-- wx.forecast_obs_pairs had grown to 574 MB — the largest object in the
-- database, and 271 MB of that was the primary key index alone, on a 275 MB
-- heap. It held 2.69 million rows covering eleven days, about 245,000 a day.
--
-- Nothing read them individually. The single consumer was wx.model_skill,
-- averaging thirty days, and averages add: a count and three sums per day
-- rebuild bias, MAE and RMSE exactly. Checked against the live view over all
-- 300 model/parameter/lead combinations before writing this — counts
-- identical, bias, MAE and RMSE agreeing to 7e-13, which is float rounding.
--
-- About 5,000 rows a month replaces 7.4 million. The rows themselves go to
-- R2 as one Parquet file per day, which is where you would read them from to
-- ask why one model missed one morning at one station.
--
-- Apply in the Supabase SQL editor. Safe to re-run.

create table if not exists wx.pair_stats_daily (
  day        date             not null,
  model      text             not null,
  parameter  text             not null,
  lead_bin   text             not null,
  n          bigint           not null,
  sum_err    double precision not null,   -- Σ(forecast − observed)  → bias
  sum_abs    double precision not null,   -- Σ|forecast − observed|  → mae
  sum_sq     double precision not null,   -- Σ(error²)               → rmse
  primary key (day, model, parameter, lead_bin)
);

-- Backfill from what is already there, so the weights never see a gap. This
-- runs before the view is repointed, so at no moment is there nothing to read.
insert into wx.pair_stats_daily (day, model, parameter, lead_bin,
                                 n, sum_err, sum_abs, sum_sq)
select valid_time::date, model, parameter, lead_bin,
       count(*), sum(error), sum(abs(error)), sum(error * error)
from wx.forecast_obs_pairs
group by 1, 2, 3, 4
on conflict (day, model, parameter, lead_bin) do update
  set n = excluded.n, sum_err = excluded.sum_err,
      sum_abs = excluded.sum_abs, sum_sq = excluded.sum_sq;

-- Same shape out as before, built from sums instead of rows.
--
-- The window is now whole days rather than an exact 30x24 hours, because that
-- is the resolution the totals have. On a thirty-day average of tens of
-- thousands of samples the difference is not measurable, and it makes the
-- figure reproducible: the same query on the same day gives the same answer,
-- which the rolling now() version never quite did.
create or replace view wx.model_skill as
select model, parameter, lead_bin,
       sum(n)                    as n,
       sum(sum_err) / sum(n)     as bias,
       sum(sum_abs) / sum(n)     as mae,
       sqrt(sum(sum_sq) / sum(n)) as rmse
from wx.pair_stats_daily
where day > (now() - interval '30 days')::date
group by model, parameter, lead_bin;

-- wx.model_weights sits on top of this unchanged, and wx.model_weights_snap
-- is refreshed from it by jobs/run_pairing.py as before. Nothing downstream
-- needs to know where the numbers came from.

-- Check it landed. These two should agree to floating-point noise while the
-- old table still has rows in it:
--
--   select count(*), max(abs(a.rmse - b.rmse))
--   from wx.model_skill a
--   join (select model, parameter, lead_bin, sqrt(avg(error*error)) rmse
--         from wx.forecast_obs_pairs
--         where valid_time > now() - interval '30 days'
--         group by 1,2,3) b using (model, parameter, lead_bin);
