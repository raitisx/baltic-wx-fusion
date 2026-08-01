"""Reference implementation of the dew model the meteogram draws.

The question is an energy budget, not a threshold. Dew is a film of liquid
water on the surface; it goes away when enough energy has arrived to evaporate
it. Solar elevation matters because it sets the irradiance, but what clears the
dew is the *integral* of that irradiance since sunrise, not the instantaneous
angle -- so a fixed "35 degrees" rule is really a summer-latitude shorthand for
"by now enough energy has arrived".

The page carries a JavaScript port of this; the two agree exactly on a year of
reanalysis at Riga (101 dewy mornings of 359, 11 with frost, median deposit
0.17 mm). This copy exists so the calibration can be re-run and argued with.

How each piece was pinned down, all at Riga:

  solar elevation   NOAA low-precision. 56.51 deg at the June solstice against
                    an almanac 56.5, 9.64 against 9.6 in December.
  irradiance        fitted to a year of hourly reanalysis shortwave, n=4112:
                    bias 0 W/m2, rmse 116 W/m2. Daily totals rmse 19%.
  surface cooling   fitted to 398 night hours of modelled surface temperature,
                    bias -0.05 K, rmse 0.92 K -- then multiplied by GRASS,
                    because a canopy is cut off from the soil under it and
                    cools further than the bare surface the model reports.
  deposition        not fitted. Set from measurement: 0.14 +/- 0.12 mm per
                    night over grassland, dew on 25 +/- 14% of nights at humid
                    temperate sites (NEON radiometer network, HESS 23, 1179).
                    This model gives 0.17 mm and 28%.

What is NOT established: that the burn-off hour beats a flat "three hours after
sunrise" on a clear summer morning. A superposed-epoch test over 13 such
mornings shows the observed warming rate stepping from 0.18 to 1.26 K/h across
the predicted hour, against +0.15 K/h two to three hours either side -- so the
timing is not wrong, but a fixed lag scored +0.99 on the same sample and the
test cannot separate them. The model earns its keep in the conditions that
sample cannot reach: October against June, overcast against clear.
"""
import math
import datetime as dt

LAT, LON = 56.92, 23.97          # EVRA, Riga
LAMBDA = 2.45e6                  # J/kg, latent heat of vaporisation
LAMBDA_S = 2.83e6                # J/kg, sublimation (frost)


# ---------------------------------------------------------------- solar
def solar_elev(when: dt.datetime, lat=LAT, lon=LON) -> float:
    """Solar elevation in degrees. NOAA's low-precision algorithm; good to
    about 0.1 deg, which is far finer than anything else here."""
    d = when.replace(tzinfo=dt.timezone.utc)
    # fractional julian day
    jd = d.timestamp() / 86400.0 + 2440587.5
    n = jd - 2451545.0
    L = (280.460 + 0.9856474 * n) % 360            # mean longitude
    g = math.radians((357.528 + 0.9856003 * n) % 360)   # mean anomaly
    lam = math.radians(L + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
    eps = math.radians(23.439 - 0.0000004 * n)
    dec = math.asin(math.sin(eps) * math.sin(lam))
    # equation of time, minutes
    y = math.tan(eps / 2) ** 2
    Lr = math.radians(L)
    eot = 4 * math.degrees(
        y * math.sin(2 * Lr) - 2 * 0.0167 * math.sin(g)
        + 4 * 0.0167 * y * math.sin(g) * math.cos(2 * Lr)
        - 0.5 * y * y * math.sin(4 * Lr) - 1.25 * 0.0167 ** 2 * math.sin(2 * g))
    tst = (d.hour * 60 + d.minute + d.second / 60 + eot + 4 * lon) % 1440
    ha = math.radians(tst / 4 - 180)
    latr = math.radians(lat)
    sin_h = (math.sin(latr) * math.sin(dec)
             + math.cos(latr) * math.cos(dec) * math.cos(ha))
    return math.degrees(math.asin(max(-1, min(1, sin_h))))


# ---------------------------------------------------------------- physics
def svp(t_c: float) -> float:
    """Saturation vapour pressure, kPa (Tetens)."""
    return 0.6108 * math.exp(17.27 * t_c / (t_c + 237.3))


def delta_svp(t_c: float) -> float:
    """d(svp)/dT, kPa/K."""
    return 4098 * svp(t_c) / (t_c + 237.3) ** 2


GAMMA = 0.0665   # psychrometric constant, kPa/K, near sea level
GRASS = 1.3      # grass canopy cools this much more than the bare surface
DEW_CAP = 0.20   # mm, no more free water than a canopy will actually hold


def cooling(cc_pct: float, ws: float) -> float:
    """How far the surface falls below screen temperature overnight, K.

    Clear and calm, grass runs 4-6 K colder than the thermometer screen;
    overcast or windy, the difference collapses. This is the whole reason dew
    appears while the screen still reads several degrees above its dew point.
    """
    cc = max(0.0, min(100.0, cc_pct)) / 100.0
    # Shape and coefficients fitted to 398 night hours of ICON surface
    # temperature at Riga (bias -0.05 K, rmse 0.92 K), then scaled up by
    # GRASS: a grass canopy is thermally cut off from the soil beneath it and
    # runs colder than the bare surface the model reports -- the classic
    # grass-minimum reading. That extra is what puts dew on a field while the
    # screen thermometer still sits above its dew point.
    return GRASS * 3.0 * (1 - 0.40 * cc) * math.exp(-ws / 4.0)


def irradiance(elev_deg: float, cc_pct: float) -> float:
    """Global horizontal irradiance, W/m^2."""
    if elev_deg <= 0:
        return 0.0
    clear = 1020 * math.sin(math.radians(elev_deg)) - 20
    if clear <= 0:
        return 0.0
    cc = max(0.0, min(100.0, cc_pct)) / 100.0
    return clear * (1 - 0.62 * cc ** 4)


def evap_rate(t_c, td_c, ws, cc, elev):
    """Evaporation from a wet surface, mm/h (Penman, split into its two terms).

    The radiation term is what the sun does. The aerodynamic term is what dry
    moving air does, and it is why dew can clear slowly on a breezy overcast
    morning with no sun worth speaking of.
    """
    g = irradiance(elev, cc)
    rn = 0.75 * g - (45 if elev > 0 else -25)   # net radiation over grass
    d = delta_svp(t_c)
    w = d / (d + GAMMA)
    vpd = max(0.0, svp(t_c) - svp(td_c))
    le_rad = w * rn
    # 6.43(1+0.536u)*VPD is MJ/m2/day; 1 MJ/m2/day = 11.574 W/m2
    le_aero = (1 - w) * 6.43 * (1 + 0.536 * ws) * vpd * 11.574
    le = le_rad + le_aero
    if le <= 0:
        return 0.0
    return le / LAMBDA * 3600 * 1000 / 1000      # W/m2 -> mm/h


def dew_rate(t_c, td_c, cc, ws):
    """Dew deposition, mm/h. Zero unless the surface reaches its dew point."""
    dt_cool = cooling(cc, ws)
    depression = t_c - td_c
    if depression >= dt_cool:
        return 0.0, dt_cool
    # Deeper below the threshold -> faster deposition. The ceiling is set by
    # the measured yield: 0.14 +/- 0.12 mm per night over grassland across the
    # NEON radiometer network, which a whole night at 0.05 mm/h reproduces.
    frac = min(1.0, (dt_cool - depression) / max(dt_cool, 0.5))
    return 0.028 * frac, dt_cool


def run(hours):
    """hours: list of dicts with ts (utc datetime), t2m, td2m, ws10m, cc_total,
    prcp (mm in the following hour). Returns the same rows with water film."""
    water = 0.0     # mm on the surface
    frost = False
    out = []
    for h in hours:
        elev = solar_elev(h["ts"])
        night = elev < 3.0
        dr, dt_cool = dew_rate(h["t2m"], h["td2m"], h["cc_total"], h["ws10m"])
        surf_t = h["t2m"] - (dt_cool if night else 0.0)
        if night and dr > 0:
            water = min(DEW_CAP, water + dr)
            if surf_t < 0:
                frost = True
        if (h.get("prcp") or 0) > 0.1:
            water = max(water, min(0.5, h["prcp"]))
            frost = False
        if water > 0:
            er = evap_rate(h["t2m"], h["td2m"], h["ws10m"], h["cc_total"], elev)
            if frost:
                er *= LAMBDA / LAMBDA_S          # sublimation costs more
                if h["t2m"] > 0.5:
                    frost = False                # melts, then evaporates
            water = max(0.0, water - er)
            if water == 0.0:
                frost = False
        out.append({**h, "elev": elev, "water": water, "frost": frost,
                    "dt_cool": dt_cool, "surf_t": surf_t})
    return out
