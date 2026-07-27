"""Common EPSG:3059 (LKS-92 / Latvia TM) target grid for ALL map imagery.

Every map product (MEPS render, UM overlay, radar composite, background) is
resampled to this one grid, so the client can stack and compare them 1:1 —
same projection as the TTA-tracker map products.
"""
from __future__ import annotations

import functools

import numpy as np

# Fixed extent covering the Baltic bbox (53.8-59.9 N, 20.0-28.6 E)
X0, X1 = 235_000, 805_000
Y0, Y1 = -40_000, 650_000
W, H = 548, 664  # pixels; matches extent aspect (570 km x 690 km)


@functools.lru_cache(maxsize=1)
def _transformers():
    from pyproj import Transformer
    fwd = Transformer.from_crs("EPSG:4326", "EPSG:3059", always_xy=True)
    inv = Transformer.from_crs("EPSG:3059", "EPSG:4326", always_xy=True)
    return fwd, inv


def to_xy(lon, lat):
    """lon/lat (deg, arrays ok) -> LKS-92 x/y meters."""
    return _transformers()[0].transform(lon, lat)


def to_px(lat, lon):
    """lat/lon -> target image pixel (x_px, y_px)."""
    x, y = to_xy(lon, lat)
    return ((np.asarray(x) - X0) / (X1 - X0) * W,
            (Y1 - np.asarray(y)) / (Y1 - Y0) * H)


@functools.lru_cache(maxsize=1)
def pixel_latlon():
    """(lat, lon) arrays of shape (H, W): geographic position of each target
    pixel center. Used to resample any georeferenced source raster."""
    xs = X0 + (np.arange(W) + 0.5) / W * (X1 - X0)
    ys = Y1 - (np.arange(H) + 0.5) / H * (Y1 - Y0)
    gx, gy = np.meshgrid(xs, ys)
    lon, lat = _transformers()[1].transform(gx, gy)
    return lat, lon


def resample_mercator_image(img_arr: np.ndarray, sw: tuple, ne: tuple) -> np.ndarray:
    """Nearest-neighbour resample of an RGBA array that is linear in web
    mercator between sw=(lat,lon) and ne=(lat,lon) (Leaflet imageOverlay /
    stitched XYZ tiles) onto the target grid. Returns (H, W, 4) uint8."""
    lat, lon = pixel_latlon()

    def merc_y(la):
        r = np.radians(la)
        return np.log(np.tan(r) + 1.0 / np.cos(r))

    u = (lon - sw[1]) / (ne[1] - sw[1])
    v = (merc_y(ne[0]) - merc_y(lat)) / (merc_y(ne[0]) - merc_y(sw[0]))
    sh, sw_px = img_arr.shape[:2]
    xi = np.clip((u * sw_px).astype(int), 0, sw_px - 1)
    yi = np.clip((v * sh).astype(int), 0, sh - 1)
    out = img_arr[yi, xi]
    inside = (u >= 0) & (u < 1) & (v >= 0) & (v < 1)
    out = out.copy()
    out[~inside] = 0
    return out
