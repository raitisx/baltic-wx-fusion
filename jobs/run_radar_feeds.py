"""Render the per-feed debug frames (docs/radar_feeds.html).

Two modes. RADAR_FEEDS_DAY renders one UTC day, optionally narrowed to an
hour window — that is the manual dispatch, used to go back over a day worth
studying. RADAR_FEEDS_TAIL=<hours> renders the last few hours instead, which
is what the tick runs: it keeps TODAY on the page, topped up as it happens,
so the debug view is never a day behind the composite.
"""
import datetime as dt
import logging
import os
import sys

sys.path.insert(0, ".")

from wxfusion import radar_store  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(levelname)s %(name)s: %(message)s")
DAY = os.environ.get("RADAR_FEEDS_DAY") or None
FROM_H = os.environ.get("RADAR_FEEDS_FROM") or None
TO_H = os.environ.get("RADAR_FEEDS_TO") or None
TAIL_H = os.environ.get("RADAR_FEEDS_TAIL") or None


def tail(hours: float) -> int:
    """The last `hours` up to now, split at UTC midnight.

    A window that starts before midnight belongs to two day archives, and the
    older one is finished off first — otherwise the last slots of yesterday
    are never rendered by anyone, the tick having rolled over to today.
    """
    now = dt.datetime.now(dt.timezone.utc)
    lo = now - dt.timedelta(hours=hours)
    done, d = 0, lo.date()
    while d <= now.date():
        first = d == lo.date()
        last = d == now.date()
        done += radar_store.render_feeds(
            d.strftime("%Y%m%d"),
            from_h=lo.hour if first else 0,
            to_h=min(24, now.hour + 1) if last else 24,
            keep_rendered=True)
        d += dt.timedelta(days=1)
    return done


if __name__ == "__main__":
    if TAIL_H:
        n = tail(float(TAIL_H))
        print(f"{n} per-feed frames rendered for the last {TAIL_H} h")
    elif DAY:
        n = radar_store.render_feeds(
            DAY, from_h=int(FROM_H) if FROM_H else None,
            to_h=int(TO_H) if TO_H else None)
        print(f"{n} per-feed frames rendered for {DAY}")
    else:
        raise SystemExit("set RADAR_FEEDS_DAY=YYYYMMDD (UTC) or "
                         "RADAR_FEEDS_TAIL=<hours>")
