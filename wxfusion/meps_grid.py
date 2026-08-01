"""A year of MEPS fields, kept as data rather than as pictures.

The problem this solves: map PNGs are output, not input. Once a frame is
rendered, changing the palette or the ceiling rule cannot fix it, and once the
frame is deleted the hour is gone. Re-rendering needs the model grid, and
`mepslatest` on thredds only holds about a day.

MET Norway's long-term archive holds years, so the grid can be kept ourselves,
cheaply, in the shape we actually draw from:

  cb    cloud_base_altitude              uint16 metres, 65535 = clear
  lcc   low_type_cloud_area_fraction     uint8  percent
  fog   fog_area_fraction                uint8  percent
  pr    precipitation over the hour       uint16 hundredths of a mm

Measured on the 20.07 09Z run over our window (300x248): 141 KiB an hour,
3.3 MiB a day, 1.2 GiB a year. Three tiers, each with its own lifetime:

  grid    maps/meps_grid/YYYY/MM/DD/<hour>.npz   365 days
  frames  maps/meps/<run>/<hour>_alt<thr>.png     14 days
  onreq   maps/meps_req/<hour>_alt<thr>.png        1 day

Anything inside the year re-renders from the grid in about a second, with no
call to thredds at all. Anything older is fetched on demand and kept for a day.
"""
from __future__ import annotations

import datetime as dt
import io
import logging
import re

import numpy as np

from . import config
from .maps_meps import (BBOX, DAP_BASE, FILL, THRESHOLDS, _gate_ceiling,
                        draw_hour, r2_client)

log = logging.getLogger(__name__)

GRID_PREFIX = "maps/meps_grid"
CLEAR = 65535           # cb sentinel: no cloud base at all
KEEP_DAYS = 365
FRAME_KEEP_DAYS = 14
REQ_KEEP_HOURS = 24


def _key(hour: dt.datetime) -> str:
    return f"{GRID_PREFIX}/{hour:%Y/%m/%d}/{hour:%Y%m%dT%H}.npz"


def store_run(url: str, leads=(1, 2, 3)) -> int:
    """Read one run's early leads and write an npz per valid hour.

    Early leads only: an hour is best described by the freshest run that
    reaches it, and every run covers the hours after it, so three leads from
    each 3-hourly run tiles the day exactly once.
    """
    import netCDF4 as nc

    m = re.search(r"(\d{8}T\d{2})Z", url)
    if not m:
        raise ValueError(f"no run tag in {url}")
    run = dt.datetime.strptime(m.group(1), "%Y%m%dT%H").replace(
        tzinfo=dt.timezone.utc)

    ds = nc.Dataset(url if url.startswith("http") else DAP_BASE + url)
    lat = np.array(ds.variables["latitude"][:])
    lon = np.array(ds.variables["longitude"][:])
    win = (lat >= BBOX[0]) & (lat <= BBOX[1]) & (lon >= BBOX[2]) & (lon <= BBOX[3])
    ys, xs = np.where(win)
    y0, y1, x0, x1 = ys.min(), ys.max(), xs.min(), xs.max()
    n = max(leads) + 1                      # +1: precipitation needs the next step

    def grab(name):
        return np.array(ds.variables[name][:n, 0, y0:y1 + 1, x0:x1 + 1])

    cb = grab("cloud_base_altitude")
    acc = grab("precipitation_amount_acc")
    lcc = grab("low_type_cloud_area_fraction")
    try:
        fog = grab("fog_area_fraction")
    except Exception:
        fog = np.zeros_like(lcc)

    s3 = r2_client()
    written = 0
    for i in leads:
        if i + 1 > acc.shape[0] - 1:
            continue                      # no next step, so no hourly rain
        hour = run + dt.timedelta(hours=i)
        # Stored the way it is drawn: the rain of the hour STARTING here.
        pr = np.clip(acc[i + 1] - acc[i], 0, None)
        cbq = np.where(cb[i] > FILL, CLEAR,
                       np.clip(cb[i], 0, 20000)).astype(np.uint16)
        buf = io.BytesIO()
        np.savez_compressed(
            buf,
            cb=cbq,
            lcc=(np.clip(lcc[i], 0, 1) * 100).astype(np.uint8),
            fog=(np.clip(fog[i], 0, 1) * 100).astype(np.uint8),
            pr=(np.clip(pr, 0, 650) * 100).astype(np.uint16),
            lat=lat[y0:y1 + 1, x0:x1 + 1].astype(np.float32),
            lon=lon[y0:y1 + 1, x0:x1 + 1].astype(np.float32),
            run=np.array([run.timestamp()]),
        )
        s3.put_object(Bucket=config.R2_BUCKET, Key=_key(hour),
                      Body=buf.getvalue(), ContentType="application/octet-stream",
                      CacheControl="public, max-age=31536000")
        written += 1
    log.info("grid: %s -> %d hours", m.group(1), written)
    return written


def read_hour(hour: dt.datetime) -> dict | None:
    """Fields for one hour, back in the units the renderer wants."""
    s3 = r2_client()
    try:
        body = s3.get_object(Bucket=config.R2_BUCKET, Key=_key(hour))["Body"].read()
    except Exception:
        return None
    z = np.load(io.BytesIO(body))
    cb = z["cb"].astype(np.float32)
    cb = np.where(cb >= CLEAR, np.nan, cb)
    lcc = z["lcc"].astype(np.float32) / 100.0
    fog = z["fog"].astype(np.float32) / 100.0
    gated, fogm = _gate_ceiling(cb, lcc, fog)
    return {"cb": gated, "fogm": fogm, "pr": z["pr"].astype(np.float32) / 100.0,
            "lat": z["lat"], "lon": z["lon"],
            "run": dt.datetime.fromtimestamp(float(z["run"][0]), dt.timezone.utc)}


def have_hour(hour: dt.datetime) -> bool:
    s3 = r2_client()
    try:
        s3.head_object(Bucket=config.R2_BUCKET, Key=_key(hour))
        return True
    except Exception:
        return False


def _prune_prefix(prefix: str, older_than: dt.datetime, pat: str) -> int:
    """Delete keys under `prefix` whose embedded hour is before `older_than`."""
    s3 = r2_client()
    rx = re.compile(pat)
    doomed, token = [], None
    while True:
        kw = {"Bucket": config.R2_BUCKET, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kw)
        for obj in resp.get("Contents", []):
            m = rx.search(obj["Key"])
            if not m:
                continue
            when = dt.datetime.strptime(m.group(1), "%Y%m%dT%H").replace(
                tzinfo=dt.timezone.utc)
            if when < older_than:
                doomed.append(obj["Key"])
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    for i in range(0, len(doomed), 1000):
        s3.delete_objects(Bucket=config.R2_BUCKET,
                          Delete={"Objects": [{"Key": k} for k in doomed[i:i + 1000]]})
    return len(doomed)


def prune(keep_days: int = KEEP_DAYS, frame_days: int = FRAME_KEEP_DAYS,
          req_hours: int = REQ_KEEP_HOURS) -> dict:
    """Roll all three tiers forward.

    Frames are keyed by the hour they are valid for, not by the run that drew
    them, so a run from before the window whose frames are still current is not
    caught by this.
    """
    now = dt.datetime.now(dt.timezone.utc)
    out = {
        "grid": _prune_prefix(GRID_PREFIX + "/", now - dt.timedelta(days=keep_days),
                              r"/(\d{8}T\d{2})\.npz$"),
        "frames": _prune_prefix("maps/meps/", now - dt.timedelta(days=frame_days),
                                r"/(\d{8}T\d{2})_alt\d+\.png$"),
        "requested": _prune_prefix("maps/meps_req/", now - dt.timedelta(hours=req_hours),
                                   r"/(\d{8}T\d{2})_alt\d+\.png$"),
    }
    log.info("prune: %d grids, %d frames, %d on-demand", out["grid"],
             out["frames"], out["requested"])
    return out


def fetch_hour_live(hour: dt.datetime) -> bool:
    """Pull one hour straight from MET's archive, for dates we do not keep.

    Picks the newest run at or before the hour that still reaches it, which is
    the same rule the stored grid follows.
    """
    from .maps_meps import archive_runs

    for back in range(0, 4):                      # 3-hourly runs, look back 9 h
        cand = hour - dt.timedelta(hours=3 * back)
        cand = cand.replace(hour=cand.hour // 3 * 3, minute=0, second=0,
                            microsecond=0)
        lead = int((hour - cand).total_seconds() // 3600)
        if not 1 <= lead <= 3:
            continue
        urls = [u for u in archive_runs(cand.date())
                if f"{cand:%Y%m%dT%H}Z" in u]
        if not urls:
            continue
        try:
            store_run(urls[0], leads=(lead,))
            return True
        except Exception:
            log.exception("live fetch failed for %s", hour)
    return False


def render_hour(hour: dt.datetime, prefix: str | None = None) -> int:
    """Draw one stored hour into frames. Returns how many PNGs were written.

    prefix=None writes into the normal frame store and registers the hour in
    maps/meps/archive.json, which is what a re-render of recent history wants.
    maps/meps_req/ is for hours pulled on demand, which live for a day and are
    never indexed — the page asks for them by name.
    """
    g = read_hour(hour)
    if g is None:
        return 0
    pngs = draw_hour(g["cb"], g["fogm"], g["pr"], g["lat"], g["lon"])
    s3 = r2_client()
    run_tag = g["run"].strftime("%Y%m%dT%H")
    vtag = hour.strftime("%Y%m%dT%H")
    for thr, body in pngs.items():
        key = (f"maps/meps_req/{vtag}_alt{thr}.png" if prefix == "req"
               else f"maps/meps/{run_tag}/{vtag}_alt{thr}.png")
        s3.put_object(Bucket=config.R2_BUCKET, Key=key, Body=body,
                      ContentType="image/png",
                      CacheControl="public, max-age=86400")
    if prefix != "req":
        _register(run_tag, vtag)
    return len(pngs)


def _register(run_tag: str, vtag: str) -> None:
    """Point maps/meps/archive.json at this run for this hour."""
    import json

    s3 = r2_client()
    try:
        arch = json.loads(s3.get_object(
            Bucket=config.R2_BUCKET, Key="maps/meps/archive.json")["Body"].read())
    except Exception:
        arch = {"hours": {}, "thresholds": THRESHOLDS}
    arch.setdefault("hours", {})[vtag] = run_tag
    s3.put_object(Bucket=config.R2_BUCKET, Key="maps/meps/archive.json",
                  Body=json.dumps(arch).encode(), ContentType="application/json",
                  CacheControl="public, max-age=300")
