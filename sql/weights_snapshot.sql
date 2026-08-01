-- Applied 2026-08-01. Kept here because it is not obvious from the schema why
-- the page reads a snapshot instead of the view it is named after.
--
-- wx_weights() used to select from wx.model_weights, a view over
-- wx.model_skill, a view that aggregates thirty days of wx.forecast_obs_pairs.
-- That table is 55 MB, and there is no index that answers the aggregate, so
-- every page load did a full heap scan to produce 15 kB of JSON. Warm it took
-- 220 ms. Cold, or while the pairing job was writing to the same table, or
-- during one of the long checkpoints this instance takes, it ran past the
-- three-second statement timeout the anon role carries, and PostgREST turned
-- the cancelled statement into "HTTP 500".
--
-- Model weights change over days. There was never a reason to recompute them
-- per visitor. The snapshot is refreshed by jobs/run_pairing.py, immediately
-- after the pairs it summarises are written and before they are pruned.

create materialized view if not exists wx.model_weights_snap as
select model, parameter, lead_bin, n, rmse, weight, now() as computed_at
from wx.model_weights;

-- Required for REFRESH ... CONCURRENTLY, which is what keeps a refresh from
-- blocking a page that happens to load during it.
create unique index if not exists model_weights_snap_key
  on wx.model_weights_snap (model, parameter, lead_bin);

create or replace function public.wx_refresh_weights()
returns text language plpgsql security definer set search_path to 'wx','public' as $$
begin
  refresh materialized view concurrently wx.model_weights_snap;
  return 'ok';
exception when others then
  -- A never-populated matview cannot be refreshed concurrently.
  refresh materialized view wx.model_weights_snap;
  return 'ok (full)';
end $$;

create or replace function public.wx_weights()
returns jsonb language sql stable security definer
set search_path to 'wx','public' as $$
  select coalesce(jsonb_agg(jsonb_build_object(
    'm', model, 'p', parameter, 'lb', lead_bin, 'n', n,
    'rmse', round(rmse::numeric, 3), 'w', round(weight::numeric, 4))), '[]'::jsonb)
  from wx.model_weights_snap
$$;

-- The page may read the weights; only the job may rebuild them.
revoke all on function public.wx_refresh_weights() from anon, authenticated;
grant execute on function public.wx_weights() to anon, authenticated;
