"""Central configuration. Everything overridable via environment variables."""
import os

# --- Storage ---------------------------------------------------------------
# Postgres connection string (Supabase pooler URI recommended for CI runners):
#   postgresql://postgres.<project-ref>:<password>@aws-0-eu-west-1.pooler.supabase.com:6543/postgres
DB_URL = os.environ.get("SUPABASE_DB_URL", "")

# R2 (S3-compatible). Only needed by the radar job.
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET = os.environ.get("R2_BUCKET", "baltic-wx")
# Public read path for the bucket. Used where we consume our own imagery
# (radar verification) rather than write it, so no credentials are needed.
MAPS_BASE = os.environ.get(
    "MAPS_BASE", "https://pub-29a41af0b6de4fe9a0d144b6a88fa144.r2.dev")

# --- Identification --------------------------------------------------------
# Honest UA on every request. Put a reachable contact address here.
USER_AGENT = os.environ.get(
    "WX_USER_AGENT",
    "baltic-wx-fusion/0.1 (contact: raitis.upmalis@gmail.com)",
)

# --- Domain ----------------------------------------------------------------
# Baltic bbox (lat_min, lat_max, lon_min, lon_max)
BBOX = (53.5, 60.5, 20.0, 30.0)

# Open-Meteo model identifiers to ingest. Keys are our canonical model names
# stored in wx.forecasts.model; values are Open-Meteo `models=` identifiers.
OPENMETEO_MODELS = {
    "icon": "icon_seamless",
    "meps": "metno_seamless",
    "ifs": "ecmwf_ifs025",
    "gfs": "gfs_seamless",
    # UKMO global 10 km. `ukmo_seamless` would splice the 2 km UK domain in
    # front, but that domain does not reach the Baltic — measured at Riga the
    # seamless series is identical to ukmo_global_deterministic_10km for all
    # 168 h — so here it is simply a second global model, finer than GFS.
    "ukmo": "ukmo_seamless",
}

# Canonical parameter names (used in wx.forecasts and wx.observations):
#   t2m °C | td2m °C | rh % | ws10m m/s | wg10m m/s | wdir deg |
#   prcp_1h mm | pres_msl hPa | cc_total % | cc_low % | cc_mid % | cc_high % |
#   vis m | cb_lcl m AGL (model-derived) | cb_ceiling m AGL (METAR BKN/OVC) |
#   wx_rain 0/1 (precip present-weather flag)

# Forecast horizon requested from Open-Meteo (days)
FORECAST_DAYS = 4  # 0-72 h operational + margin

# LVGMC (data.gov.lv CKAN datastore) resource ids — dataset
# 'hidrometeorologiskie-noverojumi', CC0.
LVGMC_RES_OPERATIVE = "17460efb-ae99-4d1d-8144-1068f184b05f"  # last hour
LVGMC_RES_ARCHIVE_FAKT = "339f73e4-20cf-4cea-be65-dcfd4b3b742c"  # 365 d actual
LVGMC_RES_STATIONS = "c32c7afd-0d05-44fd-8b24-1de85b4bf11d"

# EE real-time XML feed (Keskkonnaagentuur). Attribution required.
EE_OBS_URL = "https://ilmateenistus.ee/ilma_andmed/xml/observations.php"

# meteo.lt API (CC BY-SA 4.0). 180 req/min, 20k/day.
METEOLT_BASE = "https://api.meteo.lt/v1"

# IEM METAR
IEM_BASE = "https://mesonet.agron.iastate.edu"

# OPERA / EUMETNET Open Radar Data API (CC BY 4.0)
ORD_BASE = os.environ.get(
    "ORD_BASE", "https://api.meteogate.eu/eu-eumetnet-weather-radar"
)
