"""Re-render the radar composites from the stored originals.

Reads only R2 — no meteolapa traffic at all — so this is the job to run after
changing the colour ramp, the beam removal, the fade, or the classifier.
RADAR_RENDER_FORCE=1 rewrites hours that already have a frame, which is what
you want when the change is to how they look rather than to which hours exist.
"""
import logging, os, sys
sys.path.insert(0, ".")
from wxfusion import radar_store  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
HOURS = int(os.environ.get("RADAR_RENDER_HOURS", "336"))
FORCE = os.environ.get("RADAR_RENDER_FORCE", "") == "1"


def main() -> int:
    n = radar_store.render_stored(hours_back=HOURS, force=FORCE)
    p = radar_store.prune()
    print(f"{n} composites rendered; pruned {p['sources']} originals, "
          f"{p['frames']} old frames")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
