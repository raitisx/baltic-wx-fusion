-- Point list the browser sees.
--
-- wx.points holds three kinds and only one of them is a place a person would
-- choose from a map:
--   metar    real stations, 20 of them
--   grid     60 evenly spread cells sampled purely to score the models
--   virtual  cells created when someone clicks the map
--
-- The last two are ingest bookkeeping. When the list returned all 114 the
-- selector map came back covered in dots. Filtering here rather than in the
-- page means a cached copy of the page cannot bring them back.
create or replace function public.wx_points_list()
 returns jsonb
 language sql
 stable security definer
 set search_path to 'wx','public'
as $function$
  select coalesce(jsonb_agg(jsonb_build_object(
    'point_id', point_id, 'kind', kind, 'name', name,
    'lat', lat, 'lon', lon) order by point_id), '[]'::jsonb)
  from wx.points where active and kind = 'metar'
$function$;

grant execute on function public.wx_points_list() to anon, authenticated;
