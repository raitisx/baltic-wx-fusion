"""Prototype: per-layer cloud tops from Open-Meteo pressure-level cloud cover.

For a point, pull cloud_cover + geopotential_height at every pressure level for
each model, then walk the column bottom-up and split it into distinct decks
(contiguous runs of cloudy levels, one clear level tolerated as a bridge). Each
deck yields a base and a top height (m AMSL). Prints the raw profile and the
detected decks for a few hours so we can eyeball whether this is worth wiring
into the meteogram. Throwaway."""
import json
import urllib.request

LAT, LON = 56.9496, 24.1052          # Riga
TARGETS = ["2026-08-21T06:00", "2026-08-21T12:00", "2026-08-21T18:00"]
MODELS = {"icon": "icon_seamless", "ifs": "ecmwf_ifs025",
          "gfs": "gfs_seamless", "ukmo": "ukmo_seamless", "meps": "metno_seamless"}
LEVELS = [1000, 975, 950, 925, 900, 850, 800, 700, 600, 500, 400, 300, 250, 200]

DECK_COVER = 40      # % at a level to call it cloudy (scattered-or-more)
BRIDGE = 1           # clear levels tolerated inside one deck

hvars = []
for L in LEVELS:
    hvars.append(f"cloud_cover_{L}hPa")
    hvars.append(f"geopotential_height_{L}hPa")

u = ("https://api.open-meteo.com/v1/forecast"
     f"?latitude={LAT}&longitude={LON}"
     f"&hourly={','.join(hvars)}"
     f"&models={','.join(MODELS.values())}"
     "&forecast_days=3&timezone=UTC")
d = json.load(urllib.request.urlopen(u, timeout=90))
h = d["hourly"]
times = h["time"]


def val(var, om, t):
    arr = h.get(f"{var}_{om}") or h.get(var)
    if not arr:
        return None
    try:
        v = arr[times.index(t)]
    except ValueError:
        return None
    return v


def decks(profile):
    """profile: list of (height_m, cover_pct) sorted by height ascending.
    Returns list of (base_m, top_m, peak_cover) for each detected deck."""
    out = []
    base = top = peak = None
    gap = 0
    for hgt, cov in profile:
        cloudy = cov is not None and cov >= DECK_COVER
        if cloudy:
            if base is None:
                base = hgt
            top = hgt
            peak = cov if peak is None else max(peak, cov)
            gap = 0
        elif base is not None:
            gap += 1
            if gap > BRIDGE:
                out.append((base, top, peak))
                base = top = peak = None
                gap = 0
    if base is not None:
        out.append((base, top, peak))
    return out


for t in TARGETS:
    print(f"\n================ {t}Z  Riga ================")
    for name, om in MODELS.items():
        prof = []
        for L in LEVELS:
            cov = val(f"cloud_cover_{L}hPa", om, t)
            hgt = val(f"geopotential_height_{L}hPa", om, t)
            if hgt is not None:
                prof.append((hgt, cov))
        if not prof:
            print(f"  {name:5s} — no pressure-level data")
            continue
        prof.sort(key=lambda p: p[0])
        strip = " ".join(f"{int(hgt)}m:{'' if cov is None else int(cov)}" for hgt, cov in prof)
        ds = decks(prof)
        dstr = "  ".join(f"[{int(b)}-{int(tp)}m {int(pk)}%]" for b, tp, pk in ds) or "clear"
        print(f"  {name:5s} decks: {dstr}")
        print(f"        prof: {strip}")
print("\nDONE")
