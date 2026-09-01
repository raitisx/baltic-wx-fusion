-- Latvian UAS geographical zones (EUROCAE ED-269), with a year of history.
--
-- Source: Latvijas Gaisa Satiksme publish the whole set as one ED-269
-- UASZoneVersion JSON, listed with its own effective-from stamp and MD5 at
-- drz.lv/api/v1/export-history. The file is a SNAPSHOT: it carries every
-- zone that exists at that moment, so "what changed" is ours to work out.
--
-- Two tables, because two questions are being asked. uas_geozone_files is
-- the ingest log — which snapshot was taken when, and whether it differed
-- from the one before it. uas_geozones is the zones themselves, each row
-- one VERSION of one zone: identity plus the payload hash, opened when that
-- shape first appeared and closed when it stopped appearing. A zone whose
-- polygon or applicability is edited becomes a new row; an untouched zone
-- keeps one row however many times it is re-fetched.
--
-- That makes "the set as it stood on 14.08" a single range query, and it
-- keeps the table proportional to how often the zones actually change
-- rather than to how often we poll.
--
-- Apply in the Supabase SQL editor. Safe to re-run.

create table if not exists wx.uas_geozone_files (
  md5            text        primary key,   -- the publisher's own checksum
  effective_from timestamptz not null,      -- what the listing calls it
  crc32q         text,
  file_name      text        not null,
  fetched_at     timestamptz not null default now(),
  n_zones        integer     not null,
  changed        boolean     not null       -- did this differ from the last?
);

create index if not exists uas_geozone_files_eff
  on wx.uas_geozone_files (effective_from desc);

create table if not exists wx.uas_geozones (
  identifier     text        not null,      -- ED-269 zone id, e.g. "b2appgd"
  payload_md5    text        not null,      -- hash of the zone's own record
  first_seen     timestamptz not null,      -- effective_from of the first file
  last_seen      timestamptz not null,      -- effective_from of the latest
  withdrawn_at   timestamptz,               -- first file that no longer had it
  name           text,
  zone_type      text,                      -- ED-269 "type"
  restriction    text,                      -- PROHIBITED / REQ_AUTHORISATION / …
  reason         text[],
  other_reason   text,
  country        text,
  -- Applicability, flattened from the first entry so "active then?" is a
  -- plain SQL range test. permanent zones get a null window.
  permanent      boolean,
  starts_at      timestamptz,
  ends_at        timestamptz,
  -- Vertical extent of the first geometry, in the units the file states.
  lower_limit    double precision,
  lower_ref      text,                      -- AGL / AMSL / WGS84
  upper_limit    double precision,
  upper_ref      text,
  uom            text,                      -- M / FT
  -- Where it is. bbox is for cheap "does this touch the map" filtering; the
  -- full geometry stays as the publisher wrote it.
  min_lon        double precision,
  min_lat        double precision,
  max_lon        double precision,
  max_lat        double precision,
  geometry       jsonb       not null,      -- ED-269 "geometry" array verbatim
  zone           jsonb       not null,      -- the whole record, verbatim
  primary key (identifier, payload_md5)
);

create index if not exists uas_geozones_live
  on wx.uas_geozones (withdrawn_at, restriction)
  where withdrawn_at is null;

create index if not exists uas_geozones_window
  on wx.uas_geozones (starts_at, ends_at);

create index if not exists uas_geozones_bbox
  on wx.uas_geozones (min_lon, min_lat, max_lon, max_lat);

-- The zones as they stand right now: one row per zone, newest version.
create or replace view wx.uas_geozones_current as
  select distinct on (identifier) *
    from wx.uas_geozones
   where withdrawn_at is null
   order by identifier, last_seen desc;

comment on table wx.uas_geozones is
  'Latvian UAS geographical zones, ED-269, one row per version of a zone. '
  'Source: Latvijas Gaisa Satiksme (LGS) / Civil Aviation Agency of Latvia.';
