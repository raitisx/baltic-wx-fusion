"""Nightly pairing job."""
import logging
import sys

sys.path.insert(0, ".")
from wxfusion import db, pairing  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> int:
    conn = db.connect()
    pairing.run(conn, days=3)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
