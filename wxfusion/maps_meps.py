"""Hourly Baltic flight maps from MET Norway MEPS (2.5 km) -> R2.

Why MEPS: it publishes the fields the flight decision needs *directly* —
cloud_base_altitude, precipitation_amount_acc, wind_speed_of_gust,
fog_area_fraction — on a 2.5 km grid covering the whole Baltic, via free
OPeNDAP (thredds.met.no, NLOD/CC-BY licence, credit MET Norway).

Output per run (one PNG per lead hour per cloud-base threshold):
  maps/meps/<runYYYYMMDDTHH>/<validYYYYMMDDTHH>_alt<thr>.png
  maps/meps/latest.json   manifest: {run, hours[], thresholds[], path}

Style matches the TTA-tracker weather maps: cream land, green precip scale,
pink tint where cloud base is below the threshold, hatched-free, country
borders from a bundled Natural Earth extract.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import tempfile

import boto3
import numpy as np

from . import config
from .http import session

log = logging.getLogger(__name__)

CATALOG = "https://thredds.met.no/thredds/catalog/mepslatest/catalog.xml"
DAP_BASE = "https://thredds.met.no/thredds/dodsC/mepslatest/"
BBOX = (53.8, 59.9, 20.0, 28.6)  # lat_min, lat_max, lon_min, lon_max
THRESHOLDS = [100, 300, 650, 700, 1000]
FILL = 1e30  # cloud_base_altitude fill => clear sky

BORDERS_FILE = os.path.join(os.path.dirname(__file__), "assets", "borders_baltic.json")


def r2_client():
    if not (config.R2_ACCOUNT_ID and config.R2_ACCESS_KEY_ID):
        raise RuntimeError("R2 credentials not configured")
    return boto3.client(
        "s3",
        endpoint_url=f"https://{config.R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=config.R2_ACCESS_KEY_ID,
        aws_secret_access_key=config.R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )


def latest_run() -> str:
    r = session().get(CATALOG, timeout=60)
    r.raise_for_status()
    runs = re.findall(r'urlPath="mepslatest/(meps_det_2_5km_\d{8}T\d{2}Z\.ncml)"', r.text)
    if not runs:
        raise RuntimeError("no meps_det datasets in catalog")
    return sorted(runs)[-1]


def render_run(max_hours: int = 66) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap
    import netCDF4 as nc

    ds_name = latest_run()
    run_tag = re.search(r"(\d{8}T\d{2})Z", ds_name).group(1)
    log.info("MEPS run %s", ds_name)
    ds = nc.Dataset(DAP_BASE + ds_name)

    lat = np.array(ds.variables["latitude"][:])
    lon = np.array(ds.variables["longitude"][:])
    m = (lat >= BBOX[0]) & (lat <= BBOX[1]) & (lon >= BBOX[2]) & (lon <= BBOX[3])
    ys, xs = np.where(m)
    y0, y1, x0, x1 = ys.min(), ys.max(), xs.min(), xs.max()
    latw, lonw = lat[y0:y1 + 1, x0:x1 + 1], lon[y0:y1 + 1, x0:x1 + 1]

    times = nc.num2date(ds.variables["time"][:], ds.variables["time"].units)
    n = min(len(times), max_hours + 1)
    log.info("fetching %d steps, window %dx%d", n, y1 - y0 + 1, x1 - x0 + 1)
    cb = np.array(ds.variables["cloud_base_altitude"][:n, 0, y0:y1 + 1, x0:x1 + 1])
    acc = np.array(ds.variables["precipitation_amount_acc"][:n, 0, y0:y1 + 1, x0:x1 + 1])
    cb = np.where(cb > FILL, np.nan, cb)

    from . import proj3059 as P

    borders = json.load(open(BORDERS_FILE))
    greens = LinearSegmentedColormap.from_list(
        "g", ["#c8e6b0", "#57b647", "#1c7a1c", "#0b4d0b"])
    pink = LinearSegmentedColormap.from_list("p", ["#f3b8b4", "#f3b8b4"])

    # project the MEPS cell coordinates once (curvilinear mesh in LKS-92)
    xw, yw = P.to_xy(lonw, latw)
    borders_xy = [[P.to_xy([px for px, _ in line], [py for _, py in line])
                   for line in c["lines"]] for c in borders]

    def blank_fig():
        """Full-bleed axes on the fixed EPSG:3059 extent — no text, no
        margins; timestamps live in the page UI, not in pixels."""
        fig = plt.figure(figsize=(P.W / 100, P.H / 100), dpi=100)
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_facecolor("#fbf3de")
        ax.set_xlim(P.X0, P.X1); ax.set_ylim(P.Y0, P.Y1)
        ax.set_aspect("equal"); ax.axis("off")
        return fig, ax

    def draw_borders(ax):
        for country in borders_xy:
            for bx, by in country:
                ax.plot(bx, by, color="#6b6552", lw=0.7)

    s3 = r2_client()
    hours_out = []
    tmp = tempfile.mkdtemp()

    # shared background plate for overlay layers (UM etc.)
    fig, ax = blank_fig()
    draw_borders(ax)
    bg = os.path.join(tmp, "bg.png")
    fig.savefig(bg, dpi=100)
    plt.close(fig)
    s3.upload_file(bg, config.R2_BUCKET, "maps/bg_3059.png", ExtraArgs={
        "ContentType": "image/png", "CacheControl": "public, max-age=604800"})

    for i in range(1, n):
        valid = times[i]
        vtag = valid.strftime("%Y%m%dT%H")
        pr = np.clip(acc[i] - acc[i - 1], 0, None)
        prm = np.where(pr >= 0.1, pr, np.nan)
        for thr in THRESHOLDS:
            fig, ax = blank_fig()
            below = np.where(np.isfinite(cb[i]) & (cb[i] < thr), 1.0, np.nan)
            ax.pcolormesh(xw, yw, below, cmap=pink, vmin=0, vmax=1,
                          alpha=0.75, shading="auto")
            ax.pcolormesh(xw, yw, prm, cmap=greens, vmin=0.1, vmax=4,
                          alpha=0.9, shading="auto")
            draw_borders(ax)
            fp = os.path.join(tmp, f"{vtag}_alt{thr}.png")
            fig.savefig(fp, dpi=100)
            plt.close(fig)
            key = f"maps/meps/{run_tag}/{vtag}_alt{thr}.png"
            s3.upload_file(fp, config.R2_BUCKET, key, ExtraArgs={
                "ContentType": "image/png",
                "CacheControl": "public, max-age=86400"})
            os.unlink(fp)
        hours_out.append(valid.strftime("%Y-%m-%dT%H:00:00Z"))
        if i % 12 == 0:
            log.info("rendered %d/%d hours", i, n - 1)

    manifest = {
        "run": run_tag,
        "path": f"maps/meps/{run_tag}",
        "hours": hours_out,
        "proj": "epsg3059",
        "thresholds": THRESHOLDS,
        "credit": "MET Norway MEPS (thredds.met.no), CC-BY 4.0 / NLOD",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    s3.put_object(Bucket=config.R2_BUCKET, Key="maps/meps/latest.json",
                  Body=json.dumps(manifest).encode(),
                  ContentType="application/json",
                  CacheControl="public, max-age=300")
    log.info("manifest written: %d hours, run %s", len(hours_out), run_tag)
