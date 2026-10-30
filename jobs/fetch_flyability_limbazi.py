"""Hourly weather history for Limbaži, Sep-Oct 2026, for a flight-availability table.

Runs in CI because the dev sandbox has no egress to weather APIs.

Two sources, each with a job:

  * Open-Meteo's forecast endpoint with past_days. Over Latvia this is
    ICON-EU/ICON-D2 analysis at 2-7 km, finer than ERA5's 25 km, and one
    request covers the whole window with consistent provenance. Gives temp,
    dew point, precipitation, low/total cloud and 10 m gusts, plus sunrise
    and sunset per day. This is the table's data.

  * EVRA (Riga) METAR, 80 km SW. Too far to plan from, but it reports a
    REAL ceiling, and the table has to derive cloud base from the
    temperature/dew-point spread. Fetched so that derivation can be checked
    against something measured rather than trusted on its own.

Writes CSVs next to itself for the spreadsheet build to pick up.
"""
import csv
import io
import json
import os
import sys
import urllib.parse
import urllib.request

LAT, LON = 57.5136, 24.7186          # Limbaži
OUT = sys.argv[1] if len(sys.argv) > 1 else "data/flyability"
HOURLY = ["temperature_2m", "dew_point_2m", "relative_humidity_2m",
          "precipitation", "rain", "snowfall", "cloud_cover",
          "cloud_cover_low", "cloud_cover_mid", "wind_speed_10m",
          "wind_gusts_10m", "wind_direction_10m", "weather_code"]


def get(url, timeout=180):
    req = urllib.request.Request(url, headers={"User-Agent": "baltic-wx-fusion"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def openmeteo():
    """One call, past_days back to the end of July, three days ahead."""
    q = urllib.parse.urlencode({
        "latitude": LAT, "longitude": LON,
        "hourly": ",".join(HOURLY),
        "daily": "sunrise,sunset",
        "timezone": "Europe/Riga",
        "past_days": 92, "forecast_days": 3,
        "wind_speed_unit": "ms",
    })
    d = json.loads(get(f"https://api.open-meteo.com/v1/forecast?{q}"))
    h = d["hourly"]
    rows = []
    for i, t in enumerate(h["time"]):
        rows.append({"time": t, **{k: h[k][i] for k in HOURLY}})
    daily = [{"date": t, "sunrise": a, "sunset": b} for t, a, b
             in zip(d["daily"]["time"], d["daily"]["sunrise"],
                    d["daily"]["sunset"])]
    print(f"open-meteo: {len(rows)} hours {rows[0]['time']} .. "
          f"{rows[-1]['time']}, {len(daily)} days")
    print(f"  units: {d.get('hourly_units', {})}")
    return rows, daily


def metar_evra():
    """EVRA ceiling/gusts from the Iowa State ASOS archive."""
    q = urllib.parse.urlencode([
        ("station", "EVRA"), ("data", "tmpf"), ("data", "dwpf"),
        ("data", "sknt"), ("data", "gust"), ("data", "p01i"),
        ("data", "skyc1"), ("data", "skyl1"), ("data", "skyc2"),
        ("data", "skyl2"), ("data", "skyc3"), ("data", "skyl3"),
        ("year1", 2026), ("month1", 8), ("day1", 30),
        ("year2", 2026), ("month2", 11), ("day2", 2),
        ("tz", "Etc/UTC"), ("format", "onlycomma"), ("latlon", "no"),
        ("missing", ""), ("trace", "0.001"), ("direct", "no"),
        ("report_type", 3), ("report_type", 4),
    ])
    url = ("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?" + q)
    txt = get(url).decode("utf-8", "replace")
    rows = list(csv.DictReader(io.StringIO(txt)))
    print(f"metar EVRA: {len(rows)} reports")
    return rows


os.makedirs(OUT, exist_ok=True)


def write(name, rows, fields=None):
    if not rows:
        print(f"  !! {name}: nothing to write")
        return
    p = os.path.join(OUT, name)
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields or list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"  wrote {p}  ({len(rows)} rows, {os.path.getsize(p):,} B)")


hourly, daily = openmeteo()
write("limbazi_hourly.csv", hourly, ["time"] + HOURLY)
write("limbazi_daily.csv", daily, ["date", "sunrise", "sunset"])
try:
    write("evra_metar.csv", metar_evra())
except Exception as e:
    print(f"metar EVRA failed ({type(e).__name__}: {e}) — "
          "the table can still be built, its cloud base just goes unchecked")
