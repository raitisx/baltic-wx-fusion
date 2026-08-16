"""Static geography rasters on the 1 km EPSG:3059 render grid.

The downscaler keys the fused blend off geography, and in the Baltic geography
means water more than height: a long coast, the Gulf of Riga, and big lakes,
over ground that barely clears 300 m. So the layers that earn their keep are
the land/sea/lake split and distance to the coast; elevation is here too, for
the modest lapse-rate and snow-line signal, but it is the junior partner.

Everything is baked to the same (H, W) grid the maps render on — and that grid
is exactly 1 km per pixel (570 km across 570 px, 690 km down 690 px), so a
distance in pixels IS a distance in kilometres, which is why the coast field
needs no scaling.

This module only builds the arrays. Downloading the coastline/lake polygons,
querying elevation, saving the .npz and rendering the preview PNGs live in
jobs/build_static_grid.py. No GDAL/rasterio: polygons are rasterised by
point-in-polygon (shapely), the rest is numpy/scipy.
"""
from __future__ import annotations

import logging

import numpy as np

from . import proj3059 as P

log = logging.getLogger(__name__)

PIXEL_KM = (P.X1 - P.X0) / P.W / 1000.0  # 1.0 by construction; asserted below


def _rasterise(polygons, lat, lon) -> np.ndarray:
    """Boolean (H, W): which pixels fall inside any of the given polygons.

    polygons: shapely geometry already clipped to the domain (so the vertex
    count is small). Uses shapely's vectorised contains over the flat grid."""
    from shapely import points as _points, contains
    from shapely.geometry import box

    h, w = lat.shape
    pts = _points(lon.ravel(), lat.ravel())
    mask = contains(polygons, pts)
    return np.asarray(mask, bool).reshape(h, w)


def land_sea_lake(lat, lon, land_geom, lake_geom):
    """Three boolean (H, W) masks: land, sea, lake.

    land polygons carve land out of the sea; lake polygons carve water back out
    of the land. A pixel is sea when it is in no land polygon; lake when it is
    in a land polygon but also in a lake; land otherwise."""
    in_land = _rasterise(land_geom, lat, lon)
    in_lake = _rasterise(lake_geom, lat, lon) if lake_geom is not None \
        else np.zeros_like(in_land)
    sea = ~in_land
    lake = in_land & in_lake
    land = in_land & ~in_lake
    log.info("land/sea/lake: land %d, sea %d, lake %d px",
             int(land.sum()), int(sea.sum()), int(lake.sum()))
    return land, sea, lake


def coast_distance_km(land: np.ndarray, water: np.ndarray) -> np.ndarray:
    """Signed distance to the nearest coast, km: +over land, -over water.

    The grid is 1 km/px, so the Euclidean distance transform is already in km.
    Signed so the downscaler can widen a sea-breeze inland and a marine layer
    offshore from one field."""
    from scipy import ndimage
    # distance from each land pixel to the nearest water pixel, and vice versa
    d_to_water = ndimage.distance_transform_edt(~water)   # 0 on water
    d_to_land = ndimage.distance_transform_edt(~land)     # 0 on land
    signed = np.where(water, -d_to_land, d_to_water) * PIXEL_KM
    return signed.astype(np.float32)


def interp_elevation(coarse_lat, coarse_lon, coarse_elev, lat, lon,
                     sea: np.ndarray) -> np.ndarray:
    """Elevation (m) on the full grid, linearly interpolated from a coarse
    sample, clamped to >=0 and forced to 0 over sea."""
    from scipy.interpolate import griddata
    grid = griddata(
        np.column_stack([coarse_lon, coarse_lat]), coarse_elev,
        (lon, lat), method="linear",
    )
    # linear leaves NaN outside the sample hull — fill from nearest.
    if np.isnan(grid).any():
        near = griddata(
            np.column_stack([coarse_lon, coarse_lat]), coarse_elev,
            (lon, lat), method="nearest",
        )
        grid = np.where(np.isnan(grid), near, grid)
    grid = np.clip(grid, 0.0, None)
    grid[sea] = 0.0
    return grid.astype(np.float32)


def topographic_position(elev: np.ndarray, radius_km: float = 15.0) -> np.ndarray:
    """Height above the local mean, m: >0 on ridges/hills, <0 in valleys/basins.

    A cheap proxy for cold-air pooling and radiation-fog hollows. Window radius
    in km == pixels on this grid."""
    from scipy import ndimage
    r = max(1, int(radius_km / PIXEL_KM))
    local_mean = ndimage.uniform_filter(elev, size=2 * r + 1, mode="nearest")
    return (elev - local_mean).astype(np.float32)
