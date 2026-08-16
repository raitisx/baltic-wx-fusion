"""Build the static geography grid for the map downscaler, and preview it.

One-off (re-runnable) job. Downloads the coastline and lake polygons, samples
elevation, bakes four rasters to the 1 km EPSG:3059 grid, stores them in R2 as
one .npz the downscaler will load, and prints each layer as a base64 PNG into
the job log so the geography can be eyeballed before any meteorology keys off
it.

Layers: land/sea/lake, signed coast distance (km), elevation (m), TPI (m).

Env: R2_* to store the .npz. STATIC_ELEV_STEP overrides the elevation sample
spacing (deg, default 0.1).
"""
import base64
import io
import logging
import os
import sys
import time

sys.path.insert(0, ".")
import numpy as np  # noqa: E402

from wxfusion import config, proj3059 as P, static_grid as sg  # noqa: E402
from wxfusion.http import session  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("build_static_grid")

NE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson"
LAND_URL = f"{NE}/ne_10m_land.geojson"
LAKES_URL = f"{NE}/ne_10m_lakes.geojson"
NPZ_KEY = "maps/static/static_3059.npz"
ELEV_STEP = float(os.environ.get("STATIC_ELEV_STEP") or "0.1")
# A real DEM beats the coarse elevation API for the lapse-rate/TPI layers.
# Preferred: a GeoTIFF in R2 at DEM_KEY (any CRS — rasterio reads it and we
# reproject the grid into it). DEM_URL fetches one over https instead. With
# neither, elevation falls back to the Open-Meteo elevation API.
# DEM sources, in priority order: a file in the repo (DEM_PATH), an https URL
# (DEM_URL), then an R2 object (DEM_KEY). The bundled Baltic DEM is 1 km in
# LKS-92 — the render grid's own CRS and resolution — so it is sampled with no
# reprojection at all.
DEM_PATH = os.environ.get("DEM_PATH") or "assets/dem/BALTIC_DEM_1KM.tif"
DEM_KEY = os.environ.get("DEM_KEY") or "assets/dem/baltic_dem.tif"
DEM_URL = os.environ.get("DEM_URL") or ""
# CRS to assume when the GeoTIFF has none embedded (national DEMs often don't).
DEM_CRS = os.environ.get("DEM_CRS") or "EPSG:3059"

# FABDEM V1-2: Copernicus GLO-30 with forest and building height removed — a
# global 30 m BARE-EARTH DTM (Hawker et al., Univ. of Bristol). Preferred so the
# uplands aren't inflated by ~50% forest canopy, and it covers the whole domain
# (Russia/Belarus included), which the provided Baltic DEM does not. Distributed
# as 10x10 deg zip bundles of 1 deg GeoTIFF tiles, EPSG:4326.
# LICENCE: CC BY-NC-SA 4.0 — non-commercial; commercial use via fabdem@fathom.global.
# Opt-in (USE_FABDEM=1): FABDEM's only host, data.bris, streams at ~1 MB/s, so a
# land bundle is a 20-30 min download — fine as a one-off when you want true
# bare-earth, too slow for the default build. Default uses the bundled DEM +
# GLO-90 margin fill, which is sharp and fast; at 1 km the canopy the DTM would
# strip is worth ~0.1 K, so this is a small trade.
USE_FABDEM = (os.environ.get("USE_FABDEM") or "0") not in ("0", "", "false")
FABDEM_BASE = "https://data.bris.ac.uk/datasets/s5hqmjcdj8yo2ibzi9b4ew3sn"


def _domain_box():
    lat, lon = P.pixel_latlon()
    from shapely.geometry import box
    return box(float(lon.min()), float(lat.min()),
               float(lon.max()), float(lat.max())), lat, lon


def _load_clipped(url, box_geom):
    """Download a Natural Earth GeoJSON and return its geometry clipped to the
    domain box (so downstream point-in-polygon is over a handful of rings)."""
    import json
    from shapely.geometry import shape
    from shapely.ops import unary_union
    r = session().get(url, timeout=180)
    r.raise_for_status()
    feats = json.loads(r.content).get("features", [])
    parts = []
    for f in feats:
        g = shape(f["geometry"])
        if g.is_valid and g.intersects(box_geom):
            parts.append(g.intersection(box_geom))
    if not parts:
        return None
    geom = unary_union(parts)
    log.info("%s: %d features -> %d clipped rings",
             url.rsplit("/", 1)[-1], len(feats),
             len(getattr(geom, "geoms", [geom])))
    return geom


def _fabdem_elevation(lat, lon, land):
    """Elevation on the full grid from FABDEM (global 30 m bare-earth DTM).

    Downloads the 10x10 deg bundles that actually contain land in the domain
    from data.bris (a bundle covering only open sea is skipped — its pixels are
    0 anyway), then samples each grid pixel from its 1 deg tile (FABDEM is
    EPSG:4326, so the grid's own lon/lat sample it directly). Returns
    (elev, covered) or None if the fetch fails — the caller then falls back to
    the provided DEM / API."""
    import glob
    import re as _re
    import tempfile
    import zipfile
    try:
        import rasterio
    except Exception:
        return None
    latf = np.floor(lat).astype(int)
    lonf = np.floor(lon).astype(int)
    la0, la1 = int(latf.min()), int(latf.max())
    lo0, lo1 = int(lonf.min()), int(lonf.max())
    bundles = []
    for a in range(la0 // 10 * 10, la1 + 1, 10):
        for b in range(lo0 // 10 * 10, lo1 + 1, 10):
            box = (latf >= a) & (latf < a + 10) & (lonf >= b) & (lonf < b + 10)
            if (land & box).any():          # skip all-sea bundles
                bundles.append((a, b))
    tmp = tempfile.mkdtemp()
    got = 0
    for a, b in bundles:
        name = f"N{a:02d}E{b:03d}-N{a + 10:02d}E{b + 10:03d}_FABDEM_V1-2.zip"
        try:
            log.info("FABDEM: fetching %s", name)
            zp = os.path.join(tmp, name)
            with session().get(f"{FABDEM_BASE}/{name}", timeout=1800, stream=True) as r:
                r.raise_for_status()
                n = 0
                with open(zp, "wb") as fh:
                    for chunk in r.iter_content(1 << 20):   # stream, don't buffer in RAM
                        fh.write(chunk)
                        n += len(chunk)
            log.info("FABDEM: %s = %.0f MB, extracting", name, n / 1e6)
            with zipfile.ZipFile(zp) as z:
                z.extractall(tmp)
            os.remove(zp)
            got += 1
        except Exception as e:
            log.warning("FABDEM: %s failed (%s)", name, type(e).__name__)
    if not got:
        log.info("FABDEM: no bundles fetched — falling back")
        return None

    tiles = {}
    for f in glob.glob(os.path.join(tmp, "**", "*FABDEM*.tif"), recursive=True):
        m = _re.search(r"N(\d+)E(\d+)", os.path.basename(f))
        if m:
            tiles[(int(m.group(1)), int(m.group(2)))] = f
    elev = np.full(lat.shape, np.nan)
    for (a, b), f in tiles.items():
        cell = (latf == a) & (lonf == b)
        if not cell.any():
            continue
        with rasterio.open(f) as ds:
            pts = list(zip(lon[cell].tolist(), lat[cell].tolist()))
            vals = np.fromiter((s[0] for s in ds.sample(pts)),
                               dtype="float64", count=int(cell.sum()))
            nod = ds.nodata
        if nod is not None:
            vals[vals == nod] = np.nan
        elev[cell] = vals
    covered = np.isfinite(elev)
    log.info("FABDEM: %d tiles, %d/%d grid px covered (%.0f%%)",
             len(tiles), int(covered.sum()), lat.size, covered.mean() * 100)
    elev = np.where(covered, elev, 0.0)
    return np.clip(elev, 0.0, None).astype(np.float32), covered


def _grid_xy_3059():
    """The render grid's pixel-centre eastings/northings in LKS-92 metres."""
    xs = P.X0 + (np.arange(P.W) + 0.5) / P.W * (P.X1 - P.X0)
    ys = P.Y1 - (np.arange(P.H) + 0.5) / P.H * (P.Y1 - P.Y0)
    return np.meshgrid(xs, ys)


def _resolve_dem() -> str | None:
    """Local repo file, else https URL, else R2 object -> a readable path."""
    import tempfile
    if DEM_PATH and os.path.exists(DEM_PATH):
        log.info("DEM: %s (repo)", DEM_PATH)
        return DEM_PATH
    try:
        if DEM_URL:
            log.info("DEM: fetching %s", DEM_URL)
            r = session().get(DEM_URL, timeout=600); r.raise_for_status()
            fd = tempfile.NamedTemporaryFile(suffix=".tif", delete=False)
            fd.write(r.content); fd.close()
            return fd.name
        from wxfusion import archive
        fd = tempfile.NamedTemporaryFile(suffix=".tif", delete=False); fd.close()
        log.info("DEM: downloading r2://%s", DEM_KEY)
        archive.r2_client().download_file(config.R2_BUCKET, DEM_KEY, fd.name)
        return fd.name
    except Exception as e:
        log.info("DEM: none available (%s) — using the elevation API", type(e).__name__)
        return None


def _dem_elevation(lat, lon):
    """Elevation on the full 1 km grid sampled from a DEM GeoTIFF, or None.

    When the DEM is in LKS-92 (this grid's CRS) it is sampled at the grid's own
    metres — no reprojection. Otherwise the grid's lon/lat is reprojected into
    the DEM's CRS first, so any national/European grid works. Sea, voids, and
    pixels outside the DEM's footprint come back 0."""
    dem_path = _resolve_dem()
    if dem_path is None:
        return None
    import rasterio
    from rasterio.warp import transform as warp_transform
    h, w = lat.shape
    with rasterio.open(dem_path) as ds:
        crs = ds.crs or DEM_CRS
        if crs and rasterio.crs.CRS.from_user_input(crs).to_epsg() == 3059:
            gx, gy = _grid_xy_3059()
            xs, ys = gx.ravel().tolist(), gy.ravel().tolist()
        else:
            xs, ys = warp_transform("EPSG:4326", crs,
                                    lon.ravel().tolist(), lat.ravel().tolist())
        elev = np.fromiter((s[0] for s in ds.sample(zip(xs, ys))),
                           dtype="float64", count=h * w).reshape(h, w)
        nod = ds.nodata
        log.info("DEM: %s, CRS %s, nodata %s", dem_path.rsplit("/", 1)[-1], crs, nod)
    if nod is not None:
        elev[elev == nod] = np.nan
    covered = np.isfinite(elev)
    log.info("DEM: %d/%d grid px covered (%.0f%%)",
             int(covered.sum()), h * w, covered.sum() / (h * w) * 100)
    elev = np.where(covered, elev, 0.0)   # sea/void/outside -> datum for now
    return np.clip(elev, 0.0, None).astype(np.float32), covered


def _sample_elevation(lat, lon, need=None):
    """Elevation on a coarse lon/lat lattice from Open-Meteo's free elevation
    API — Copernicus GLO-90 under the hood, which (unlike EU-DEM) covers the
    whole domain including the Russia/Belarus margin. Returns coarse
    (lat, lon, elev).

    need: optional (H, W) boolean of the pixels that actually need filling; when
    given, only lattice points whose nearest grid cell is needed are queried, so
    filling a thin margin costs a handful of calls instead of the whole grid."""
    lo0, lo1 = float(lon.min()), float(lon.max())
    la0, la1 = float(lat.min()), float(lat.max())
    glon = np.arange(lo0, lo1 + 1e-9, ELEV_STEP)
    glat = np.arange(la0, la1 + 1e-9, ELEV_STEP)
    LO, LA = np.meshgrid(glon, glat)
    LO, LA = LO.ravel(), LA.ravel()
    if need is not None:
        # keep only lattice points sitting on (or one step from) a needed pixel
        from scipy import ndimage
        near = ndimage.binary_dilation(need, iterations=int(ELEV_STEP * 111) + 2)
        gx, gy = P.to_px(LA, LO)
        gx = np.clip(gx.astype(int), 0, P.W - 1)
        gy = np.clip(gy.astype(int), 0, P.H - 1)
        keep = near[gy, gx]
        LA, LO = LA[keep], LO[keep]
    s = session()
    elev = np.full(LO.shape, np.nan)
    B = 100
    for i in range(0, len(LO), B):
        la_s = ",".join(f"{v:.4f}" for v in LA[i:i + B])
        lo_s = ",".join(f"{v:.4f}" for v in LO[i:i + B])
        try:
            r = s.get("https://api.open-meteo.com/v1/elevation",
                      params={"latitude": la_s, "longitude": lo_s}, timeout=60)
            r.raise_for_status()
            elev[i:i + B] = r.json().get("elevation", [np.nan] * (len(LA[i:i + B])))
        except Exception as e:
            log.warning("elevation chunk %d failed (%s)", i, type(e).__name__)
        time.sleep(0.4)  # one-off build; stay gentle on the free tier
    ok = np.isfinite(elev)
    log.info("elevation: %d/%d coarse points at %.2f deg", int(ok.sum()), len(LO), ELEV_STEP)
    return LA[ok], LO[ok], elev[ok]


# --- preview rendering ------------------------------------------------------
def _png(fig):
    import matplotlib.pyplot as plt
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def _emit(name, png):
    b64 = base64.b64encode(png).decode()
    print(f"@@SNAP_BEGIN {name} {len(b64)}")
    print(b64)
    print(f"@@SNAP_END {name}")


def _preview(land, sea, lake, coast, elev, tpi):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # land/sea/lake as a 3-colour label image
    lbl = np.zeros(land.shape + (3,), np.uint8)
    lbl[sea] = (70, 110, 170)      # sea blue
    lbl[land] = (150, 170, 120)    # land green-grey
    lbl[lake] = (90, 170, 200)     # lake cyan
    fig = plt.figure(figsize=(P.W / 100, P.H / 100)); ax = fig.add_axes([0, 0, 1, 1])
    ax.imshow(lbl); ax.axis("off")
    _emit("LANDSEA", _png(fig))

    for name, arr, cmap in [
        ("COAST", coast, "RdBu"),
        ("ELEV", elev, "terrain"),
        ("TPI", tpi, "coolwarm"),
    ]:
        fig = plt.figure(figsize=(P.W / 100, P.H / 100)); ax = fig.add_axes([0, 0, 1, 1])
        vmax = float(np.nanpercentile(np.abs(arr), 99)) or 1.0
        if name == "ELEV":
            ax.imshow(arr, cmap=cmap, vmin=0, vmax=vmax)
        else:
            ax.imshow(arr, cmap=cmap, vmin=-vmax, vmax=vmax)
        ax.axis("off")
        _emit(name, _png(fig))


def main() -> int:
    assert abs(sg.PIXEL_KM - 1.0) < 1e-6, f"grid not 1 km/px: {sg.PIXEL_KM}"
    box_geom, lat, lon = _domain_box()

    land_geom = _load_clipped(LAND_URL, box_geom)
    lake_geom = _load_clipped(LAKES_URL, box_geom)
    if land_geom is None:
        log.error("no land polygons in domain — aborting")
        return 1

    land, sea, lake = sg.land_sea_lake(lat, lon, land_geom, lake_geom)
    water = sea | lake
    coast = sg.coast_distance_km(land, water)

    dem = _fabdem_elevation(lat, lon, land) if USE_FABDEM else None  # bare-earth
    if dem is None:
        dem = _dem_elevation(lat, lon)       # provided Baltic DEM, else None
    if dem is not None:
        elev, covered = dem
        # The provided DEM stops at the Baltic states' border; the land beyond
        # it (the Russia/Belarus margin) came back 0, which is a false sea-level.
        # Fill exactly those pixels from Copernicus GLO-90 (via the elevation
        # API), which does cover them, and leave the sharp DEM untouched.
        gap = land & ~covered
        if int(gap.sum()):
            log.info("elevation: filling %d uncovered land px from GLO-90", int(gap.sum()))
            try:
                cla, clo, cel = _sample_elevation(lat, lon, need=gap)
                fill = sg.interp_elevation(cla, clo, cel, lat, lon, sea)
                elev[gap] = fill[gap]
            except Exception:
                log.exception("margin fill failed — that edge stays at datum")
        elev[sea] = 0.0                      # keep the water flat and at datum
    else:
        try:
            cla, clo, cel = _sample_elevation(lat, lon)
            elev = sg.interp_elevation(cla, clo, cel, lat, lon, sea)
        except Exception:
            log.exception("elevation failed — writing zeros; other layers stand")
            elev = np.zeros(land.shape, np.float32)
    tpi = sg.topographic_position(elev)
    tpi[~land] = 0.0   # TPI is a land signal; zero it over water so the abrupt
    #                    land->0 step at the coast does not read as a ridge

    # bake to one .npz in R2
    buf = io.BytesIO()
    np.savez_compressed(buf, land=land, sea=sea, lake=lake,
                        coast_km=coast, elev_m=elev, tpi_m=tpi,
                        lat=lat.astype(np.float32), lon=lon.astype(np.float32))
    body = buf.getvalue()
    try:
        from wxfusion import archive
        archive.r2_client().put_object(
            Bucket=config.R2_BUCKET, Key=NPZ_KEY, Body=body,
            ContentType="application/octet-stream",
            CacheControl="public, max-age=604800")
        log.info("static grid: wrote %s (%.0f kB)", NPZ_KEY, len(body) / 1024)
    except Exception:
        log.exception("R2 write failed — preview still emitted")

    _preview(land, sea, lake, coast, elev, tpi)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
