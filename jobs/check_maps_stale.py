"""Is the map imagery stale enough that the hourly tick should re-render it?

GitHub delays scheduled runs heavily on this repo — the twice-daily models
workflow has fired as much as 90 min late, and a skipped pass leaves the maps
on the previous day's model run. That matters beyond freshness: from a run
that old the current day sits past meteo.pl's +24 h hourly window, so the UM
column only changes every 3 hours instead of every hour.

The hourly tick fires far more reliably, so it checks this and repairs it.
Reads the public manifest — no credentials needed. Writes `maps=1|0` to
GITHUB_OUTPUT; on any error it stays quiet (0) rather than triggering an
expensive render on a transient blip.
"""
import datetime as dt
import json
import os
import sys
import urllib.request

BASE = os.environ.get("MAPS_BASE", "https://pub-29a41af0b6de4fe9a0d144b6a88fa144.r2.dev")
# 8 h: longer than a normal 12 h models cadence leaves it right after a pass,
# short enough to catch a skipped pass within the same working day.
MAX_AGE_H = float(os.environ.get("MAPS_MAX_AGE_H", "8"))


def age_hours(path: str) -> float | None:
    try:
        # Explicit UA: some proxies reject urllib's default outright.
        req = urllib.request.Request(
            f"{BASE}/{path}", headers={"User-Agent": "baltic-wx-fusion/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            generated = json.load(r)["generated_at"]
        stamp = dt.datetime.fromisoformat(generated)
        return (dt.datetime.now(dt.timezone.utc) - stamp).total_seconds() / 3600
    except Exception as exc:  # noqa: BLE001 — never fail the tick over this
        print(f"could not read {path}: {exc.__class__.__name__}")
        return None


def main() -> int:
    ages = {p: age_hours(p) for p in
            ("maps/um4/latest.json", "maps/meps/latest.json")}
    for path, age in ages.items():
        print(f"{path}: {'unknown' if age is None else f'{age:.1f} h old'}")

    known = [a for a in ages.values() if a is not None]
    stale = bool(known) and max(known) > MAX_AGE_H
    print(f"-> {'stale, re-rendering' if stale else 'fresh, nothing to do'}"
          f" (threshold {MAX_AGE_H:.0f} h)")

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as fh:
            fh.write(f"maps={'1' if stale else '0'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
