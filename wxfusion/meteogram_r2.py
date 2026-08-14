"""Publish per-point meteogram history to R2, one small JSON file per point.

The page's historical meteogram — past weeks, "the freshest run that covered
each hour" — is read from Postgres through wx_point_window (forecasts) and
wx_point_obs_window (observations). Those two RPCs are the only reason
wx.forecasts_h must keep a fortnight and wx.observations_h ninety days, and
between them those tables are almost the whole 500 MB database. Everything they
return is already archived to R2 as Parquet; this republishes it in the exact
shape the page consumes, so the page can read history straight from the bucket
and the hot tables can shrink to what the nightly pairing job needs.

Additive on its own: it only reads Postgres and writes side-channel files,
exactly like write_fog_cells. Nothing depends on it until the page is pointed
at these files (phase 2) and the retention windows are cut (phase 4).

Incremental and rolling. Each pass reads the freshest rows from whatever window
is still hot, merges them over the file already in R2 — a newer run wins for a
given hour, older hours are left as they were — and drops anything past the
keep window. So the published fortnight survives even once forecasts_h holds
only a couple of days: the tail is refreshed, the head is what earlier passes
already wrote.

Layout (the same public bucket the maps use, config.MAPS_BASE):
  meteogram/fcst/{point_id}.json   array of wx_point_window rows
  meteogram/obs/{point_id}.json    array of {t, p, v} (wx_point_obs_window shape)
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
import os
from urllib.parse import quote

from botocore.exceptions import ClientError

from . import archive, config

log = logging.getLogger(__name__)

# How much history each point's file carries. The page pages two weeks back
# (WEEKS_BACK=2), and the oldest visible Monday can be up to twenty days ago, so
# twenty-one days covers the furthest hour it can show. The forecast side of the
# hot table only holds a fortnight, so a fresh file starts with fourteen days of
# forecasts — but the merge accumulates, so within a week the file carries the
# full twenty-one, which is more archived forecast history than wx_point_window
# could ever return. Overridable so phase 4 can tune it without a code change.
KEEP_DAYS = 21

FCST_PREFIX = "meteogram/fcst/"
OBS_PREFIX = "meteogram/obs/"

# ISO 8601 with an explicit +00:00 offset, byte-for-byte what PostgREST returns
# for a timestamptz and what the page's live Open-Meteo path already builds — so
# a forecast hour and its observation share one time key across both sources.
_ISO = 'YYYY-MM-DD"T"HH24:MI:SS+00:00'

# The freshest run per (point, model, valid_time), i.e. exactly what
# wx_point_window collapses forecasts_h to, but for every point at once. The
# columns and their order mirror that function so the file is a drop-in.
FCST_SQL = f"""
select point_id, model as m,
       to_char(run_time   at time zone 'UTC', '{_ISO}') as run,
       to_char(valid_time at time zone 'UTC', '{_ISO}') as t,
       t2m, td2m, rh, ws10m, wg10m, wdir, prcp_1h, pres_msl,
       cc_low, cc_mid, cc_high, cc_total, cb_lcl, cb
from (
  select distinct on (point_id, model, valid_time) *
  from wx.forecasts_h
  where valid_time >= now() - make_interval(days => %(days)s)
  order by point_id, model, valid_time, run_time desc
) f
order by point_id, m, t
"""

_FCST_VALS = ["t2m", "td2m", "rh", "ws10m", "wg10m", "wdir", "prcp_1h",
              "pres_msl", "cc_low", "cc_mid", "cc_high", "cc_total",
              "cb_lcl", "cb"]

# The observation side of the same window, unpivoted to {t, p, v} — the shape
# wx_point_obs_window returns. The point join is station-only, matching that
# function (the radar-derived rows pairing also reads are not shown here).
OBS_SQL = f"""
select pt.point_id,
       to_char(o.time at time zone 'UTC', '{_ISO}') as t,
       x.parameter as p, round(x.value::numeric, 2) as v
from wx.observations_h o
join wx.points pt on pt.kind = o.source and pt.station_id = o.station_id
cross join lateral (values
    ('t2m', o.t2m), ('td2m', o.td2m), ('rh', o.rh), ('ws10m', o.ws10m),
    ('wg10m', o.wg10m), ('wdir', o.wdir), ('prcp_1h', o.prcp_1h),
    ('pres_msl', o.pres_msl), ('cc_total', o.cc_total), ('vis', o.vis),
    ('cb_ceiling', o.cb_ceiling), ('snow', o.snow), ('wx_rain', o.wx_rain)
  ) as x(parameter, value)
where o.time >= now() - make_interval(days => %(days)s)
  and x.value is not null
order by pt.point_id, o.time
"""


def _fcst_rows(conn, days: int) -> dict[str, list[dict]]:
    """point_id -> [row], each row shaped like a wx_point_window result."""
    out: dict[str, list[dict]] = {}
    with conn.cursor() as cur:
        cur.execute(FCST_SQL, {"days": days})
        for r in cur:
            pid, m, run, t = r[0], r[1], r[2], r[3]
            # float4 widened to a Python float prints a long tail (12.3 ->
            # 12.300000190734863). Three decimals is finer than any of these
            # quantities is measured and drops the artefact.
            row = {"m": m, "run": run, "t": t}
            for name, val in zip(_FCST_VALS, r[4:]):
                if val is not None:
                    row[name] = round(float(val), 3)
            out.setdefault(pid, []).append(row)
    return out


def _obs_rows(conn, days: int) -> dict[str, list[dict]]:
    """point_id -> [{t, p, v}], shaped like a wx_point_obs_window result."""
    out: dict[str, list[dict]] = {}
    with conn.cursor() as cur:
        cur.execute(OBS_SQL, {"days": days})
        for pid, t, p, v in cur:
            out.setdefault(pid, []).append({"t": t, "p": p, "v": float(v)})
    return out


def _all_points(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("select point_id from wx.points order by point_id")
        return [r[0] for r in cur]


def _get_json(s3, key: str):
    """The list already stored under key, or None if there is no file yet.

    A missing object is the normal case on the first pass — every point starts
    without a file — so it must come back as None, not an error, or nothing is
    ever written. R2 reports it as NoSuchKey/404 on GetObject.
    """
    try:
        obj = s3.get_object(Bucket=config.R2_BUCKET, Key=key)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise
    data = obj["Body"].read()
    if obj.get("ContentEncoding") == "gzip" or data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return json.loads(data)


def _put_json(s3, key: str, rows: list) -> int:
    """gzip so a fortnight of a point is a few kB on the wire; fetch() inflates
    it transparently from the Content-Encoding. Returns the stored byte count."""
    raw = json.dumps(rows, separators=(",", ":")).encode()
    body = gzip.compress(raw, compresslevel=6)
    s3.put_object(
        Bucket=config.R2_BUCKET, Key=key, Body=body,
        ContentType="application/json", ContentEncoding="gzip",
        CacheControl="public, max-age=600",
    )
    return len(body)


def _merge(existing: list[dict] | None, fresh: list[dict],
           key, cutoff: str) -> list[dict]:
    """Fresh rows win on a key collision, older rows are kept, anything before
    the cutoff time is dropped. Sorted by time so the file stays readable.

    Times are compared as strings: every one is the same-width ISO 8601 with a
    +00:00 offset, so lexical order is chronological and no parsing is needed.
    """
    merged: dict = {}
    for row in existing or []:
        if row["t"] >= cutoff:
            merged[key(row)] = row
    for row in fresh:
        if row["t"] >= cutoff:
            merged[key(row)] = row
    return sorted(merged.values(), key=lambda r: (r["t"], key(r)))


def verify(conn, pubkey: str, sample: int = 8, week_days: int = 7) -> dict:
    """Fetch published files over the PUBLIC url — the browser's exact path,
    gzip and all — and confirm they equal what the page's RPCs return for the
    same window. This is the gate the retention cut waits on: if the page were
    silently falling back to the RPCs, cutting the tables would blank it, so
    prove the R2 path actually serves the same rows first.

    Compares R2 (public, no credentials) against wx_point_window /
    wx_point_obs_window (via PostgREST, the publishable key) — both exactly the
    two sources the browser reaches. Raises AssertionError on any mismatch.
    """
    import datetime as _dt

    import requests

    supa = os.environ.get("SUPABASE_URL", "https://xxxywjxmdpildafzbnpj.supabase.co")
    base = config.MAPS_BASE
    now = _dt.datetime.now(_dt.timezone.utc)
    # Compare only the settled window: the newest couple of hours are where a
    # just-arrived observation, a fresh run, or a public-cache lag (max-age 600)
    # legitimately make the file and the live RPC differ for a moment. Below
    # this cutoff both are stable, so any difference there is a real fault.
    lo = (now - _dt.timedelta(days=week_days)).strftime("%Y-%m-%dT%H:%M:%S")
    hi = (now - _dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
    p_start = lo + "+00:00"
    p_end = now.strftime("%Y-%m-%dT%H:%M:%S+00:00")

    # Station points with recent obs, so both files are non-trivial.
    with conn.cursor() as cur:
        cur.execute("""
            select pt.point_id
            from wx.points pt
            join wx.observations_h o on o.source=pt.kind and o.station_id=pt.station_id
            where o.time > now() - interval '2 days'
            group by pt.point_id
            order by pt.point_id
            limit %s""", (sample,))
        points = [r[0] for r in cur]
    if not points:
        raise AssertionError("verify: no sample points with recent observations")

    hdr = {"apikey": pubkey, "Authorization": f"Bearer {pubkey}",
           "Content-Type": "application/json"}

    def _rpc(fn, pid):
        r = requests.post(f"{supa}/rest/v1/rpc/{fn}",
                          json={"p_point_id": pid, "p_start": p_start, "p_end": p_end},
                          headers=hdr, timeout=60)
        r.raise_for_status()
        return r.json()

    def _r2(kind, pid):
        # requests inflates Content-Encoding: gzip transparently, same as fetch().
        r = requests.get(f"{base}/meteogram/{kind}/{quote(pid, safe='')}.json", timeout=60)
        r.raise_for_status()
        return r.json()

    def _in(t):
        return lo <= t[:19] < hi

    checked = 0
    for pid in points:
        # forecasts: freshest run per (model, valid_time), settled window only.
        # R2 is a SUPERSET here, not an equal: the hot tier the RPC reads is
        # pruned to FORECAST_KEEP_DAYS (5), while the published file keeps the
        # full KEEP_DAYS (21) window this verify samples (7 days). So forecasts
        # 5-7 days old are in R2 but no longer in the RPC — expected, that is the
        # archive outliving the hot tier. Require every key the RPC still serves
        # to be present and equal in R2 (which catches R2 missing anything the
        # page needs); allow R2 to carry the older ones the RPC has dropped.
        rpc_f = {(x["m"], x["t"][:19]): x for x in _rpc("wx_point_window", pid) if _in(x["t"])}
        r2_f = {(x["m"], x["t"][:19]): x for x in _r2("fcst", pid) if _in(x["t"])}
        assert set(rpc_f) <= set(r2_f), (
            f"{pid} fcst: keys the RPC serves are missing from r2 "
            f"(rpc={len(rpc_f)} r2={len(r2_f)}): "
            f"{list(set(rpc_f) - set(r2_f))[:3]}")
        for k, rv in rpc_f.items():
            for p in _FCST_VALS:
                a, b = rv.get(p), r2_f[k].get(p)
                if a is None or b is None:
                    assert (a is None) == (b is None) or abs((a or 0) - (b or 0)) < 0.01, \
                        f"{pid} {k} {p}: rpc={a} r2={b}"
                else:
                    assert abs(float(a) - float(b)) < 0.01, f"{pid} {k} {p}: rpc={a} r2={b}"
        # observations: {t,p,v}, settled window only
        rpc_o = {(x["t"][:19], x["p"]): float(x["v"]) for x in _rpc("wx_point_obs_window", pid) if _in(x["t"])}
        r2_o = {(x["t"][:19], x["p"]): float(x["v"]) for x in _r2("obs", pid) if _in(x["t"])}
        assert set(rpc_o) == set(r2_o), (
            f"{pid} obs keys differ: rpc={len(rpc_o)} r2={len(r2_o)}")
        for k, v in rpc_o.items():
            assert abs(v - r2_o[k]) < 0.01, f"{pid} obs {k}: rpc={v} r2={r2_o[k]}"
        checked += 1
        log.info("verify: %s ok (fcst %d, obs %d rows)", pid, len(rpc_f), len(rpc_o))

    log.info("verify: %d points match the RPCs over the public R2 path", checked)
    return {"checked": checked, "points": points}


def publish(conn, keep_days: int = KEEP_DAYS, hot_days: int | None = None,
            only: list[str] | None = None) -> dict:
    """Read the hot window, merge each point over its R2 file, write it back.

    hot_days is how far back to read the (possibly already shrunk) hot tables;
    it defaults to keep_days, which is right while the tables still hold at
    least that much. only= limits the point set, for verifying a handful
    against the RPCs before trusting the whole run.
    """
    hot_days = hot_days if hot_days is not None else keep_days
    cutoff = (dt.datetime.now(dt.timezone.utc)
              - dt.timedelta(days=keep_days)).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    fcst = _fcst_rows(conn, hot_days)
    obs = _obs_rows(conn, hot_days)
    points = only or _all_points(conn)
    s3 = archive.r2_client()

    n_files = 0
    n_bytes = 0
    for pid in points:
        for prefix, fresh_by_pid, keyfn in (
            (FCST_PREFIX, fcst, lambda r: (r["m"], r["t"])),
            (OBS_PREFIX, obs, lambda r: (r["t"], r["p"])),
        ):
            key = f"{prefix}{pid}.json"
            fresh = fresh_by_pid.get(pid, [])
            try:
                existing = _get_json(s3, key)
            except Exception:
                log.exception("meteogram: read failed for %s", key)
                continue
            # Nothing new and nothing stored, or nothing survives the cutoff:
            # do not write an empty file.
            merged = _merge(existing, fresh, keyfn, cutoff)
            if not merged:
                continue
            try:
                n_bytes += _put_json(s3, key, merged)
                n_files += 1
            except Exception:
                log.exception("meteogram: write failed for %s", key)

    log.info("meteogram: %d files written, %.0f kB total, keep=%dd hot=%dd",
             n_files, n_bytes / 1024, keep_days, hot_days)
    return {"files": n_files, "bytes": n_bytes}
