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

The test is "is there a newer run than the one on screen", not "is the run on
screen old". Those sound alike and are not: a run's age keeps climbing however
often you redraw it, so an age limit a producer's own cadence cannot meet
fires forever. GFS publishes six-hourly about five hours late, so its newest
available run is routinely 5-11 h old and a 7 h limit was never satisfiable —
the tick re-rendered it every hour, redrew the same run, and found it stale
again. Between that and the same thing on the UM side the hourly tick was
averaging 14.4 minutes and 402 billed minutes a day over 04-06.08.

Comparing run tags settles it: once the newest run has been drawn there is
nothing to do until the next one publishes. MIN_REDRAW_H is the floor for the
other case — a newer run exists but cannot be fetched, which is where UM has
been since 02.08 — so that retries a few times a day instead of every hour.

One output per product. They used to share a flag, so a UM run that would not
advance dragged MEPS through a full re-render beside it every time.
"""
import datetime as dt
import json
import os
import re
import sys
import urllib.request

BASE = os.environ.get("MAPS_BASE", "https://pub-29a41af0b6de4fe9a0d144b6a88fa144.r2.dev")

# What each producer's newest PUBLISHED run is, deduced from the clock: cycle
# length and how long after a cycle it appears. Same idea as model_cycle() in
# the Open-Meteo ingest, and the same numbers where they overlap.
#
# This exists because the age test on its own could not be satisfied. GFS runs
# every six hours and publishes about five hours late, so its newest available
# run is routinely 5-11 h old — permanently past a 7 h limit. Re-rendering
# redrew the same run, the age kept climbing, and the next tick re-rendered it
# again. Measured over 04-06.08 that loop, together with the same thing on the
# UM side, ran the hourly tick at 14.4 minutes a go for 402 billed minutes a
# day. The question is not how old the run is, it is whether a newer one
# exists — and once it has been drawn, the answer is no.
CYCLE_H = {"meps": 3, "um4": 12, "gfs": 6}
DELAY_H = {"meps": 2, "um4": 5, "gfs": 5}
# ...and a floor under how often the same product may be redrawn, for the case
# a newer run exists but cannot be fetched. UM has been stuck on 02.08 12Z for
# four days; without this it would be retried every hour forever.
MIN_REDRAW_H = float(os.environ.get("MAPS_MIN_REDRAW_H", "3"))
# Backstop only, for a manifest whose run tag will not parse at all.
MAX_AGE_H = float(os.environ.get("MAPS_MAX_AGE_H", "8"))


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


def newest_run(kind: str, now: dt.datetime) -> str:
    """Tag of the newest run of `kind` that could already be published."""
    cyc, dly = CYCLE_H.get(kind, 6), DELAY_H.get(kind, 5)
    t = now - dt.timedelta(hours=dly)
    return t.replace(minute=0, second=0, microsecond=0,
                     hour=(t.hour // cyc) * cyc).strftime("%Y%m%d%H")


def drawn_run(path: str) -> str | None:
    d = _read(path)
    if not d:
        return None
    m = re.search(r"(\d{8})T(\d{2})", str(d.get("run", "")))
    return (m.group(1) + m.group(2)) if m else None


def main() -> int:
    flags = {"meps": False, "um4": False, "gfs": False}
    # GFS is watched separately because it is repaired separately: it needs
    # eccodes, which the tick does not carry, so it gets its own output and its
    # own install rather than riding along with the MEPS/UM catch-up.
    #
    # It is watched at all because it can fail silently. Its step in models.yml
    # is continue-on-error — a NOAA outage must not take the twice-daily pass
    # down — and on 01.08 that swallowed a NameError in the colour setup, which
    # fired before the per-lead try block and before the "nothing rendered"
    # guard could see it. The step went green having produced nothing, and days
    # 3-7 of the map column quietly sat on the 01.08 12Z run until someone ran
    # the backfill by hand and watched it fail. Nothing was wrong with the data
    # and nothing said so.
    for path in ("maps/um4/latest.json", "maps/meps/latest.json",
                 "maps/gfs/latest.json"):
        drawn, run = ages(path)
        print(f"{path}: drawn {'?' if drawn is None else f'{drawn:.1f} h'} ago, "
              f"run {'?' if run is None else f'{run:.1f} h'} old")
        kind = path.split("/")[1]
        have, want = drawn_run(path), newest_run(kind, dt.datetime.now(dt.timezone.utc))
        behind = have is not None and have < want
        # The backstop is now only for a manifest whose run tag will not parse.
        # Everything else is decided by "is there a newer run than the one on
        # screen", which a re-render actually settles.
        old = behind or (have is None and drawn is not None and drawn > MAX_AGE_H)
        if old and drawn is not None and drawn < MIN_REDRAW_H:
            print(f"  behind ({have} < {want}) but redrawn {drawn:.1f} h ago "
                  f"— waiting for {MIN_REDRAW_H:.0f} h")
            old = False
        elif behind:
            print(f"  newer run available: {have} -> {want}")
        if not old:
            continue
        flags[kind] = True
    print("-> " + ", ".join(
        f"{k} {'re-rendering' if v else 'fresh'}" for k, v in flags.items()))

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as fh:
            for k, v in flags.items():
                fh.write(f"{'um' if k == 'um4' else k}={'1' if v else '0'}\n")
            # Kept so an unedited workflow still works: the old single flag.
            fh.write(f"maps={'1' if (flags['meps'] or flags['um4']) else '0'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
