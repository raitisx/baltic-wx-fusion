"""Latvian UAS geographical zones (ED-269) into Postgres, with history.

Latvijas Gaisa Satiksme publish every zone in one EUROCAE ED-269
UASZoneVersion document, listed at drz.lv/api/v1/export-history with an
effective-from stamp, a CRC32-Q and an MD5. The document is a SNAPSHOT of
the whole set, so nothing in it says what changed since yesterday — that is
worked out here by hashing each zone's own record.

A zone whose record is byte-identical to the stored one just has its
last_seen moved forward. An edited zone opens a new version row. A zone that
stops appearing is closed with withdrawn_at, not deleted, so a year later it
is still possible to ask what the map looked like on a given day.

Data is published by LGS under the geo-awareness obligation of EU 2019/947
Art 15; see CREDIT.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re

from . import config, db
from .http import session

log = logging.getLogger(__name__)

LISTING = "https://drz.lv/api/v1/export-history/iframe-eng"
FILE_BASE = "https://drz.lv/api/v1/export-history/UASZoneVersion"
CREDIT = ("UAS geographical zones: Latvijas Gaisa Satiksme (LGS) / "
          "Civil Aviation Agency of Latvia, published under EU 2019/947 "
          "Art 15 (EUROCAE ED-269)")
KEEP_DAYS = 365


def _listing(s):
    """(file name, effective_from, crc32q, md5) for the current document."""
    r = s.get(LISTING, timeout=60)
    r.raise_for_status()
    txt = re.sub(r"<[^>]+>", "\t", r.text)
    txt = re.sub(r"[ \xa0]+", " ", txt)
    name = re.search(r"(UASZoneVersion_[A-Za-z0-9_]+\.json)", txt)
    if not name:
        raise RuntimeError("the export listing named no UASZoneVersion file")
    name = name.group(1)
    # The row reads: effective-from, CRC32-Q, MD5, file name. The stamp and
    # the md5 are both in the file name too, which is what we trust.
    stamp = re.search(r"UASZoneVersion_(\d{4})_(\d{2})_(\d{2})T"
                      r"(\d{2})_(\d{2})_(\d{2})_(\d+)Z_([0-9A-Fa-f]{32})", name)
    if not stamp:
        raise RuntimeError(f"cannot read the stamp out of {name}")
    y, mo, d, h, mi, sec, _frac, md5 = stamp.groups()
    eff = dt.datetime(int(y), int(mo), int(d), int(h), int(mi), int(sec),
                      tzinfo=dt.timezone.utc)
    crc = re.search(r"\b([0-9A-F]{8})\b", txt)
    return name, eff, (crc.group(1) if crc else None), md5.upper()


def fetch(s=None) -> tuple[dict, str, dt.datetime, str | None, str]:
    """The current ED-269 document, with what the listing said about it."""
    s = s or session()
    name, eff, crc, md5 = _listing(s)
    last = None
    for url in (f"{FILE_BASE}/{name}", FILE_BASE,
                f"https://drz.lv/api/v1/export-history/{name}"):
        try:
            r = s.get(url, timeout=180)
        except Exception as e:                      # noqa: BLE001
            last = e
            continue
        if r.ok and r.content[:1].strip() in (b"{", b"["):
            log.info("geozones: %s, %d bytes, effective %s",
                     name, len(r.content), eff)
            return r.json(), name, eff, crc, md5
        last = RuntimeError(f"{url} -> HTTP {r.status_code}")
    raise RuntimeError(f"could not download {name}: {last}")


def _bbox(geometry):
    """(min lon, min lat, max lon, max lat) over every ring of a zone."""
    xs, ys = [], []

    def walk(c):
        if (isinstance(c, (list, tuple)) and len(c) == 2
                and all(isinstance(v, (int, float)) for v in c)):
            xs.append(float(c[0]))
            ys.append(float(c[1]))
        elif isinstance(c, (list, tuple)):
            for x in c:
                walk(x)

    for g in geometry or []:
        hp = g.get("horizontalProjection") or {}
        walk(hp.get("coordinates"))
        # A circle is a centre and a radius, not a ring: box it by hand.
        ctr = hp.get("center") or hp.get("centre")
        rad = hp.get("radius")
        if ctr and rad:
            import math
            lon, lat = float(ctr[0]), float(ctr[1])
            dlat = float(rad) / 111_320.0
            dlon = dlat / max(0.1, math.cos(math.radians(lat)))
            xs += [lon - dlon, lon + dlon]
            ys += [lat - dlat, lat + dlat]
    if not xs:
        return (None, None, None, None)
    return (min(xs), min(ys), max(xs), max(ys))


def _ts(v):
    if not v:
        return None
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def _row(z: dict, eff: dt.datetime) -> tuple:
    """One ED-269 zone -> the columns wx.uas_geozones holds."""
    body = json.dumps(z, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    payload_md5 = hashlib.md5(body.encode()).hexdigest()
    ap = (z.get("applicability") or [{}])[0] or {}
    geo = z.get("geometry") or []
    g0 = geo[0] if geo else {}
    lo, la, hi_lon, hi_lat = _bbox(geo)
    return (
        z.get("identifier"), payload_md5, eff, eff,
        z.get("name"), z.get("type"), z.get("restriction"),
        z.get("reason") or None, z.get("otherReasonInfo"), z.get("country"),
        (str(ap.get("permanent", "")).upper() == "YES"),
        _ts(ap.get("startDateTime")), _ts(ap.get("endDateTime")),
        g0.get("lowerLimit"), g0.get("lowerVerticalReference"),
        g0.get("upperLimit"), g0.get("upperVerticalReference"),
        g0.get("uomDimensions"),
        lo, la, hi_lon, hi_lat,
        json.dumps(geo, ensure_ascii=False),
        json.dumps(z, ensure_ascii=False),
    )


COLS = ("identifier, payload_md5, first_seen, last_seen, name, zone_type, "
        "restriction, reason, other_reason, country, permanent, starts_at, "
        "ends_at, lower_limit, lower_ref, upper_limit, upper_ref, uom, "
        "min_lon, min_lat, max_lon, max_lat, geometry, zone")


def ingest(doc: dict, name: str, eff: dt.datetime, crc: str | None,
           md5: str) -> dict:
    """Store one snapshot. Returns what it did, for the log."""
    feats = doc.get("features") if isinstance(doc, dict) else None
    if not isinstance(feats, list):
        raise RuntimeError("the document carries no features array")
    rows = [_row(z, eff) for z in feats if isinstance(z, dict)]
    ids = [r[0] for r in rows]
    out = {"zones": len(rows), "new": 0, "withdrawn": 0, "seen_before": False}
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("select 1 from wx.uas_geozone_files where md5 = %s", (md5,))
        out["seen_before"] = cur.fetchone() is not None
        cur.executemany(
            f"""insert into wx.uas_geozones ({COLS})
                 values ({', '.join(['%s'] * 24)})
              on conflict (identifier, payload_md5) do update
                 set last_seen = greatest(wx.uas_geozones.last_seen,
                                          excluded.last_seen),
                     withdrawn_at = null""",
            rows)
        # Anything live that this snapshot does not mention is gone as of now.
        cur.execute("""update wx.uas_geozones
                          set withdrawn_at = %s
                        where withdrawn_at is null
                          and not (identifier = any(%s))""", (eff, ids))
        out["withdrawn"] = cur.rowcount
        cur.execute("""select count(*) from wx.uas_geozones
                        where first_seen = %s""", (eff,))
        out["new"] = cur.fetchone()[0]
        cur.execute("""insert into wx.uas_geozone_files
                         (md5, effective_from, crc32q, file_name, n_zones,
                          changed)
                       values (%s, %s, %s, %s, %s, %s)
                    on conflict (md5) do update
                         set fetched_at = now()""",
                    (md5, eff, crc, name, len(rows),
                     bool(out["new"] or out["withdrawn"])))
        conn.commit()
    return out


def prune(keep_days: int = KEEP_DAYS) -> dict:
    """A year of history, no more.

    A version is dropped only once it has been withdrawn for longer than the
    window — a zone that is still live keeps its row however old it is, which
    is the point: the current set must never lose a member to pruning.
    """
    cut = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=keep_days)
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("""delete from wx.uas_geozones
                        where withdrawn_at is not null
                          and withdrawn_at < %s""", (cut,))
        zones = cur.rowcount
        cur.execute("""delete from wx.uas_geozone_files
                        where effective_from < %s""", (cut,))
        files = cur.rowcount
        conn.commit()
    return {"zones": zones, "files": files}


def run(keep_days: int = KEEP_DAYS) -> dict:
    doc, name, eff, crc, md5 = fetch()
    out = ingest(doc, name, eff, crc, md5)
    out["pruned"] = prune(keep_days)
    log.info("geozones: %d zones (%d new versions, %d withdrawn), "
             "snapshot %s%s, pruned %s",
             out["zones"], out["new"], out["withdrawn"], eff,
             " (already stored)" if out["seen_before"] else "", out["pruned"])
    return out
