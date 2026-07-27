# baltic-wx-fusion

Phase 1 plumbing for the Baltic weather fusion service. See
`baltic-wx-fusion-requirements.md` (in the weather folder) for the full spec.

## What runs

| Job | Schedule | What it does |
|---|---|---|
| `jobs/run_observations.py` | hourly (GH Actions) | METAR (IEM) + LVĢMC (data.gov.lv) + meteo.lt + EE XML → `wx.observations` |
| `jobs/run_forecasts.py` | hourly | Open-Meteo ICON/MEPS/IFS/GFS extracts at all `wx.points` → `wx.forecasts` (incl. derived `cb_lcl`) |
| `jobs/run_pairing.py` | nightly 02:40 UTC | joins forecasts × observations → `wx.forecast_obs_pairs` |
| `jobs/run_radar.py` | every 15 min, experimental | OPERA composites via ORD API → R2 `baltic-wx` bucket |

Storage (already provisioned):

- Supabase project **TT Aero tracker** (`xxxywjxmdpildafzbnpj`), schema `wx`:
  `points`, `forecasts`, `observations`, `forecast_obs_pairs` + views
  `model_skill` (rolling 30-day RMSE/MAE/bias) and `model_weights`
  (normalized `1/RMSE²` per parameter × lead_bin, min 20 samples).
- Cloudflare R2 bucket **baltic-wx** for radar (and later grid) rasters.

## Setup (one time)

1. Push this folder to a GitHub repo.
2. Add repository secrets (Settings → Secrets and variables → Actions):
   - `SUPABASE_DB_URL` — Supabase Dashboard → Connect → *Transaction pooler* URI
     (the `...pooler.supabase.com:6543/postgres` one), password filled in.
   - `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` — Cloudflare
     Dashboard → R2 → Manage API tokens (Object Read & Write on `baltic-wx`).
     Only the radar job needs these.
3. Enable Actions. Trigger each workflow once by hand (`workflow_dispatch`) and
   check the run logs.

## Which model was best last week?

```sql
select * from wx.model_skill
where parameter = 'prcp_1h' and lead_bin = '12-24'
order by rmse;
```

Current fusion weights: `select * from wx.model_weights order by parameter, lead_bin, weight desc;`

## Design decisions & caveats (read once)

- **Canonical parameters** (`t2m, td2m, rh, ws10m, wg10m, wdir, prcp_1h,
  pres_msl, cc_total/low/mid/high, vis, cb_lcl, cb_ceiling, wx_rain, snow`) are
  normalized at ingest, so pairing is a straight SQL join.
- **Cloud base**: forecast side is `cb_lcl` (Espy LCL from T/Td — P1 proxy);
  observed side is METAR `cb_ceiling` (lowest BKN/OVC/VV). Pairing maps both
  to `cb`. Upgrading the forecast derivation (layered cloud cover) is Phase 2+.
- **run_time = retrieval hour**, not true model cycle (Open-Meteo doesn't
  expose it). This matches decision-time semantics for the report; raw-GRIB
  ingest can add true cycles later.
- **LVĢMC DATETIME assumed UTC** — verify after first day (compare RIGASLU
  `t2m` vs EVRA METAR) and flip `ASSUME_UTC` in `wxfusion/ingest/lvgmc.py` if off.
- **EE feed is real-time only** — every hour not collected is lost. Don't
  pause this workflow.
- **meteo.lt backfill**: `wxfusion/ingest/meteolt.py:fetch_date()` pulls any
  historical day (10 y depth) — run a one-off loop when you want the archive.
- **LVĢMC backfill**: archive resource id is in `config.py`
  (`LVGMC_RES_ARCHIVE_FAKT`, trailing 365 days).
- **Radar (ORD) — verified live**: EDR queries against
  `api.meteogate.eu/eu-eumetnet-weather-radar` work (ACRR:comp = 1 h accum,
  RATE:comp, DBZH:comp at 15-min cadence; EE + LT radars present, LV not yet
  flowing to OPERA — composite covers LV via neighbour overlap). The anonymous
  tier serves point/area extractions only; full ODIM HDF5 rasters need the free
  API key + MQTT service (Phase 2/3 TODO, see wxfusion/radar_opera.py header).
  Current job archives composite point-series JSON to R2 every 15 min.
- **Full model grids are NOT ingested in Phase 1** (deliberate — Postgres
  row-per-cell does not scale; grids go to R2 as zarr/COG in Phase 3 when the
  fusion API and map tiles need them).
- **Attribution obligations**: Keskkonnaagentuur (link to ilmateenistus.ee),
  meteo.lt (CC BY-SA 4.0), LVĢMC (CC0, attribution courteous), EUMETNET OPERA
  (CC BY 4.0), IEM. Put these in any public-facing UI.

## Phase 2 next steps (not in this repo yet)

- UM (meteo.pl) scraper at the 12 METAR points — after/alongside contacting
  `meteo@icm.edu.pl` about licensed access. Constraints in requirements doc §5.
- EE historical bulk archive investigation.
- True cloud-layer profiles from pressure-level cloud cover.
