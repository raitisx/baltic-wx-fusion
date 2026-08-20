"""Throwaway probe: what does the MEPS OPeNDAP dataset actually publish?

We already read cloud_base_altitude etc. from meps_det_2_5km (mepslatest). To add
a cloud TOP we need to know whether MEPS carries a cloud-top field directly, or
whether we have to derive it from 3D cloud on model levels (a separate meps_det_ml
dataset). This lists the catalog and dumps the variable inventory so we can decide.
Run via the meps-probe workflow, read the logs, then delete this + the workflow.
"""
import re
import sys

sys.path.insert(0, ".")
import netCDF4 as nc  # noqa: E402
from wxfusion import maps_meps as M  # noqa: E402
from wxfusion.http import session  # noqa: E402

# 1) What datasets does the mepslatest catalog expose (pp, sfc, ml…)?
print("=== CATALOG urlPath entries (mepslatest) ===")
try:
    txt = session().get(M.CATALOG, timeout=60).text
    for u in sorted(set(re.findall(r'urlPath="([^"]+)"', txt))):
        print("  ", u)
except Exception as e:
    print("catalog fetch failed:", e)

# 2) Open the latest deterministic pp dataset and dump every variable.
name = M.list_runs()[-1]
url = M.DAP_BASE + name
print("\n=== OPENING", url, "===")
ds = nc.Dataset(url)
print("dims:", {k: len(v) for k, v in ds.dimensions.items()})
print("\n=== ALL VARIABLES ===")
for k, v in ds.variables.items():
    print(f"{k} | dims={v.dimensions} | std={getattr(v,'standard_name','')} "
          f"| long={getattr(v,'long_name','')} | units={getattr(v,'units','')}")

print("\n=== CLOUD / TOP / HEIGHT / CONDENSATE MATCHES ===")
terms = ["cloud", "top", "ceiling", "condens", "base", "hybrid",
         "pressure", "geopotential", "height"]
for k, v in ds.variables.items():
    blob = (k + " " + getattr(v, "standard_name", "") + " "
            + getattr(v, "long_name", "")).lower()
    if any(t in blob for t in terms):
        print("MATCH", k, "| std=", getattr(v, "standard_name", ""),
              "| long=", getattr(v, "long_name", ""),
              "| dims=", v.dimensions)
print("\nDONE")
