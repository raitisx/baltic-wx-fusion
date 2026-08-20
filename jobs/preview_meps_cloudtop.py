"""Preview MEPS cloud_top_altitude over the Baltic as a coloured height field.

MEPS publishes cloud_top_altitude directly (time, surface, y, x) — same shape as
the cloud_base_altitude we already render. This throwaway renders one lead hour
and prints it as base64 (@@SNAP markers) so we can eyeball it before wiring the
full pipeline. Delete alongside the probe once the real layer lands.
"""
import base64
import io
import json
import re
import sys

sys.path.insert(0, ".")
import numpy as np  # noqa: E402
import netCDF4 as nc  # noqa: E402
import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.colors import BoundaryNorm, ListedColormap  # noqa: E402
from wxfusion import maps_meps as M  # noqa: E402
from wxfusion import proj3059 as P  # noqa: E402

LEAD = 6  # forecast hour from run start (hourly steps, time[0] = run)

name = M.list_runs()[-1]
run_tag = re.search(r"(\d{8}T\d{2})Z", name).group(1)
print("run", run_tag, "lead +%dh" % LEAD)
ds = nc.Dataset(M.DAP_BASE + name)
lat = np.array(ds.variables["latitude"][:])
lon = np.array(ds.variables["longitude"][:])
mk = (lat >= M.BBOX[0]) & (lat <= M.BBOX[1]) & (lon >= M.BBOX[2]) & (lon <= M.BBOX[3])
ys, xs = np.where(mk)
y0, y1, x0, x1 = ys.min(), ys.max(), xs.min(), xs.max()
latw = lat[y0:y1 + 1, x0:x1 + 1]
lonw = lon[y0:y1 + 1, x0:x1 + 1]

ct = np.array(ds.variables["cloud_top_altitude"][LEAD, 0, y0:y1 + 1, x0:x1 + 1], dtype=float)
# Clear sky comes back as a huge fill (~1e30); mask it and anything above the
# troposphere so the colour scale isn't blown out.
ct = np.where(np.isfinite(ct) & (ct > 0) & (ct < 20000), ct, np.nan)
finite = np.isfinite(ct)
print("cloud-top: cover %.0f%%  min %.0f  median %.0f  max %.0f m" % (
    100 * finite.mean(),
    np.nanmin(ct) if finite.any() else -1,
    np.nanmedian(ct) if finite.any() else -1,
    np.nanmax(ct) if finite.any() else -1))

xw, yw = P.to_xy(lonw, latw)
borders = json.load(open(M.BORDERS_FILE))

# Height ramp (m): low tops blue-grey, mid neutral, high convective tops warm —
# reads a bit like a satellite cloud-top-height product.
levels = [0, 500, 1000, 2000, 3000, 4000, 6000, 8000, 10000, 13000]
colors = ["#6b8fb5", "#86a8ca", "#aac6de", "#d3e2ef", "#eef3f8",
          "#f7ead0", "#f0c88c", "#e59a58", "#d3673d"]
cmap = ListedColormap(colors)
norm = BoundaryNorm(levels, cmap.N)

fig = plt.figure(figsize=(P.W / 100, P.H / 100), dpi=100)
ax = fig.add_axes([0, 0, 1, 1])
ax.set_facecolor("#fbf3de")
ax.set_xlim(P.X0, P.X1)
ax.set_ylim(P.Y0, P.Y1)
ax.axis("off")
try:
    M._draw_sea(ax)   # needs R2; no-ops to cream sea without creds
except Exception as e:
    print("sea layer skipped:", type(e).__name__)
ax.pcolormesh(xw, yw, ct, cmap=cmap, norm=norm, shading="auto", zorder=2)
for c in borders:
    for line in c["lines"]:
        bx, by = P.to_xy([lo for lo, _ in line], [la for _, la in line])
        ax.plot(bx, by, color="#55503f", lw=0.8, zorder=5)

buf = io.BytesIO()
fig.savefig(buf, dpi=100, format="png", facecolor="#fbf3de")
b = base64.b64encode(buf.getvalue()).decode()
print("@@SNAP_BEGIN CTOP %d" % len(b))
print(b)
print("@@SNAP_END CTOP")
print("DONE")
