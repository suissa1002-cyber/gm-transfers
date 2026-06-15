"""שירות worker נפרד לעבודות הרקע (Render Background Worker).

מטרה: להוציא את כל ה-jobs (poller, auto_transfer, pushes, sales/catalog sync,
backfill, reconcile, חשבוניות...) מתהליך ה-web — כך שעיבוד הרקע לא גוזל CPU
מבקשות המשתמשים. ה-web רץ עם RUN_JOBS=0, ה-worker הזה מריץ את ה-scheduler.

הפעלה ב-Render: Background Worker, Start Command: `python worker.py`,
אותם משתני סביבה כמו ה-web (DATABASE_URL וכו'), מומלץ DB_POOL_MAX=4.

⚠️ רק תהליך אחד אמור להריץ את ה-jobs החוזרים — לכן ה-web ב-RUN_JOBS=0
וה-worker הוא היחיד שקורא register_recurring_jobs().
"""
import logging
import threading

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("worker")

import main   # מייבא את האפליקציה + פונקציות ה-jobs + ה-scheduler (לא מריץ uvicorn)


def run():
    main.db.init_db()
    log.info("worker: DB ready (%s)", "Postgres" if main.cfg.DATABASE_URL else "SQLite")
    if not main.scheduler.running:
        main.scheduler.start()
    main.register_recurring_jobs()
    log.info("worker: recurring jobs registered — running")
    try:
        main.poller.poll_once()   # סבב ראשוני
    except Exception as e:  # noqa: BLE001
        log.warning("worker: initial poll failed: %s", e)
    # שומרים את התהליך חי (ה-scheduler רץ ב-threads ברקע)
    threading.Event().wait()


if __name__ == "__main__":
    run()
