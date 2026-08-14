-- Point list the browser sees.
--
-- wx.points holds several kinds; the selectable ones are the real measuring
-- stations:
--   metar    airports, 20 of them (temp/wind/humidity, but no usable rain)
--   lvgmc    Latvian environment agency, 34 — real rain and humidity
--   lt       meteo.lt, 52 — real rain and humidity
--   ee       Estonian network, 152 — real rain and humidity
-- and the bookkeeping ones that are NOT places a person picks:
--   grid     evenly spread cells sampled purely to score the models
--   virtual  cells created when someone clicks the map
--
-- The national networks were added after this list first shipped, so it long
-- returned METAR only and their rain/humidity were unreachable from the page.
-- Grid/virtual stay filtered out here rather than in the page, so a cached copy
-- of the page cannot bring them back.
create or replace function public.wx_points_list()
 returns jsonb
 language sql
 stable security definer
 set search_path to 'wx','public'
as $function$
  select coalesce(jsonb_agg(jsonb_build_object(
    'point_id', point_id, 'kind', kind, 'name', name,
    'lat', lat, 'lon', lon) order by point_id), '[]'::jsonb)
  from wx.points where active and kind in ('metar','lvgmc','lt','ee')
$function$;

grant execute on function public.wx_points_list() to anon, authenticated;
