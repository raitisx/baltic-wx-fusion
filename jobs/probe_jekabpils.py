"""One-off: per-model cloud numbers at Jekabpils for a target hour, exactly as
the page computes them (LCL = 125*(T-Td), ceiling gate cc_low >= 60). Shows why
the fused ceiling reads 'none' when there is scattered low cloud. Throwaway."""
import json
import urllib.request

LAT, LON = 56.4986, 25.8578          # Jekabpils
TARGETS = ["2026-08-20T08:00", "2026-08-20T09:00", "2026-08-20T10:00",
           "2026-08-20T11:00", "2026-08-20T12:00"]
OM_MODELS = {"icon": "icon_seamless", "meps": "metno_seamless",
             "ifs": "ecmwf_ifs025", "gfs": "gfs_seamless", "ukmo": "ukmo_seamless"}
HVARS = ["temperature_2m", "dew_point_2m", "cloud_cover_low",
         "cloud_cover_mid", "cloud_cover_high"]

u = ("https://api.open-meteo.com/v1/forecast"
     f"?latitude={LAT}&longitude={LON}"
     f"&hourly={','.join(HVARS)}"
     f"&models={','.join(OM_MODELS.values())}"
     "&past_days=2&forecast_days=3&timezone=UTC")
d = json.load(urllib.request.urlopen(u, timeout=60))
h = d["hourly"]
times = h["time"]

def get(var, om, t):
    arr = h.get(f"{var}_{om}") or h.get(var)
    if not arr:
        return None
    try:
        return arr[times.index(t)]
    except ValueError:
        return None

OLD, NEW = 60, 38   # BKN (5 oktas) vs SCT (3 oktas)
for t in TARGETS:
    print(f"\n=== {t}Z  Jekabpils ===")
    wtot = 0
    wceil = {OLD: 0, NEW: 0}
    for name, om in OM_MODELS.items():
        t2 = get("temperature_2m", om, t)
        td = get("dew_point_2m", om, t)
        cl = get("cloud_cover_low", om, t)
        cm = get("cloud_cover_mid", om, t)
        ch = get("cloud_cover_high", om, t)
        lcl = None if (t2 is None or td is None) else max(0, 125 * (t2 - td))
        if lcl is not None:
            wtot += 1
            for thr in (OLD, NEW):
                if cl is not None and cl >= thr:
                    wceil[thr] += 1
        c60 = "YES" if (cl is not None and cl >= OLD) else "no"
        c38 = "YES" if (cl is not None and cl >= NEW) else "no"
        print(f"  {name:5s} ccL={cl!s:>4} ccM={cm!s:>4} ccH={ch!s:>4} "
              f"LCL={'' if lcl is None else round(lcl)}m  "
              f"ceil@60%={c60:3s}  ceil@38%={c38}")
    for thr in (OLD, NEW):
        agree = (wceil[thr] / wtot) if wtot else None
        shown = (agree is not None and agree >= 0.5)
        print(f"  -> @{thr}%: vote {wceil[thr]}/{wtot} = {agree}  => fused "
              f"ceiling: {'SHOWN' if shown else 'NONE (<0.5)'}")
print("\nDONE")
