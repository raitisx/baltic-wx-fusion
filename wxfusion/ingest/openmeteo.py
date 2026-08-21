"""Forecast ingest: Open-Meteo multi-model station-point extracts.

One request per batch of points fetches all configured models at once
(Open-Meteo suffixes variables with the model id). Full grids intentionally
NOT ingested in Phase 1 — see README.

run_time: Open-Meteo does not expose which model run a value came from, so we
deduce it from the fetch time and each centre's published schedule — see
model_cycle() at the foot of this module. Stamping the fetch hour instead
(what we did originally) smeared the lead-time buckets by up to 10 hours,
which is exactly what the skill weighting is binned on.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import time

from .. import config
from ..http import session

log = logging.getLogger(__name__)

HOURLY_VARS = {
    "temperature_2m": "t2m",
    "dew_point_2m": "td2m",
    "relative_humidity_2m": "rh",
    "precipitation": "prcp_1h",
    "wind_speed_10m": "ws10m",
    "wind_gusts_10m": "wg10m",
    "wind_direction_10m": "wdir",
    "pressure_msl": "pres_msl",
    "cloud_cover": "cc_total",
    "cloud_cover_low": "cc_low",
    "cloud_cover_mid": "cc_mid",
    "cloud_cover_high": "cc_high",
    # Fog has an intensity, and this is it. Open-Meteo serves visibility for
    # icon, metno (MEPS) and gfs; ecmwf_ifs025 returns nulls, so the fused
    # value is a three-model mean where it exists at all.
    "visibility": "vis",
}

# The subset the FUSED MAP actually draws: cloud (all four layers), the moisture
# that sets base and fog (t2m, td2m, rh), rain, and visibility. No wind, no
# pressure — the map has no arrows — which drops the per-point Open-Meteo weight
# from ~6.5 to ~4.5 and is what lets the background fill grid fit the free tier.
# t2m/td2m/rh/cc_low must stay: the ceiling (cb) and LCL are derived from them.
MAP_HOURLY_VARS = {
    "temperature_2m": "t2m",
    "dew_point_2m": "td2m",
    "relative_humidity_2m": "rh",
    "precipitation": "prcp_1h",
    "cloud_cover": "cc_total",
    "cloud_cover_low": "cc_low",
    "cloud_cover_mid": "cc_mid",
    "cloud_cover_high": "cc_high",
    "visibility": "vis",
}


# Low-cloud fraction at or above which a ceiling is deemed to exist. 38% is the
# SCT threshold aviation uses (3 oktas). A pure-METAR fit prefers the BKN
# threshold (60%, 5 oktas — cuts mean absolute error from ~920 m to ~530 m for
# ICON and ~918 m to ~559 m for MEPS), but the meteogram is a flight-planning
# aid and we don't fly through scattered cloud either, so scattered low cloud
# is reported as a ceiling deliberately — a conservative floor, not the best
# statistical fit. Keep this in step with CEILING_COVER on the page.
CEILING_COVER = float(os.environ.get("CEILING_COVER", "38"))

# Fog is a ceiling on the ground, and the low-cloud fraction does not see it.
# At 56.57N 26.40E on 29 July 04:00 UTC, MEPS had relative humidity 100% with
# temperature and dew point both 11.4 — saturated at the surface, LCL zero —
# and cloud_cover_low of 0. The gate above therefore reported no ceiling,
# while MEPS's own map painted the cell as fog and its fog_area_fraction
# agreed. That is the meteogram/map disagreement in one cell: not a
# difference of models, a condition the cover test cannot express.
FOG_RH = float(os.environ.get("FOG_RH", "97"))
FOG_DEWPOINT_DEP = float(os.environ.get("FOG_DEWPOINT_DEP", "0.3"))


# --- model cycle estimation -------------------------------------------------
# Open-Meteo does not tell us which model run a value came from, so we used to
# stamp run_time = the hour we fetched. That is wrong by hours and it smears
# the lead-time buckets that the whole skill weighting rests on: a "6 hour
# ahead" forecast fetched at 04:00 from the 00Z run is really 6 h ahead of the
# run, but if IFS's 00Z run only published at 07:00 we cannot have had it yet.
#
# Each centre runs on a fixed schedule and publishes after a fairly stable
# delay, so the newest run we *could* have received is deducible from the
# fetch time. Still an estimate, but hours closer than pretending fetch time
# is run time.
CYCLE_HOURS = {"icon": 6, "meps": 3, "ifs": 6, "gfs": 6, "ukmo": 6}
# UKMO out of its own meta.json on 02.08: 06Z run initialised, available 13Z —
# 7.2 h, the same shape as IFS, so it takes the same rounded-up 8.
PUBLISH_DELAY_H = {"icon": 4, "meps": 2, "ifs": 8, "gfs": 5, "ukmo": 8}


def model_cycle(model: str, fetched_at: dt.datetime) -> dt.datetime:
    """Newest run of `model` that could already have been published."""
    cycle = CYCLE_HOURS.get(model, 6)
    delay = PUBLISH_DELAY_H.get(model, 5)
    t = fetched_at - dt.timedelta(hours=delay)
    return t.replace(minute=0, second=0, microsecond=0,
                     hour=(t.hour // cycle) * cycle)


def lcl_height(t2m: float, td2m: float) -> float:
    """Lifted condensation level in m AGL (Espy approximation)."""
    return max(0.0, 125.0 * (t2m - td2m))


# Cloud TOP — the height of the lowest cloudy deck, for the meteogram's top
# line. Derived from the pressure-level cloud-cover profile so it is a
# multi-model quantity, fused the same way the ceiling (cb) is, instead of the
# single-model MEPS field it replaces (which went blank whenever MEPS was the
# dry model while the fused base showed cloud). Standard-atmosphere geopotential
# heights (m AMSL) stand in for Open-Meteo's own geopotential_height — they
# agree to a few metres over the Baltic lowland, and skipping that field halves
# the request weight this adds. Levels stop at 700 hPa (~3 km): the meteogram's
# cloud-top band caps there, so a deck reaching 700 hPa already pegs at the top.
# Pressure levels sampled for the deck top. 1000-700 hPa (to ~3 km) is what the
# meteogram's band draws; 600-300 hPa carry on up so the TRUE top of a deep deck
# is known — the graph pegs the line at 3 km but the tooltip shows the real
# height from here (a rain deck tops out near 7-9 km, not 3). 975/925 and the
# levels above 300 are skipped as close enough to their neighbours.
CTOP_LEVEL_M = {1000: 111, 950: 540, 900: 988, 850: 1457, 800: 1949, 700: 3012,
                600: 4206, 500: 5574, 400: 7185, 300: 9164}
CTOP_VARS = [f"cloud_cover_{L}hPa" for L in sorted(CTOP_LEVEL_M)]


def low_deck_top(cover_by_level: dict) -> float | None:
    """Top height (m AMSL) of the lowest cloudy deck, or None if there is none.

    Walks the profile from the ground up; the first contiguous run of levels
    with cloud cover >= CEILING_COVER (one clear level tolerated as a bridge)
    is the low deck, and the highest cloudy level in it is the top. Same cover
    threshold as the ceiling, so a top exists on the footing the base does."""
    prof = sorted((CTOP_LEVEL_M[L], c) for L, c in cover_by_level.items()
                  if L in CTOP_LEVEL_M)
    base = top = None
    gap = 0
    for hgt, cov in prof:
        if cov is not None and cov >= CEILING_COVER:
            if base is None:
                base = hgt
            top = hgt
            gap = 0
        elif base is not None:
            gap += 1
            if gap > 1:      # two clear levels end the deck; one is bridged
                break
    return top


def fetch(points: list[dict], hourly_vars: dict | None = None) -> list[tuple]:
    """points: dicts with point_id/lat/lon. Returns wx.forecasts rows.

    hourly_vars selects which Open-Meteo fields to pull (defaults to the full
    HOURLY_VARS station set). The map's background grid passes MAP_HOURLY_VARS
    — the nine fields it draws — so its per-point weight, and thus its share of
    the free-tier budget, is smaller than a station's.
    """
    if not points:
        return []
    # The station set (default) also derives the cloud-top line from the
    # pressure-level cloud-cover profile; the map grid (MAP_HOURLY_VARS) does
    # not draw a top, so it skips the extra levels and keeps its lean weight.
    want_ctop = hourly_vars is None
    hourly_vars = hourly_vars or HOURLY_VARS
    fetched_at = dt.datetime.now(dt.timezone.utc)
    s = session()
    rows: list[tuple] = []
    def _ingest(point, loc):
        hourly = loc.get("hourly", {})
        times = [
            dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone.utc)
            for t in hourly.get("time", [])
        ]
        for our_model, om_model in config.OPENMETEO_MODELS.items():
            # Each centre is on its own schedule, so the run behind this data
            # differs per model even though we fetched them together.
            run_time = model_cycle(our_model, fetched_at)
            # collect per-variable arrays for this model
            series: dict[str, list] = {}
            for om_var, param in hourly_vars.items():
                arr = hourly.get(f"{om_var}_{om_model}") or hourly.get(om_var)
                if arr:
                    series[param] = arr
            # Pressure-level cloud cover for the cloud-top line (station set
            # only). Kept apart from `series` because it is not emitted raw —
            # only the derived cb_top is — so it never becomes a stored param.
            ctop_levels: dict[int, list] = {}
            if want_ctop:
                for lv in CTOP_LEVEL_M:
                    arr = (hourly.get(f"cloud_cover_{lv}hPa_{om_model}")
                           or hourly.get(f"cloud_cover_{lv}hPa"))
                    if arr:
                        ctop_levels[lv] = arr
            for i, valid in enumerate(times):
                t2m = td2m = cc_low = cc_mid = cc_high = rh = None
                for param, arr in series.items():
                    v = arr[i] if i < len(arr) else None
                    if v is None:
                        continue
                    rows.append(
                        (our_model, run_time, valid, point["point_id"], param, float(v))
                    )
                    if param == "t2m":
                        t2m = float(v)
                    elif param == "td2m":
                        td2m = float(v)
                    elif param == "cc_low":
                        cc_low = float(v)
                    elif param == "cc_mid":
                        cc_mid = float(v)
                    elif param == "cc_high":
                        cc_high = float(v)
                    elif param == "rh":
                        rh = float(v)
                have_ceiling = False
                if t2m is not None and td2m is not None:
                    lcl = lcl_height(t2m, td2m)
                    # Raw LCL, kept as a diagnostic.
                    rows.append(
                        (our_model, run_time, valid, point["point_id"], "cb_lcl", lcl)
                    )
                    # Ceiling — the quantity everything else means by "cloud
                    # base". An LCL is a condensation *level*: on a humid night
                    # it sits at 60 m whether or not any cloud forms, which is
                    # why the meteogram used to scream no-fly while the MEPS
                    # map showed clear sky. A ceiling only exists where the
                    # model also has broken-or-more low cloud. Absent means no
                    # ceiling below ~1 km, not missing data.
                    fog = ((rh is not None and rh >= FOG_RH)
                           or (t2m - td2m) <= FOG_DEWPOINT_DEP)
                    have_ceiling = (cc_low is not None and cc_low >= CEILING_COVER) or fog
                    if have_ceiling:
                        rows.append(
                            (our_model, run_time, valid, point["point_id"], "cb", lcl)
                        )
                # Cloud top of the lowest deck (m AMSL), for the meteogram's top
                # line. The page fuses it across models and keeps it consistent
                # with the fused base. Prefer the real deck from the pressure
                # profile; but Open-Meteo's aggregate cc_low and its
                # pressure-level cloud_cover disagree for some models (ICON most
                # often), so where there is a ceiling but the profile shows no
                # deck, fall back to the low band's top — pegged up if there is
                # mid/high cloud above it — rather than leave a base with no top.
                ct = low_deck_top({lv: (arr[i] if i < len(arr) else None)
                                   for lv, arr in ctop_levels.items()}) if ctop_levels else None
                if ct is None and have_ceiling:
                    hi = ((cc_mid is not None and cc_mid >= CEILING_COVER)
                          or (cc_high is not None and cc_high >= CEILING_COVER))
                    ct = 3012.0 if hi else 1949.0     # 700 hPa vs 800 hPa (low-band top)
                if ct is not None:
                    rows.append(
                        (our_model, run_time, valid, point["point_id"], "cb_top", float(ct))
                    )
    # Open-Meteo's free tier rate-limits per minute, and one pass is ~338
    # points x 5 models. Firing that as a few back-to-back requests bursts past
    # the limit and the whole batch comes back 429 (which is what broke the
    # ingest once national stations tripled the point count). Keep each request
    # small and pace them — ~50 points a request with a pause between, well
    # under the per-minute budget. Batching also keeps each URL short, so the
    # old 414 stays gone. A chunk that still fails after the session's own 429
    # backoff is skipped (its points get no forecast this pass) rather than
    # sinking the whole models run; only a total wipeout — nothing ingested at
    # all — re-raises, so a genuine outage still surfaces.
    MAX_POINTS_PER_REQUEST = 50
    PAUSE_S = 45
    got = 0
    last_err = None
    for start in range(0, len(points), MAX_POINTS_PER_REQUEST):
        chunk = points[start:start + MAX_POINTS_PER_REQUEST]
        if start:
            time.sleep(PAUSE_S)
        lat = ",".join(f"{p['lat']:.4f}" for p in chunk)
        lon = ",".join(f"{p['lon']:.4f}" for p in chunk)
        try:
            r = s.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "hourly": ",".join(list(hourly_vars)
                                       + (CTOP_VARS if want_ctop else [])),
                    "models": ",".join(config.OPENMETEO_MODELS.values()),
                    "forecast_days": config.FORECAST_DAYS,
                    "windspeed_unit": "ms",
                    "timezone": "UTC",
                },
                timeout=120,
            )
            r.raise_for_status()
            payload = r.json()
        except Exception as e:
            last_err = e
            log.warning("openmeteo: points %d-%d failed (%s) — skipping this chunk",
                        start, start + len(chunk), type(e).__name__)
            continue
        locs = payload if isinstance(payload, list) else [payload]
        for point, loc in zip(chunk, locs):
            _ingest(point, loc)
        got += len(chunk)
    if got == 0 and last_err is not None:
        raise last_err
    log.info("openmeteo: %d forecast rows (%d/%d points, %d models)",
             len(rows), got, len(points), len(config.OPENMETEO_MODELS))
    return rows
