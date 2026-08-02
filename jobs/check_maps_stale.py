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

TWO clocks, because one of them measures the wrong thing. How long ago we drew
the pictures is not how fresh they are. Measured on 02.08 at 15:30Z: drawn 8.0
hours ago, but drawn FROM the 03Z run, which was 12.5 hours old with MET
Norway already serving 12Z. On a twice-daily cadence the draw clock only
crosses its threshold just before the next pass would have run anyway, and any
re-render in between resets it, so it can miss a stale run entirely.

What that costs is horizon, which is the part that gets noticed. MEPS reaches
66 hours: the 03Z run ended at 04.08 20Z, so the maps had no 5 August in them
at all, while the 12Z run would already have reached 05.08 06Z.
"""
import datetime as dt
import json
import os
import re
import sys
import urllib.request

BASE = os.environ.get("MAPS_BASE", "https://pub-29a41af0b6de4fe9a0d144b6a88fa144.r2.dev")
# How long since we last drew them. 8 h: longer than a normal 12 h models
# cadence leaves it right after a pass, short enough to catch a skipped pass
# within the same working day.
MAX_AGE_H = float(os.environ.get("MAPS_MAX_AGE_H", "8"))
# How old the model RUN behind them may be. Seven hours lets the twice-daily
# models pass be topped up once in between, which roughly halves the age of
# the run the maps are drawn from and buys back the same in horizon.
MAX_RUN_AGE_H = float(os.environ.get("MAPS_MAX_RUN_AGE_H", "7"))


def _read(path: str) -> dict | None:
    try:
        # Explicit UA: some proxies reject urllib's default outright.
        req = urllib.request.Request(
            f"{BASE}/{path}", headers={"User-Agent": "baltic-wx-fusion/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except Exception as exc:  # noqa: BLE001 — never fail the tick over this
        print(f"could not read {path}: {exc.__class__.__name__}")
        return None


def _since(stamp: dt.datetime) -> float:
    return (dt.datetime.now(dt.timezone.utc) - stamp).total_seconds() / 3600


def ages(path: str) -> tuple[float | None, float | None]:
    """(hours since we drew it, hours since the run it was drawn from)."""
    d = _read(path)
    if not d:
        return None, None
    drawn = run = None
    try:
        drawn = _since(dt.datetime.fromisoformat(d["generated_at"]))
    except Exception:
        pass
    # "20260802T03", or an ncml filename carrying the same stamp
    m = re.search(r"(\d{8})T(\d{2})", str(d.get("run", "")))
    if m:
        try:
            run = _since(dt.datetime.strptime(m.group(1) + m.group(2),
                                              "%Y%m%d%H").replace(
                                                  tzinfo=dt.timezone.utc))
        except Exception:
            pass
    return drawn, run


def main() -> int:
    stale = False
    for path in ("maps/um4/latest.json", "maps/meps/latest.json"):
        drawn, run = ages(path)
        print(f"{path}: drawn {'?' if drawn is None else f'{drawn:.1f} h'} ago, "
              f"run {'?' if run is None else f'{run:.1f} h'} old")
        if drawn is not None and drawn > MAX_AGE_H:
            stale = True
        if run is not None and run > MAX_RUN_AGE_H:
            stale = True
    print(f"-> {'stale, re-rendering' if stale else 'fresh, nothing to do'} "
          f"(drawn > {MAX_AGE_H:.0f} h or run > {MAX_RUN_AGE_H:.0f} h)")

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as fh:
            fh.write(f"maps={'1' if stale else '0'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
