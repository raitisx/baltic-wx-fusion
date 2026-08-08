"""Rebuild wx.pair_stats_daily from the comparison files in R2.

The daily totals are derived data: every one of them can be recomputed from
the Parquet day files under archive/pairs/, which are the record of what was
actually compared. This is the job that does it.

It exists because the totals and the evidence age at different speeds. A
pairing pass rebuilds the last three days from the forecasts still in the hot
table, and those are pruned on their own schedule — so a day re-totalled after
its long leads have been dropped comes out smaller than it was. Pairing now
refuses to overwrite a day with a thinner one, but 05.08 and 06.08 were
already thinned before that landed (698k comparisons down to 327k on the 5th),
and only the archive still knows the full figures.

Reads only. Writes nothing to R2, deletes nothing, and applies the same rule
pairing does: a day is replaced only when the new count is at least the old
one, so running this can restore a total but never shrink it.

Env:
  PAIR_STATS_FROM  first day, YYYY-MM-DD (default: 30 days ago)
  PAIR_STATS_TO    last day, YYYY-MM-DD (default: yesterday)
"""
import datetime as dt
import io
import logging
import os
import sys

sys.path.insert(0, ".")
from wxfusion import archive, config, db  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("rebuild_pair_stats")

UPSERT = """
insert into wx.pair_stats_daily
  (day, model, parameter, lead_bin, n, sum_err, sum_abs, sum_sq)
values (%s, %s, %s, %s, %s, %s, %s, %s)
on conflict (day, model, parameter, lead_bin) do update
  set n = excluded.n, sum_err = excluded.sum_err,
      sum_abs = excluded.sum_abs, sum_sq = excluded.sum_sq
  where excluded.n >= wx.pair_stats_daily.n
"""


def _day_totals(day: dt.date) -> list[tuple] | None:
    """(day, model, parameter, lead_bin, n, sum_err, sum_abs, sum_sq) or None."""
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    key = archive.pairs_key_for(day)
    try:
        obj = archive.r2_client().get_object(Bucket=config.R2_BUCKET, Key=key)
    except Exception:
        log.info("%s: no archive file", day)
        return None
    t = pq.read_table(io.BytesIO(obj["Body"].read()),
                     columns=["model", "parameter", "lead_bin", "error"])
    # Group in Arrow rather than Python: a day is a quarter of a million rows
    # and this runs over a month of them.
    t = t.append_column("abs_err", pc.abs(t.column("error")))
    t = t.append_column("sq_err", pc.multiply(t.column("error"), t.column("error")))
    g = t.group_by(["model", "parameter", "lead_bin"]).aggregate([
        ("error", "count"), ("error", "sum"), ("abs_err", "sum"), ("sq_err", "sum")])
    d = g.to_pydict()
    return [(day, d["model"][i], d["parameter"][i], d["lead_bin"][i],
             d["error_count"][i], d["error_sum"][i],
             d["abs_err_sum"][i], d["sq_err_sum"][i])
            for i in range(g.num_rows)]


def main() -> int:
    today = dt.date.today()
    start = os.environ.get("PAIR_STATS_FROM", "")
    end = os.environ.get("PAIR_STATS_TO", "")
    d0 = dt.date.fromisoformat(start) if start else today - dt.timedelta(days=30)
    d1 = dt.date.fromisoformat(end) if end else today - dt.timedelta(days=1)
    if d1 < d0:
        log.error("empty range: %s to %s", d0, d1)
        return 1

    conn = db.connect()
    days = restored = 0
    day = d0
    while day <= d1:
        rows = _day_totals(day)
        if rows:
            with conn.cursor() as cur:
                for r in rows:
                    cur.execute(UPSERT, r)
                    restored += cur.rowcount
            conn.commit()
            log.info("%s: %d combinations, %d comparisons",
                     day, len(rows), sum(r[4] for r in rows))
            days += 1
        day += dt.timedelta(days=1)

    log.info("%d days read, %d daily totals written or raised", days, restored)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
