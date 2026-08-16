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
DEM_KEY = os.environ.get("DEM_KEY") or "assets/dem/baltic_dem.tif"
DEM_URL = os.environ.get("DEM_URL") or ""


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


def _dem_elevation(lat, lon):
    """Elevation on the full 1 km grid, sampled from a provided DEM GeoTIFF.

    Looks for the TIF in R2 (DEM_KEY) or over https (DEM_URL), downloads it to
    the runner, and samples it at every grid pixel — reprojecting the grid's
    lon/lat into the DEM's own CRS first, so any national or European grid
    works without the caller stating it. Returns the (H, W) array, or None if
    no DEM is configured/reachable (caller falls back to the elevation API)."""
    import tempfile
    dem_path = None
    try:
        if DEM_URL:
            log.info("DEM: fetching %s", DEM_URL)
            r = session().get(DEM_URL, timeout=600)
            r.raise_for_status()
            fd = tempfile.NamedTemporaryFile(suffix=".tif", delete=False)
            fd.write(r.content); fd.close()
            dem_path = fd.name
        else:
            from wxfusion import archive
            fd = tempfile.NamedTemporaryFile(suffix=".tif", delete=False)
            fd.close()
            log.info("DEM: downloading r2://%s", DEM_KEY)
            archive.r2_client().download_file(config.R2_BUCKET, DEM_KEY, fd.name)
            dem_path = fd.name
    except Exception as e:
        log.info("DEM: none available (%s) — using the elevation API", type(e).__name__)
        return None

    import rasterio
    from rasterio.warp import transform as warp_transform
    h, w = lat.shape
    with rasterio.open(dem_path) as ds:
        xs, ys = warp_transform("EPSG:4326", ds.crs,
                                lon.ravel().tolist(), lat.ravel().tolist())
        elev = np.fromiter((s[0] for s in ds.sample(zip(xs, ys))),
                           dtype="float64", count=h * w).reshape(h, w)
        nod = ds.nodata
        log.info("DEM: %s, CRS %s, nodata %s", dem_path.rsplit("/", 1)[-1], ds.crs, nod)
    if nod is not None:
        elev[elev == nod] = np.nan
    # sea/void -> 0; clamp the odd negative (below-datum polders) up to 0
    elev = np.where(np.isfinite(elev), elev, 0.0)
    return np.clip(elev, 0.0, None).astype(np.float32)


def _sample_elevation(lat, lon):
    """Elevation on a coarse lon/lat lattice from Open-Meteo's free elevation
    API (Copernicus GLO-90 under the hood). Returns coarse (lat, lon, elev)."""
    lo0, lo1 = float(lon.min()), float(lon.max())
    la0, la1 = float(lat.min()), float(lat.max())
    glon = np.arange(lo0, lo1 + 1e-9, ELEV_STEP)
    glat = np.arange(la0, la1 + 1e-9, ELEV_STEP)
    LO, LA = np.meshgrid(glon, glat)
    LO, LA = LO.ravel(), LA.ravel()
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
        time.sleep(1.0)  # one-off build; stay gentle on the free tier
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

    elev = _dem_elevation(lat, lon)          # real DEM if one is provided
    if elev is not None:
        elev[sea] = 0.0                      # keep the water flat and at datum
    else:
        try:
            cla, clo, cel = _sample_elevation(lat, lon)
            elev = sg.interp_elevation(cla, clo, cel, lat, lon, sea)
        except Exception:
            log.exception("elevation failed — writing zeros; other layers stand")
            elev = np.zeros(land.shape, np.float32)
    tpi = sg.topographic_position(elev)

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
