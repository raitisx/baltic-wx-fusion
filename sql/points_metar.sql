-- METAR verification stations.
--
-- METAR is the only source in the system carrying a directly observed cloud
-- base; everything else is model-derived LCL. Every station here is also a
-- forecast point, because a verification pair needs both sides at the same
-- place — that is what feeds wx.model_skill and the fused weights.
--
-- Applied to the live database on 28.07.2026. Kept in the repo so the station
-- set is reproducible rather than living only in the database.

-- LV / EE / LT — the original twelve.
insert into wx.points (point_id, kind, station_id, name, lat, lon, active) values
  ('EETN','metar','EETN','Tallinn Lennart Meri', 59.413, 24.833, true),
  ('EETU','metar','EETU','Tartu',                58.308, 26.690, true),
  ('EEPU','metar','EEPU','Parnu',                58.419, 24.473, true),
  ('EEKA','metar','EEKA','Kardla',               58.991, 22.831, true),
  ('EEKE','metar','EEKE','Kuressaare',           58.230, 22.510, true),
  ('EVRA','metar','EVRA','Riga Intl',            56.924, 23.971, true),
  ('EVLA','metar','EVLA','Liepaja',              56.518, 21.097, true),
  ('EVVA','metar','EVVA','Ventspils',            57.358, 21.544, true),
  ('EYVI','metar','EYVI','Vilnius',              54.634, 25.286, true),
  ('EYKA','metar','EYKA','Kaunas',               54.964, 24.085, true),
  ('EYSA','metar','EYSA','Siauliai',             55.894, 23.395, true),
  ('EYPA','metar','EYPA','Palanga',              55.973, 21.094, true)
on conflict (point_id) do update
  set active = true, kind = excluded.kind, station_id = excluded.station_id,
      name = excluded.name, lat = excluded.lat, lon = excluded.lon;

-- Belarus and Russia, added 28.07.2026. The twelve above are all inside
-- LV/EE/LT, which left the entire eastern approach without ceiling ground
-- truth — Pskov sits 60 km from the Latvian border and 50 km from the
-- Estonian one, right where weather arrives from on easterly flow.
--
-- Five of these fall outside the EPSG:3059 raster grid (X 235..805,
-- Y -40..650 km) so they get no dot on the selector map. That costs nothing:
-- a station does not need to be on the map to verify a forecast, and widening
-- the grid would mean re-rendering every stored image.
--
-- All eight were confirmed live against IEM on 28.07.2026, each reporting a
-- cloud ceiling. UMMM (old Minsk) and ULOL (Velikiye Luki) are deliberately
-- absent: their archives end in 2018 and 2012.
insert into wx.points (point_id, kind, station_id, name, lat, lon, active) values
  ('UMKK','metar','UMKK','Kaliningrad',    54.700, 20.617, true),  -- on map
  ('ULOO','metar','ULOO','Pskov',          57.784, 28.396, true),  -- on map
  ('UMMS','metar','UMMS','Minsk 2',        53.883, 28.033, true),  -- on map
  ('UMMG','metar','UMMG','Hrodna',         53.602, 24.056, true),  -- 21 km S
  ('UMII','metar','UMII','Vitebsk',        55.167, 30.217, true),  -- 91 km E
  ('UMIO','metar','UMIO','Orsha',          54.440, 30.297, true),
  ('UMOO','metar','UMOO','Mogilev',        53.956, 30.096, true),
  ('ULLI','metar','ULLI','St Petersburg',  59.967, 30.300, true)
on conflict (point_id) do update
  set active = true, kind = excluded.kind, station_id = excluded.station_id,
      name = excluded.name, lat = excluded.lat, lon = excluded.lon;
