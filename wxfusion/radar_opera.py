"""EUMETNET Open Radar Data (ORD) client — OPERA composites, CC BY 4.0.

VERIFIED against the live API (2026-07-27):
- GET {ORD_BASE}/collections/observations           -> parameter list
  (ACRR:comp = 1 h rainfall accumulation, RATE:comp = rain rate,
   DBZH:comp = max reflectivity; 15-min cadence, rolling ~24 h cache)
- GET .../observations/locations                    -> 139 radar sites +
  '0-20010-0-OPERA' (the pan-European composite)
- GET .../observations/locations/0-20010-0-OPERA
      ?parameter-name=ACRR:comp&datetime=START/END  -> CoverageJSON
  PointSeries with real values.
Baltic single sites present: EE eehar/eesur, LT ltlau/ltvil. LV radars are
NOT in ORD yet (LVGMC not flowing) — composite still covers LV via
neighbours' overlap.

LIMITATION: the anonymous EDR tier serves point/area *extractions*, not the
full ODIM HDF5 rasters. Raster files come via the MQTT notification service +
authenticated downloads (free API key) — wire that up in Phase 2/3 when map
verification needs the full grid. Until then this module supports:
  - archive_composite_series(): dump recent composite point-series JSON to R2
    (cheap continuous archive, keeps the "why we didn't fly" history growing)
  - point_series(): ad-hoc series for any parameter/location

Register for an API key + MQTT: see
https://eumetnet.github.io/openradardata-documentation/
"""
from __future__ import annotations

import datetime as dt
import json
import logging

import boto3

from . import config
from .http import session

log = logging.getLogger(__name__)

OBS = "/collections/observations"
OPERA_COMPOSITE_LOCATION = "0-20010-0-OPERA"


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


def discover_collections() -> dict:
    r = session().get(f"{config.ORD_BASE}{OBS}", timeout=60)
    r.raise_for_status()
    return r.json()


def point_series(
    location: str, parameter: str, hours: int = 3
) -> dict | None:
    """CoverageJSON PointSeries for a location id, or None if empty (204)."""
    now = dt.datetime.now(dt.timezone.utc)
    start = now - dt.timedelta(hours=hours)
    r = session().get(
        f"{config.ORD_BASE}{OBS}/locations/{location}",
        params={
            "parameter-name": parameter,
            "datetime": f"{start:%Y-%m-%dT%H:%M:%SZ}/{now:%Y-%m-%dT%H:%M:%SZ}",
        },
        timeout=90,
    )
    if r.status_code == 204 or not r.content:
        return None
    r.raise_for_status()
    return r.json()


def archive_composite_series() -> None:
    """Store the last 3 h of composite ACRR/RATE point-series in R2."""
    now = dt.datetime.now(dt.timezone.utc)
    stored = 0
    for pname in ("ACRR:comp", "RATE:comp"):
        cov = point_series(OPERA_COMPOSITE_LOCATION, pname, hours=3)
        if cov is None:
            log.warning("ORD: no data for %s", pname)
            continue
        key = (
            f"radar/opera-series/{pname.replace(':', '_')}/"
            f"{now:%Y/%m/%d}/{now:%H%M}.json"
        )
        r2_client().put_object(
            Bucket=config.R2_BUCKET,
            Key=key,
            Body=json.dumps(cov).encode(),
            ContentType="application/prs.coverage+json",
        )
        stored += 1
        log.info("ORD: stored %s", key)
    if not stored:
        log.warning("ORD: nothing stored this cycle")
