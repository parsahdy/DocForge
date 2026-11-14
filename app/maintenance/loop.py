"""Maintenance process.

    python -m app.maintenance.loop                      # reconcile + janitor, forever
    python -m app.maintenance.loop --once               # one cycle, then exit
    python -m app.maintenance.loop --audit              # list orphan objects (dry run), then exit
    python -m app.maintenance.loop --audit --delete-orphans
"""

from __future__ import annotations

import argparse
import logging
import time

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.maintenance.janitor import apply_retention, expire_pending_uploads, find_orphan_objects
from app.maintenance.reconciler import reconcile_once
from app.services.queue import publish_job
from app.services.storage import Storage

logger = logging.getLogger("docforge.maintenance")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument("--audit", action="store_true", help="report orphan objects (slow) and exit")
    parser.add_argument("--delete-orphans", action="store_true", help="with --audit: delete what is found")
    args = parser.parse_args()

    setup_logging()
    settings = get_settings()
    storage = Storage(settings)

    if args.audit:
        orphans = find_orphan_objects(settings, storage, delete_found=args.delete_orphans)
        logger.info("orphan audit", extra={"fields": {"orphans": len(orphans), "deleted": args.delete_orphans, "keys": orphans[:20]}})
        return

    while True:
        try:
            stats = reconcile_once(settings, storage, publish_job)
            stats["expired_uploads"] = expire_pending_uploads(settings, storage)
            stats["originals_deleted"] = apply_retention(settings, storage)
            logger.info("maintenance cycle", extra={"fields": stats})
        except Exception:  # noqa: BLE001 - the maintenance process must outlive any single failure
            logger.exception("maintenance cycle failed")
        if args.once:
            return
        time.sleep(settings.reconcile_interval_s)


if __name__ == "__main__":
    main()