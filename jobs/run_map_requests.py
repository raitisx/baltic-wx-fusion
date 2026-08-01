"""Render the hours the page asked for.

The page is static files on a CDN, so there is nothing to render on demand at
the moment of the click. Instead a click on a date with no frames writes a row
through an RPC, and this — running on the hourly tick — draws it. Worst case
the picture appears an hour later, which is the honest cost of having no
server; the page says so while it waits.

Inside the year the grid is already in R2 and this is a second's work. Outside
it, the hour is pulled from MET's archive first. Either way the frames land in
maps/meps_req/ and are swept a day later.
"""
import datetime as dt
import logging
import sys

sys.path.insert(0, ".")
from wxfusion import db, meps_grid  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("run_map_requests")


def main() -> int:
    conn = db.connect()
    with conn.cursor() as cur:
        cur.execute("select * from public.wx_pending_maps()")
        hours = [r[0] for r in cur.fetchall()]
    if not hours:
        log.info("no map requests pending")
        conn.close()
        return 0
    log.info("%d hours requested", len(hours))

    for hour in hours:
        hour = hour.astimezone(dt.timezone.utc).replace(minute=0, second=0,
                                                        microsecond=0)
        note = "from grid"
        try:
            if not meps_grid.have_hour(hour):
                note = "fetched from MET archive"
                if not meps_grid.fetch_hour_live(hour):
                    note = "not in the archive"
                    raise RuntimeError(note)
            n = meps_grid.render_hour(hour, prefix="req")
            if not n:
                raise RuntimeError("nothing rendered")
            log.info("%s: %d frames (%s)", hour, n, note)
        except Exception as exc:
            note = f"{note}: {exc}"
            log.exception("%s failed", hour)
        with conn.cursor() as cur:
            cur.execute("update wx.map_requests set done_at=now(), note=%s "
                        "where hour=%s", (note[:200], hour))
        conn.commit()
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
