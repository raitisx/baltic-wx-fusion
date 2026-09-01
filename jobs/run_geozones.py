"""Latvian UAS geozones -> Postgres. Twice a day, at 06:00 and 17:00 Riga.

GitHub cron only speaks UTC, and Riga is UTC+2 in winter and UTC+3 in
summer, so the workflow fires at all four candidate hours and this decides
whether the local clock actually says 06 or 17. The two wrong ones cost a
few seconds each and no requests, which is cheaper than being an hour off
for half the year.

GEOZONES_FORCE=1 skips that check, for running it by hand.
"""
import logging
import os
import sys
import zoneinfo
import datetime as dt

sys.path.insert(0, ".")

from wxfusion import geozones  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(levelname)s %(name)s: %(message)s")
HOURS = {6, 17}
KEEP_DAYS = int(os.environ.get("GEOZONES_KEEP_DAYS", geozones.KEEP_DAYS))
FORCE = os.environ.get("GEOZONES_FORCE") == "1"

if __name__ == "__main__":
    now = dt.datetime.now(zoneinfo.ZoneInfo("Europe/Riga"))
    if not FORCE and now.hour not in HOURS:
        print(f"{now:%H:%M %Z} in Riga is not one of "
              f"{sorted(HOURS)}: nothing to do")
        raise SystemExit(0)
    out = geozones.run(KEEP_DAYS)
    print(f"{out['zones']} zones stored ({out['new']} new versions, "
          f"{out['withdrawn']} withdrawn); pruned {out['pruned']}")
    print(geozones.CREDIT)
