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
import json
import logging
import re

import numpy as np

from . import config
from .maps_meps import (BBOX, DAP_BASE, FILL, THRESHOLDS, _gate_ceiling,
                        draw_hour, r2_client)

log = logging.getLogger(__name__)

GRID_PREFIX = "maps/meps_grid"
# Stored in a format Deno can open with nothing but DecompressionStream, so the
# edge function can draw an old hour while you wait instead of queueing it:
#
#   "WXG1" | uint32 header length | JSON header | raw arrays, back to back
#
# all deflate-compressed. npz would have been the obvious choice and is a zip,
# which Deno has no way to read without pulling in a library.
MAGIC = b"WXG1"
CLEAR = 65535           # cb sentinel: no cloud base at all
KEEP_DAYS = 365
FRAME_KEEP_DAYS = 14
REQ_KEEP_HOURS = 24


def _key(hour: dt.datetime) -> str:
    return f"{GRID_PREFIX}/{hour:%Y/%m/%d}/{hour:%Y%m%dT%H}.wxg"


def pack(arrays: dict, meta: dict) -> bytes:
    """{name: ndarray} -> one deflate blob. See MAGIC."""
    import zlib

    header = {"meta": meta, "arrays": [
        {"name": k, "dtype": v.dtype.str, "shape": list(v.shape)}
        for k, v in arrays.items()]}
    hb = json.dumps(header).encode()
    # Every section padded to 8 bytes. A typed-array view in JS must start on a
    # multiple of its element size, and a JSON header is whatever length it is:
    # without this the reader throws "start offset should be a multiple of 2"
    # on the first uint16 field. Caught in test before it ever ran.
    hb += b" " * (-len(hb) % 8)
    parts = [MAGIC + len(hb).to_bytes(4, "little") + b"\0" * 8 + hb]
    for v in arrays.values():
        b = np.ascontiguousarray(v).tobytes()
        parts.append(b + b"\0" * (-len(b) % 8))
    return zlib.compress(b"".join(parts), 6)


def unpack(blob: bytes) -> tuple[dict, dict]:
    import zlib

    raw = zlib.decompress(blob)
    if raw[:4] != MAGIC:
        raise ValueError("not a wxg blob")
    hlen = int.from_bytes(raw[4:8], "little")
    header = json.loads(raw[16:16 + hlen])
    out, off = {}, 16 + hlen
    for spec in header["arrays"]:
        a = np.frombuffer(raw, dtype=np.dtype(spec["dtype"]),
                          count=int(np.prod(spec["shape"])), offset=off)
        out[spec["name"]] = a.reshape(spec["shape"])
        off += a.nbytes + (-a.nbytes % 8)
    return out, header["meta"]


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
    try:
        tair = grab("air_temperature_2m")          # Kelvin
    except Exception:
        tair = None

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
        arrays = {
            "cb": cbq,
            "lcc": (np.clip(lcc[i], 0, 1) * 100).astype(np.uint8),
            "fog": (np.clip(fog[i], 0, 1) * 100).astype(np.uint8),
            "pr": (np.clip(pr, 0, 650) * 100).astype(np.uint16),
        }
        if tair is not None:
            # Kelvin x100 stays inside uint16 for any temperature the planet
            # produces, so no new dtype and no sign to get wrong.
            arrays["t2m"] = (np.clip(tair[i], 150, 340) * 100).astype(np.uint16)
        blob = pack(arrays, {"run": run.strftime("%Y%m%dT%H"), "hour": hour.strftime("%Y%m%dT%H"),
            "rows": int(y1 - y0 + 1), "cols": int(x1 - x0 + 1)})
        s3.put_object(Bucket=config.R2_BUCKET, Key=_key(hour),
                      Body=blob, ContentType="application/octet-stream",
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
    z, meta = unpack(body)
    cb = z["cb"].astype(np.float32)
    cb = np.where(cb >= CLEAR, np.nan, cb)
    lcc = z["lcc"].astype(np.float32) / 100.0
    fog = z["fog"].astype(np.float32) / 100.0
    gated, fogm = _gate_ceiling(cb, lcc, fog)
    lat, lon = _window_latlon()
    # Grids written before the freezing palette have no temperature in them;
    # those hours draw green as they always did.
    t2m = (z["t2m"].astype(np.float32) / 100.0 - 273.15) if "t2m" in z else None
    return {"cb": gated, "fogm": fogm, "pr": z["pr"].astype(np.float32) / 100.0,
            "t2m": t2m, "lat": lat, "lon": lon,
            "run": dt.datetime.strptime(meta["run"], "%Y%m%dT%H").replace(
                tzinfo=dt.timezone.utc)}


_LATLON = None


def _window_latlon():
    """MEPS lat/lon over our window. Fixed grid, so fetched once and cached —
    no reason to carry it in every hour's blob."""
    global _LATLON
    if _LATLON is None:
        s3 = r2_client()
        blob = s3.get_object(Bucket=config.R2_BUCKET,
                             Key=f"{GRID_PREFIX}/latlon.wxg")["Body"].read()
        z, _ = unpack(blob)
        _LATLON = (z["lat"], z["lon"])
    return _LATLON


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
                              r"/(\d{8}T\d{2})\.wxg$"),
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
    pngs = draw_hour(g["cb"], g["fogm"], g["pr"], g["lat"], g["lon"], g.get("t2m"))
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


def build_static() -> dict:
    """Write the three fixed things the edge renderer needs, once.

    latlon.wxg   MEPS lat/lon over our window, so hourly blobs need not carry it
    index.wxg    for every one of the 570x690 output pixels, which source cell
                 it comes from — the whole reprojection, precomputed, because
                 Deno has no pyproj and this grid never moves
    borders.wxg  the coastline and frontier drawn once into a mask

    About 1.5 MB together, fetched once per edge instance and kept in memory.
    """
    import json as _json

    import netCDF4 as nc

    from . import proj3059 as P
    from .maps_meps import BORDERS_FILE, latest_run

    ds = nc.Dataset(DAP_BASE + latest_run())
    lat = np.array(ds.variables["latitude"][:])
    lon = np.array(ds.variables["longitude"][:])
    win = (lat >= BBOX[0]) & (lat <= BBOX[1]) & (lon >= BBOX[2]) & (lon <= BBOX[3])
    ys, xs = np.where(win)
    y0, y1, x0, x1 = ys.min(), ys.max(), xs.min(), xs.max()
    latw = lat[y0:y1 + 1, x0:x1 + 1].astype(np.float32)
    lonw = lon[y0:y1 + 1, x0:x1 + 1].astype(np.float32)
    rows, cols = latw.shape

    # Nearest source cell for every output pixel. Brute force over a 300x248
    # source is 29 billion distance pairs, so go via the projected plane and a
    # KD-tree instead: seconds, and exact.
    from scipy.spatial import cKDTree

    sx, sy = P.to_xy(lonw.ravel(), latw.ravel())
    tree = cKDTree(np.c_[sx, sy])
    gx, gy = np.meshgrid(np.arange(P.W), np.arange(P.H))
    mx = P.X0 + (gx + 0.5) * (P.X1 - P.X0) / P.W
    my = P.Y1 - (gy + 0.5) * (P.Y1 - P.Y0) / P.H
    dist, idx = tree.query(np.c_[mx.ravel(), my.ravel()], k=1)
    # Beyond half a grid step there is no data, only extrapolation.
    idx = np.where(dist <= 3500, idx, 0xFFFFFFFF).astype(np.uint32)

    # Borders, rasterised the same way the Python renderer draws them.
    from PIL import Image as PILImage
    from PIL import ImageDraw

    im = PILImage.new("L", (P.W, P.H), 0)
    d = ImageDraw.Draw(im)
    for c in _json.load(open(BORDERS_FILE)):
        for line in c["lines"]:
            px, py = P.to_px([la for _, la in line], [lo for lo, _ in line])
            pts = list(zip(px.tolist(), py.tolist()))
            d.line(pts, fill=1, width=3)      # halo
            d.line(pts, fill=2, width=1)      # core
    border = np.array(im, dtype=np.uint8)

    s3 = r2_client()
    out = {}
    for name, arrays, meta in (
            ("latlon", {"lat": latw, "lon": lonw}, {"rows": rows, "cols": cols}),
            ("index", {"idx": idx}, {"w": P.W, "h": P.H, "rows": rows, "cols": cols}),
            ("borders", {"mask": border}, {"w": P.W, "h": P.H})):
        blob = pack(arrays, meta)
        s3.put_object(Bucket=config.R2_BUCKET, Key=f"{GRID_PREFIX}/{name}.wxg",
                      Body=blob, ContentType="application/octet-stream",
                      CacheControl="public, max-age=31536000")
        out[name] = len(blob)
    log.info("static: %s", ", ".join(f"{k} {v/1024:.0f} KiB" for k, v in out.items()))
    return out
