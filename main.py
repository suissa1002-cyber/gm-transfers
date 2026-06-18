"""
Transfers app — FastAPI backend.
מגיש את ה-SPA, חושף API לקליטה/לוח-בהעברה, ומריץ poller + התראות ברקע (APScheduler).
"""

import os
import json as json_mod
import logging
from typing import Optional
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Header, Request, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from apscheduler.schedulers.background import BackgroundScheduler

import config as cfg
import db
import poller
import misroute

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("transfers.main")

app = FastAPI(title=cfg.APP_TITLE)
# דחיסת תשובות גדולות (קטלוג החיפוש של מלאי-חי ~5,600 מוצרים)
from fastapi.middleware.gzip import GZipMiddleware  # noqa: E402
app.add_middleware(GZipMiddleware, minimum_size=1024)
_here = os.path.dirname(__file__)
_static_dir = os.path.join(_here, "static")

scheduler = BackgroundScheduler(timezone=cfg.TZ)


# ──────────────────────────────────────────────────────────────
# רקע: poller + התראות
# ──────────────────────────────────────────────────────────────
def _poll_job():
    result = poller.poll_once()
    if result.get("new"):
        try:
            import alerts
            alerts.on_new_transfers(result["new"])
        except Exception as e:  # noqa: BLE001
            logger.warning("alerts.on_new_transfers failed: %s", e)


def _alerts_job():
    try:
        import alerts
        alerts.check_aging()
    except Exception as e:  # noqa: BLE001
        logger.warning("alerts.check_aging failed: %s", e)


def _digest_job():
    try:
        import alerts
        alerts.daily_digest()
    except Exception as e:  # noqa: BLE001
        logger.warning("alerts.daily_digest failed: %s", e)


def _serial_sync_job():
    try:
        import serial_sync
        serial_sync.full_sync()
    except Exception as e:  # noqa: BLE001
        logger.warning("serial_sync failed: %s", e)


def _rebalance_job():
    try:
        import rebalance_scan
        rebalance_scan.scan()
    except Exception as e:  # noqa: BLE001
        logger.warning("rebalance_scan failed: %s", e)


def _sales_ingest_job():
    try:
        import sales_ingest
        sales_ingest.ingest_incremental()
    except Exception as e:  # noqa: BLE001
        logger.warning("sales_ingest failed: %s", e)
    _sold_reconcile_job()   # אחרי כל קליטת מכירות — מזהה מכשירים שנמכרו לפני קליטה


def _sold_reconcile_job():
    """סוגר אוטומטית כרטיסי קליטה שהמכשיר שלהם נמכר לפני קליטה, ומתריע בטלגרם.
    case1: נמכר בסניף היעד · case2: נמכר בסניף שונה מהיעד — בשניהם מנקה מהדשבורד."""
    try:
        rows = db.reconcile_sold_transfer_items()
        for r in rows:
            to_name = cfg.branch_name(r.get("to_branch_id"))
            sold_name = cfg.branch_name(r.get("sold_branch_id"))
            if r.get("same_branch"):
                head = "📦✅ <b>מכשיר נמכר לפני קליטה</b>"
                where = f"נמכר בסניף היעד (<b>{to_name}</b>) לפני שבוצעה קליטה ב-GreenOS."
            else:
                head = "📦↪️ <b>מכשיר נמכר בסניף שונה מהיעד</b>"
                where = f"היעד היה <b>{to_name}</b>, אך נמכר ב<b>{sold_name}</b>."
            cleared = ("כרטיס הקליטה נסגר ונוקה מהדשבורד."
                       if r.get("transfer_closed") else "הפריט סומן כנמכר ונוקה מהכרטיס.")
            _tg_admin(f"{head}\n{r.get('name')} (סריאלי <code>{r.get('serial')}</code>)\n"
                      f"{where}\n{cleared}")
            logger.info("sold-reconcile: serial=%s op=%s same=%s closed=%s",
                        r.get("serial"), r.get("op_id"), r.get("same_branch"),
                        r.get("transfer_closed"))
        if rows:
            logger.info("sold-reconcile: closed %d stuck receive item(s)", len(rows))
    except Exception as e:  # noqa: BLE001
        logger.warning("sold reconcile job error: %s", e)


def _sales_backfill_job(days: int = 90, max_new_docs: int = 1500):
    try:
        import sales_ingest
        sales_ingest.backfill(days=days, max_new_docs=max_new_docs)
    except Exception as e:  # noqa: BLE001
        logger.warning("sales_backfill failed: %s", e)


def _catalog_refresh_job():
    try:
        import order_recommend
        order_recommend.refresh_catalog_to_db()
    except Exception as e:  # noqa: BLE001
        logger.warning("catalog_refresh failed: %s", e)


def _removals_ingest_job():
    try:
        import removals_ingest
        removals_ingest.ingest_incremental()
    except Exception as e:  # noqa: BLE001
        logger.warning("removals_ingest failed: %s", e)


def _auto_transfer_job():
    try:
        import auto_transfer
        auto_transfer.scan_orders()
    except Exception as e:  # noqa: BLE001
        logger.warning("auto_transfer failed: %s", e)


def _media_backup_job():
    """מגבה מדיה נכנסת ממטא (תמונות/מסמכים) לפני שמטא מוחק (~30 יום). מוריד עד 30
    פריטים שטרם גובו בכל ריצה (rate-limit ידידותי)."""
    try:
        import wa
        pend = db.wa_media_pending(limit=30)
        if not pend:
            return
        ok = 0
        for it in pend:
            if wa.backup_media(it["wamid"], it["media_id"]):
                ok += 1
        if ok:
            logger.info("media backup: saved %d/%d", ok, len(pend))
    except Exception as e:  # noqa: BLE001
        logger.warning("media backup failed: %s", e)


def _cargo_shipping_advance_job():
    """כלל פשוט: הודפסה תווית Cargo (קיים `cslfw_shipping`) → ההזמנה ל'בהפצה'.
    תופס תוויות מכל מקור (תוסף Cargo / WP / GreenOS). כולל 'הושלם' — אצל
    Green Mobile זה מצב מוקדם (NewOrder קובע אותו בהנפקת חשבונית), לא סופי.
    מקדם **פעם אחת** לכל הזמנה (דגל cargo_adv) — לא נלחם בשינויים ידניים אחר כך.
    _advance_to_shipping ממילא לא נוגע בנמסר/מוכנה-לאיסוף/בוטל/זוכה (סטטוס סופי)."""
    try:
        creds = _wc_creds()
        if not creds:
            return
        base, k, s = creds
        import requests as _rq
        r = _rq.get(f"{base}/wp-json/wc/v3/orders",
                    params={"per_page": 50, "orderby": "date", "order": "desc",
                            "status": "processing,on-hold,send-cargo,order-processing,completed"},
                    auth=(k, s), timeout=40)
        if not r.ok:
            return
        for o in r.json():
            oid = o.get("id")
            meta = {m.get("key"): m.get("value") for m in (o.get("meta_data") or [])}
            if not meta.get("cslfw_shipping"):
                continue                       # אין תווית/משלוח Cargo — לא נוגעים
            if db.sales_state_get(f"cargo_adv:{oid}"):
                continue                       # כבר קודם פעם אחת — לא חוזרים
            ns = _advance_to_shipping(oid)
            db.sales_state_set(f"cargo_adv:{oid}", "1")
            if ns == "shipping-stage":
                logger.info("cargo auto-advance -> shipping-stage: order %s", o.get("number"))
    except Exception as e:  # noqa: BLE001
        logger.warning("cargo shipping-advance failed: %s", e)


def _removals_backfill_job(days: int = 90):
    try:
        import removals_ingest
        removals_ingest.backfill(days=days)
    except Exception as e:  # noqa: BLE001
        logger.warning("removals_backfill failed: %s", e)


def _token_watch_job():
    try:
        import token_watch
        token_watch.check()
    except Exception as e:  # noqa: BLE001
        logger.warning("token_watch failed: %s", e)


def _wa_push_job():
    try:
        import wa_push
        wa_push.poll_and_push()
    except Exception as e:  # noqa: BLE001
        logger.warning("wa_push failed: %s", e)


def _orders_push_job():
    try:
        import wa_push
        wa_push.poll_and_push_orders()
    except Exception as e:  # noqa: BLE001
        logger.warning("orders_push failed: %s", e)


def _is_stale(iso_ts, hours: float) -> bool:
    """האם חותמת זמן ISO ישנה מ-X שעות (או חסרה/לא תקינה)."""
    if not iso_ts:
        return True
    try:
        t = datetime.fromisoformat(str(iso_ts))
        now = datetime.now(t.tzinfo) if t.tzinfo else datetime.now()
        return (now - t).total_seconds() > hours * 3600
    except Exception:  # noqa: BLE001
        return True


# RUN_JOBS=0 → מצב "web בלבד": לא מריצים את עבודות הרקע החוזרות (הן רצות בשירות
# worker נפרד, כדי שעיבוד הרקע לא יגזול CPU מבקשות המשתמשים). ברירת מחדל=מופעל
# (תאימות לאחור לשירות יחיד). השרת עדיין מפעיל את ה-scheduler לטריגרים חד-פעמיים.
_RUN_JOBS = os.getenv("RUN_JOBS", "1").strip() not in ("0", "false", "no")


def _keepalive_job():
    """פינג לשירותי Render חינמיים כדי שלא יירדמו (נרדמים אחרי 15 דק' חוסר פעילות).
    URLs ב-KEEPALIVE_URLS (מופרד בפסיק); ברירת מחדל = invoice-manager."""
    import requests as _rq
    urls = [u.strip() for u in os.getenv(
        "KEEPALIVE_URLS", "https://invoice-manager-tfqj.onrender.com").split(",") if u.strip()]
    for u in urls:
        try:
            _rq.get(u, timeout=20)
        except Exception as e:  # noqa: BLE001
            logger.warning("keepalive ping failed %s: %s", u, e)


def register_recurring_jobs():
    """רושם את כל עבודות הרקע החוזרות. נקרא מ-_startup (אם _RUN_JOBS) או מ-worker.py."""
    # סבב ראשון מיד, ואז לפי האינטרוול
    scheduler.add_job(_poll_job, "interval", seconds=cfg.POLL_INTERVAL_SEC,
                      id="poll", next_run_time=None, max_instances=1)
    scheduler.add_job(_alerts_job, "interval", minutes=15, id="alerts", max_instances=1)
    # דוח יומי 09:00 (Sun-Thu) למנהלים
    scheduler.add_job(_digest_job, "cron", id="digest",
                      hour=cfg.DIGEST_HOUR, minute=0, day_of_week=cfg.DIGEST_DAYS,
                      max_instances=1)
    # אינדקס סריאל→מוצר: סבב baseline כל 3 שעות. ריצה ראשונית רק אם האינדקס ישן —
    # הוא נשמר ב-DB, ואין סיבה לשרוף ~555 קריאות אחרי כל deploy/restart
    # (זה גם מה שגרם ל"שגיאת קופה" בטאב מלאי חי: הסנכרון הרווה את מגבלת הקצב).
    scheduler.add_job(_serial_sync_job, "interval", hours=3, id="serial_sync", max_instances=1)
    if _is_stale(db.serial_index_last_sync(), hours=3):
        scheduler.add_job(_serial_sync_job, "date", id="serial_sync_initial",
                          run_date=datetime.now() + timedelta(seconds=60))
    else:
        logger.info("serial index fresh — skipping initial full sync")
    # איזון מלאי: פעמיים ביום (כך שתמיד נופל בתוך שעות הפעילות של איזה יום) — תחילת/סוף יום
    scheduler.add_job(_rebalance_job, "cron", id="rebalance_am", hour=8, minute=30, max_instances=1)
    scheduler.add_job(_rebalance_job, "cron", id="rebalance_pm", hour=21, minute=0, max_instances=1)
    # איסוף מכירות מצטבר: כל 3 שעות. ריצה ראשונית רק אם האיסוף האחרון ישן —
    # deploys תכופים לא צריכים להפעיל אותו שוב ושוב (מעמיס על הקצב המשותף).
    scheduler.add_job(_sales_ingest_job, "interval", hours=3, id="sales_ingest", max_instances=1)
    # סגירת כרטיסי קליטה למכשירים שנמכרו לפני קליטה — גם עצמאית (גיבוי ל-hook באיסוף)
    scheduler.add_job(_sold_reconcile_job, "interval", minutes=30, id="sold_reconcile", max_instances=1)
    if _is_stale(db.sales_state_get("last_run"), hours=2):
        scheduler.add_job(_sales_ingest_job, "date", id="sales_ingest_initial",
                          run_date=datetime.now() + timedelta(seconds=120))
    else:
        logger.info("sales ingest fresh — skipping initial run")
    # הורדות מלאי מרלוג: כל 3 שעות. ריצה ראשונית רק אם ישן (אותו היגיון).
    scheduler.add_job(_removals_ingest_job, "interval", hours=3, id="removals_ingest", max_instances=1)
    if _is_stale(db.sales_state_get("removals_last_run"), hours=2):
        scheduler.add_job(_removals_ingest_job, "date", id="removals_initial",
                          run_date=datetime.now() + timedelta(seconds=180))
    else:
        logger.info("removals ingest fresh — skipping initial run")
    # קטלוג מוצרים ל-DB: רענון כל 6 שעות. ריצה ראשונית רק אם הקטלוג ישן (נשמר ב-DB).
    scheduler.add_job(_catalog_refresh_job, "interval", hours=6, id="catalog_refresh", max_instances=1)
    # גיבוי מדיה נכנסת ממטא (תמונות/מסמכים) — כל 5 דק', כדי שלא יאבד כשמטא ימחק (~30 יום)
    scheduler.add_job(_media_backup_job, "interval", minutes=5, id="media_backup", max_instances=1)
    scheduler.add_job(_media_backup_job, "date", id="media_backup_initial",
                      run_date=datetime.now() + timedelta(seconds=45))
    # שידור אוטומטי של בקשות העברה לאתר על הזמנות אתר ששולמו
    scheduler.add_job(_auto_transfer_job, "interval", minutes=5, id="auto_transfer", max_instances=1)
    # קידום ל'בהפצה' להזמנות עם משלוח Cargo (גם תוויות שהודפסו מחוץ ל-GreenOS)
    scheduler.add_job(_cargo_shipping_advance_job, "interval", minutes=3,
                      id="cargo_shipping_advance", max_instances=1, coalesce=True)
    # מסירת Cargo (נמסר) → 'נמסרה' (+חוו"ד) → +24ש 'הושלם'; ישנות → ישר 'הושלם'
    scheduler.add_job(_cargo_delivery_sync_job, "interval", minutes=30,
                      id="cargo_delivery", max_instances=1, coalesce=True)
    # עדכוני סטטוס אוטומטיים ללקוח (בהפצה/מוכן לאיסוף/נק' מסירה) — מחליף קונקטופ
    scheduler.add_job(_status_notify_job, "interval", minutes=3,
                      id="status_notify", max_instances=1, coalesce=True)
    # catch-up בהפעלה אם הקטלוג ישן מ-2ש' — כך דפלויים תכופים לא מרעיבים את רענון
    # ה-6ש' ומשאירים מוצרים שחוברו לאחרונה מחוץ לקטלוג (חוסם שידור הזמנות).
    if _is_stale(db.catalog_meta().get("updated_at"), hours=2):
        scheduler.add_job(_catalog_refresh_job, "date", id="catalog_initial",
                          run_date=datetime.now() + timedelta(seconds=150))
    else:
        logger.info("catalog fresh — skipping initial refresh")
    # Web Push וואטסאפ: זיהוי הודעות נכנסות כל 60ש (מדלג כשאין מכשירים רשומים)
    scheduler.add_job(_wa_push_job, "interval", seconds=60, id="wa_push", max_instances=1)
    # Web Push הזמנות חדשות: 🎉 push על הזמנת אתר חדשה כל 60ש (גם כשהאפליקציה סגורה)
    scheduler.add_job(_orders_push_job, "interval", seconds=60, id="orders_push", max_instances=1)
    # שליחה מתוזמנת ("שלח בשעה X") — בדיקה כל 30ש, שולח מה שהגיע זמנו
    scheduler.add_job(_wa_scheduled_job, "interval", seconds=30, id="wa_scheduled", max_instances=1)
    # שינוי סטטוס הזמנה מתוזמן — בדיקה כל 30ש, מחיל מה שהגיע זמנו
    scheduler.add_job(_scheduled_status_job, "interval", seconds=30, id="scheduled_status", max_instances=1)
    # קליטת חשבוניות ממייל הקופה — כל 3 שעות (לא דחוף; חוסך עומס IMAP/PDF)
    scheduler.add_job(_invoice_capture_job, "interval", hours=3, id="invoice_capture", max_instances=1)
    # catch-up בהפעלה: דפלוי תכוף מאפס את טיימר ה-3ש' → היום אף קליטה לא רצה. ריצה
    # קצרה אחרי כל הפעלה (אידמפוטנטית — רק חדשות) מבטיחה שלא נפספס יום שלם.
    scheduler.add_job(_invoice_capture_job, "date", id="invoice_capture_initial",
                      run_date=datetime.now() + timedelta(seconds=90))
    # backfill היסטוריית וואטסאפ — רץ רק בשעות שקטות (21:00-09:00 IL); resumable.
    # פייר בכל שעה בחלון; max_instances=1 → ריצה ארוכה אחת ללילה + התאוששות מ-restart.
    scheduler.add_job(_wa_backfill_job, "cron", hour="21-23,0-8", minute=10,
                      id="wa_backfill_nightly", max_instances=1, coalesce=True)
    # ניטור טוקן ConnectOp: בדיקה יומית 09:30 + בדיקת boot (לוג בלבד כשהכל תקין)
    scheduler.add_job(_token_watch_job, "cron", id="token_watch",
                      hour=9, minute=30, max_instances=1)
    scheduler.add_job(_token_watch_job, "date", id="token_watch_initial",
                      run_date=datetime.now() + timedelta(seconds=45))
    # keep-alive: שומר שירותי Render חינמיים ערים (נרדמים אחרי 15 דק' חוסר פעילות).
    # invoice-manager (Itzik) מריץ פולינג Gmail פנימי שמפסיק בשינה — הפינג מחליף את
    # ה-cron של GitHub Actions (שנחסם עם מכסת הדקות).
    if os.getenv("KEEPALIVE_URLS", "https://invoice-manager-tfqj.onrender.com").strip():
        scheduler.add_job(_keepalive_job, "interval", minutes=10, id="keepalive", max_instances=1)


@app.on_event("startup")
def _startup():
    db.init_db()
    logger.info("DB ready (%s)", "Postgres" if cfg.DATABASE_URL else "SQLite")
    # פיתוח מקומי: DISABLE_BACKGROUND_JOBS=1 מכבה את כל עבודות הרקע.
    if os.getenv("DISABLE_BACKGROUND_JOBS", "").strip() in ("1", "true", "yes"):
        logger.warning("background jobs DISABLED (DISABLE_BACKGROUND_JOBS)")
        return
    scheduler.start()   # תמיד — גם במצב web (לטריגרים חד-פעמיים מ-endpoints, כמו תשלום)
    if not _RUN_JOBS:
        logger.info("WEB mode — recurring jobs run in the separate worker service")
        return
    register_recurring_jobs()
    try:
        poller.poll_once()   # סבב ראשוני קצר כדי שהלוח לא יהיה ריק בהפעלה
    except Exception as e:  # noqa: BLE001
        logger.warning("initial poll failed: %s", e)


@app.on_event("shutdown")
def _shutdown():
    if scheduler.running:
        scheduler.shutdown(wait=False)


# ──────────────────────────────────────────────────────────────
# API
# ──────────────────────────────────────────────────────────────
def _require_admin(x_admin_key: Optional[str] = Header(None)):
    """הגנת קונסולת הניהול. אם ADMIN_PASSWORD ריק — פתוח (פיתוח)."""
    if not cfg.ADMIN_PASSWORD:
        return
    if (x_admin_key or "") != cfg.ADMIN_PASSWORD:
        raise HTTPException(401, "admin auth required")


def _actor_name(x_admin_key, x_device_token) -> str:
    """מי ביצע את הפעולה — לקונסולה או למכשיר סניף מאושר (לתיעוד בבקשות העברה)."""
    if cfg.ADMIN_PASSWORD and (x_admin_key or "") == cfg.ADMIN_PASSWORD:
        return "קונסולת ניהול"
    d = db.device_get(x_device_token or "") if x_device_token else None
    if d:
        nm = (d.get("name") or "").strip()
        bh = (d.get("branch_hint") or "").strip()
        return f"{nm} ({bh})" if nm and bh else (nm or bh or "מכשיר מאושר")
    return ""


def _require_admin_or_device(x_admin_key, x_device_token):
    """גישה למנהל (סיסמה) או למכשיר סניף מאושר — לפיצ'רים שפתוחים לסניפים
    (מלאי חי, בקשת משיכה). הניהול המלא נשאר בסיסמה בלבד."""
    if not cfg.ADMIN_PASSWORD or (x_admin_key or "") == cfg.ADMIN_PASSWORD:
        return
    d = db.device_get(x_device_token or "") if x_device_token else None
    if d and d.get("status") == "approved":
        return
    raise HTTPException(401, "admin or approved device required")


def _caller_device(x_admin_key, x_device_token):
    """כמו _require_admin_or_device, אבל מחזיר זהות: None=מנהל, dict=מכשיר סניפי."""
    if not cfg.ADMIN_PASSWORD or (x_admin_key or "") == cfg.ADMIN_PASSWORD:
        return None
    d = db.device_get(x_device_token or "") if x_device_token else None
    if d and d.get("status") == "approved":
        return d
    raise HTTPException(401, "admin or approved device required")


def _device_branch(d) -> str:
    """הסניף הנעול של מכשיר. מכשיר ותיק בלי נעילה — מאמץ חד-פעמית את הסניף שנרשם."""
    locked = (d.get("branch_locked") or "").strip()
    if not locked:
        locked = (d.get("branch_hint") or "").strip()
        if locked:
            db.device_set_locked(d["token"], locked)
    return locked


@app.get("/health")
def health():
    return {"ok": True, "stats": db.stats()}


# ── WhatsApp Cloud API webhook (פרויקט ניתוק קונקטופ, שלב 1 — קבלה ישירה ממטא) ──
# רדום עד שמפנים את override_callback_uri אלינו ב-Meta. ציבורי (מטא קוראת לו);
# אבטחה דרך verify-token (GET) + חתימת HMAC (POST).
@app.get("/api/wa/webhook")
def wa_webhook_verify(request: Request):
    p = request.query_params
    expect = os.getenv("META_WEBHOOK_VERIFY_TOKEN", "").strip()
    if p.get("hub.mode") == "subscribe" and expect and p.get("hub.verify_token") == expect:
        return PlainTextResponse(p.get("hub.challenge") or "")
    raise HTTPException(403, "verify failed")


_HANDLED_WAMIDS = set()   # מניעת כפילות: מטא שולחת webhook שוב כשהתגובה איטית


def _typing_keepalive(wamid, stop_evt, max_seconds=150):
    """שומר על חיווי ההקלדה *חי* לכל אורך עיבוד הבוט. חיווי מטא פג אחרי ~25 שניות,
    אז כשהבוט עובד זמן רב (מנוע חיפוש → אורי → API) הלקוח רואה 'דממה'. כאן משדרים
    את החיווי מחדש כל 20 שניות עד שהבוט מסיים (stop_evt) או עד תקרה — כך הלקוח תמיד
    רואה שמשהו קורה בצד השני, בכל סיטואציה."""
    import wa
    waited = 0
    while waited < max_seconds:
        try:
            wa.send_typing(wamid)
        except Exception:  # noqa: BLE001
            pass
        if stop_evt.wait(8):       # רענון כל 8ש (החיווי פג אחרי ~25ש) — בלי פערים
            return
        waited += 8


def _dispatch_bot(bot_sender, inb):
    """ריצת הבוט ברקע — כדי שה-webhook יחזיר 200 מיד (מונע retry של מטא וכפילות)."""
    import threading
    wamid = inb[4] if len(inb) > 4 else ""
    stop_evt = threading.Event()
    if wamid:                      # חיווי הקלדה רציף לכל אורך העיבוד (לא רק 25ש)
        threading.Thread(target=_typing_keepalive, args=(wamid, stop_evt),
                         daemon=True).start()
    try:
        import wa_bot
        wa_bot.handle(bot_sender, inb[1], inb[2] or "text",
                      reply_id=(inb[3] if len(inb) > 3 else ""),
                      wamid=wamid)
    except Exception as e:  # noqa: BLE001
        logger.warning("wa bot dispatch failed: %s", e)
    finally:
        stop_evt.set()             # עוצר את חיווי ההקלדה ברגע שיש תשובה


@app.post("/api/wa/webhook")
async def wa_webhook_recv(request: Request, background: BackgroundTasks):
    raw = await request.body()
    import wa_webhook
    sig = request.headers.get("x-hub-signature-256", "")
    # שלב 6 (גלגול בטוח): אם השולח ב-BOT_WHITELIST → הבוט ה-native מטפל בו, ולכן
    # *לא* מעבירים לקונקטופ (אחרת הלקוח יקבל שתי תשובות). כל שאר הלקוחות —
    # מועברים לקונקטופ כרגיל. כל פעולה כאן עטופה ב-try; כשל → ברירת מחדל בטוחה
    # (מעבירים לקונקטופ), כך שלקוחות אמיתיים לעולם לא מושפעים.
    bot_sender = None
    try:
        import wa_bot
        snd = wa_webhook.extract_sender(raw)
        if snd and wa_bot.enabled_for(snd):
            bot_sender = snd
    except Exception:  # noqa: BLE001
        bot_sender = None
    if not bot_sender:
        wa_webhook.forward_to_connectop(raw, sig)
    # העיבוד והשמירה שלנו — רק על חתימה תקפה (מגן על החנות שלנו, לא על קונקטופ).
    if wa_webhook.verify_signature(raw, sig):
        try:
            wa_webhook.process(raw)
        except Exception as e:  # noqa: BLE001
            logger.warning("wa webhook process failed: %s", e)
        if bot_sender:                       # הבוט ה-native — רק לשולח whitelisted
            try:
                inb = wa_webhook.extract_inbound(raw)
                if inb and inb[1] is not None:
                    wamid = inb[4] if len(inb) > 4 else ""
                    if wamid and wamid in _HANDLED_WAMIDS:
                        pass                     # אותה הודעה כבר טופלה (retry) — דילוג
                    else:
                        if wamid:
                            _HANDLED_WAMIDS.add(wamid)
                            if len(_HANDLED_WAMIDS) > 800:
                                _HANDLED_WAMIDS.clear()
                        # ריצה ברקע → 200 מיידי, בלי retry מצד מטא
                        background.add_task(_dispatch_bot, bot_sender, inb)
            except Exception as e:  # noqa: BLE001
                logger.warning("wa bot dispatch failed: %s", e)
    else:
        logger.warning("wa webhook bad signature — הועבר לקונקטופ, דילגנו על שמירה מקומית")
    return {"ok": True}


@app.get("/api/wa/media")
def wa_media(wamid: str, t: Optional[str] = None, x_admin_key: Optional[str] = Header(None)):
    """מדיה: היסטורי → redirect ל-CDN של ChatRace; חי (מטא) → גיבוי שלנו או הורדה
    ממטא (ומגבה תוך כדי). אימות: כותרת ניהול **או** טוקן חתום ב-?t= (כדי ש-<img>
    יוכל לטעון בלי כותרת)."""
    import wa
    if not (t and wa.media_token(wamid) == t):
        _require_admin(x_admin_key)
    m = db.wa_msg_get(wamid)
    if not m:
        raise HTTPException(404, "הודעה לא נמצאה")
    if m.get("media_url"):
        from fastapi.responses import RedirectResponse
        return RedirectResponse(m["media_url"])
    try:
        content, mime = wa.serve_media(wamid)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e)[:200])
    from fastapi.responses import Response
    return Response(content, media_type=mime,
                    headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/admin/wa/store-stats")
def wa_store_stats(x_admin_key: Optional[str] = Header(None)):
    """כמה הודעות כבר נאספו בחנות העצמאית — לניטור שלב הקבלה."""
    _require_admin(x_admin_key)
    import wa_backfill
    return {"messages": db.wa_msg_count(), "backfill": wa_backfill.progress()}


def _wa_backfill_job():
    try:
        import wa_backfill
        wa_backfill.run()
    except Exception as e:  # noqa: BLE001
        logger.warning("wa_backfill failed: %s", e)


@app.post("/api/admin/wa/backfill")
def wa_backfill_start(x_admin_key: Optional[str] = Header(None)):
    """מפעיל ידנית שאיבת היסטוריה (resumable; עוצר לבד בשעות מענה 09-21 IL)."""
    _require_admin(x_admin_key)
    db.sales_state_set("wa_backfill_stop", "0")
    scheduler.add_job(_wa_backfill_job, "date", id="wa_backfill_manual", max_instances=1,
                      replace_existing=True)
    return {"started": True}


@app.post("/api/admin/wa/backfill/stop")
def wa_backfill_stop(x_admin_key: Optional[str] = Header(None)):
    """עצירה ידנית — הריצה תיעצר בשיחה הבאה (resumable; ממשיכה בלילה הבא)."""
    _require_admin(x_admin_key)
    db.sales_state_set("wa_backfill_stop", "1")
    return {"stopping": True}


# ── קריאה עצמאית (שלב 3) — endpoints בדיקה מול החנות שלנו, בלי לגעת בקריאה החיה ──
@app.get("/api/admin/wa/conversations-native")
def wa_conversations_native(x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    import wa
    return {"conversations": wa.list_conversations_native()}


@app.get("/api/admin/wa/thread-native/{phone}")
def wa_thread_native(phone: str, limit: int = 80, x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    import wa
    return wa.get_thread_native(phone, limit=limit)


@app.get("/api/config")
def app_config():
    """דגלים ל-frontend: האם הניהול דורש סיסמה."""
    return {"admin_required": bool(cfg.ADMIN_PASSWORD),
            "admin_branch_id": cfg.ADMIN_BRANCH_ID}


class AdminLogin(BaseModel):
    password: str


@app.post("/api/admin/login")
def admin_login(body: AdminLogin):
    if not cfg.ADMIN_PASSWORD or body.password == cfg.ADMIN_PASSWORD:
        return {"ok": True}
    raise HTTPException(401, "wrong password")


@app.get("/api/branches")
def branches():
    return [{"id": bid, "name": name} for bid, name in cfg.BRANCHES.items()]


def _enrich(t: dict) -> dict:
    """מוסיף שמות סניף קריאים להעברה (לשימוש כל ה-endpoints)."""
    if t:
        t["from_branch_name"] = cfg.branch_name(t.get("from_branch_id"))
        t["to_branch_name"] = cfg.branch_name(t.get("to_branch_id"))
    return t


@app.get("/api/transfers")
def transfers(branch_id: int):
    # list_in_transit כבר מחזיר את שדות הכותרת (כולל received_units/total_units/status)
    # שהכרטיסים בלוח צריכים — אין צורך ב-get_transfer פר-שורה (שמושך גם items).
    return [_enrich(t) for t in db.list_in_transit(branch_id)]


@app.get("/api/transfers/{op_id}")
def transfer_detail(op_id: str):
    t = db.get_transfer(op_id)
    if not t:
        raise HTTPException(404, "transfer not found")
    return _enrich(t)


class CloseIn(BaseModel):
    reason: Optional[str] = None
    by: Optional[str] = None


@app.post("/api/transfers/{op_id}/close")
def close_transfer(op_id: str, body: CloseIn):
    """סגירת קליטה ידנית — פריטים שלא נסרקו נרשמים כחוסר, ההעברה יוצאת מהלוח."""
    t = db.get_transfer(op_id)
    if not t:
        raise HTTPException(404, "transfer not found")
    return _enrich(db.close_transfer(op_id, body.reason or "", body.by or ""))


class ScanIn(BaseModel):
    branch_id: int
    code: str
    op_id: Optional[str] = None
    employee: Optional[str] = None
    method: Optional[str] = None


@app.post("/api/scan")
def scan(body: ScanIn):
    res = db.receive_scan(body.branch_id, body.code, body.op_id, body.employee, body.method)
    if res.get("matched"):
        # נקלט בסניף הנכון → סוגר חריגת "לא במקום" אם הייתה פתוחה על הסריאל
        try: db.resolve_misroutes(body.code)
        except Exception as e: logger.warning("resolve_misroutes failed: %s", e)  # noqa: BLE001
    else:
        # לא תאם להעברה נכנסת → בדיקה חיה אם המכשיר שייך לסניף אחר
        try:
            mr = misroute.check(body.code, body.branch_id, body.employee or "")
            if mr: res["misroute"] = mr
        except Exception as e:  # noqa: BLE001
            logger.warning("misroute check failed: %s", e)
    res["transfer"] = _enrich(res.get("transfer"))
    return res


@app.get("/api/stats")
def stats():
    return db.stats()


# ── שידור בקשות העברה למסך הסניף ──
# טייל אחד לכל סניף יעד; בקשות חדשות לאותו יעד מצטרפות לטייל הקיים.
class BroadcastIn(BaseModel):
    branch_id: int
    line_ids: Optional[list[int]] = None     # שידור ממוקד (בקשה ממלאי חי)


@app.post("/api/admin/broadcast")
def admin_broadcast(body: BroadcastIn, x_admin_key: Optional[str] = Header(None), x_device_token: Optional[str] = Header(None)):
    """משדר בקשות העברה למסך הקליטה של סניף המקור (תצוגה בלבד)."""
    d = _caller_device(x_admin_key, x_device_token)
    if d is not None:
        # נעילת סניף: מכשיר משדר רק בקשות שמערבות את הסניף שלו (אל/מ).
        # בין שני סניפים אחרים — רק דרך זרימת האישור (plan-action).
        own = _device_branch(d)
        rows = {ln["id"]: ln for ln in db.plan_list()}
        for lid in (body.line_ids or []):
            ln = rows.get(int(lid))
            if not ln or (str(ln.get("to_branch")) != own and str(ln.get("from_branch")) != own):
                raise HTTPException(403, "העברה בין סניפים אחרים — דרך אישור מנהל")
        if not body.line_ids:
            raise HTTPException(403, "שידור כללי — דרך מנהל בלבד")
    n = db.plan_mark_broadcast(body.branch_id, body.line_ids)
    return {"ok": True, "lines": n}


@app.get("/api/admin/broadcasts")
def admin_broadcasts(x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    return {"branches": db.broadcast_branches()}


@app.get("/api/broadcast")
def get_broadcast(branch_id: int):
    """ציבורי — הבקשות המשודרות לסניף, מקובצות לפי סניף יעד (טייל לכל יעד)."""
    # מעבר גרסה: שידור ישן (טבלת broadcasts) → מסמן את שורות הסניף כ-bcast=1 ומנקה
    legacy = db.broadcast_get(branch_id)
    if legacy:
        db.plan_mark_broadcast(branch_id)
        db.broadcast_clear(branch_id)
    groups = db.broadcast_groups(branch_id)
    all_lines = []
    for g in groups:
        g["to_name"] = cfg.branch_name(g["to_branch"])
        for ln in g["lines"]:
            ln["from_name"] = cfg.branch_name(ln.get("from_branch"))
            ln["to_name"] = cfg.branch_name(ln.get("to_branch"))
        all_lines.extend(g["lines"])
    _enrich_color(all_lines)   # שורות הקבוצות הן אותם dict-ים — מועשרות יחד
    return {"active": bool(groups), "groups": groups,
            "broadcast_at": groups[0]["latest"] if groups else None,
            "from_name": cfg.branch_name(branch_id), "lines": all_lines}


class DismissIn(BaseModel):
    branch_id: int
    to_branch: Optional[int] = None


@app.post("/api/broadcast/dismiss")
def dismiss_broadcast(body: DismissIn):
    """ציבורי — הסניף סוגר טייל של יעד מסוים (או הכל). השורות נשארות בתוכנית."""
    db.broadcast_dismiss_group(body.branch_id, body.to_branch)
    db.broadcast_clear(body.branch_id)   # legacy
    return {"ok": True}


@app.get("/api/admin/overview")
def admin_overview(days: int = 7, x_admin_key: Optional[str] = Header(None)):
    """לוח ניהול אופרציה — כל ההעברות בכל הסניפים: מי לא סרק, מי ממתין, מה נקלט."""
    _require_admin(x_admin_key)
    import alerts  # שימוש חוזר ב-_age_hours
    rows = db.list_all_transfers(include_received_days=days)
    agg = db.transfers_overview_aggregates([t["op_id"] for t in rows])   # שאילתה אחת במקום 4×N
    out = []
    for t in rows:
        age = alerts._age_hours(t)
        t = _enrich(t)
        t["age_hours"] = round(age, 1)
        t["overdue"] = (t["status"] != "received"
                        and age >= cfg.RECEIVE_ESCALATE_HOURS)
        t["missing"] = (t.get("total_units", 0) or 0) - (t.get("received_units", 0) or 0)
        a = agg.get(str(t["op_id"]), {})
        t["receivers"] = a.get("receivers", [])
        t["manual_count"] = a.get("manual_count", 0)
        t["redirected_count"] = a.get("redirected", 0)
        t["missing_count"] = a.get("missing", 0)
        t["items_search"] = a.get("search_text", "")
        out.append(t)
    mis = db.list_open_misroutes()
    for m in mis:
        m["expected_branch_name"] = cfg.branch_name(m.get("expected_branch_id"))
        m["scanned_branch_name"] = cfg.branch_name(m.get("scanned_branch_id"))
    summary = {
        "in_transit": sum(1 for t in out if t["status"] == "in_transit"),
        "partial":    sum(1 for t in out if t["status"] == "partial"),
        "received":   sum(1 for t in out if t["status"] == "received"),
        "overdue":    sum(1 for t in out if t["overdue"]),
        "misroutes":  len(mis),
    }
    return {"summary": summary, "transfers": out, "misroutes": mis}


@app.post("/api/poll")
def manual_poll():
    return poller.poll_once()


@app.post("/api/admin/serial-sync")
def admin_serial_sync(x_admin_key: Optional[str] = Header(None)):
    """הפעלת סבב אינדוקס סריאלים ידנית (רץ ברקע)."""
    _require_admin(x_admin_key)
    scheduler.add_job(_serial_sync_job, "date", id="serial_sync_manual",
                      run_date=datetime.now() + timedelta(seconds=1), replace_existing=True)
    return {"started": True, "index_size": db.serial_index_count()}


@app.get("/api/admin/serial-index")
def admin_serial_index(x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    return {"index_size": db.serial_index_count()}


@app.get("/api/admin/rebalance")
def admin_rebalance(x_admin_key: Optional[str] = Header(None)):
    """רשימת המלצות איזון מלאי + זמן הסריקה האחרונה."""
    _require_admin(x_admin_key)
    items = db.rebalance_list()
    for it in items:
        it["needs_names"] = [cfg.branch_name(b) for b in (it.get("needs") or [])]
        it["surplus_names"] = [cfg.branch_name(b) for b in (it.get("surplus") or [])]
    return {"last_scan": db.rebalance_last_scan(), "items": items,
            "branches": [{"id": b, "name": cfg.branch_name(b)} for b in (1, 2, 3, 4)]}


class PlanLine(BaseModel):
    product_id: str
    name: Optional[str] = ""
    from_branch: int
    to_branch: int
    qty: int = 1
    serial: Optional[str] = ""    # בקשה ליחידה סריאלית ספציפית (התאמה אוטומטית מול העברות)


class PlanAdd(BaseModel):
    lines: list[PlanLine]


@app.get("/api/admin/plan")
def admin_plan(x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    lines = db.plan_list()
    for ln in lines:
        ln["from_name"] = cfg.branch_name(ln.get("from_branch"))
        ln["to_name"] = cfg.branch_name(ln.get("to_branch"))
    _enrich_color(lines)
    return {"lines": lines, "branches": [{"id": b, "name": cfg.branch_name(b)} for b in (1, 2, 3, 4)]}


@app.post("/api/admin/plan")
def admin_plan_add(body: PlanAdd, x_admin_key: Optional[str] = Header(None), x_device_token: Optional[str] = Header(None)):
    d = _caller_device(x_admin_key, x_device_token)
    if d is not None:
        # נעילת סניף: העברות שמערבות את הסניף של המכשיר — חופשי.
        # העברה בין שני סניפים *אחרים* — נשלחת לאישור מנהל בטלגרם ומשודרת עם האישור.
        own = _device_branch(d)
        if not own:
            raise HTTPException(403, "המכשיר לא משויך לסניף — פנה למנהל")
        foreign = [l for l in body.lines
                   if str(l.to_branch) != own and str(l.from_branch) != own]
        if foreign:
            import uuid
            rid = uuid.uuid4().hex[:12]
            db.sales_state_set(f"plnreq:{rid}", json_mod.dumps({
                "status": "pending", "device": d.get("name") or "",
                "lines": [l.model_dump() for l in body.lines],
                "at": datetime.now().isoformat(timespec="seconds")}))
            base = (cfg.APP_BASE_URL or "https://gm-transfers.onrender.com").rstrip("/")
            ok_url = f"{base}/plan-action?req={rid}&action=approve&sig={_sig(rid, 'plnreq:approve')}"
            no_url = f"{base}/plan-action?req={rid}&action=deny&sig={_sig(rid, 'plnreq:deny')}"
            desc = "\n".join(f"• {l.name or l.product_id} ×{l.qty} — "
                             f"{cfg.branch_name(l.from_branch)} ← {cfg.branch_name(l.to_branch)}"
                             for l in body.lines)
            _tg_admin(f"🔁 <b>בקשת העברה בין סניפים — דרוש אישור</b>\n"
                      f"🖥️ מבקש: <b>{d.get('name') or '?'}</b> (סניף {cfg.branch_name(own)})\n{desc}\nלאשר?",
                      buttons=[{"text": "✅ אשר ושדר", "url": ok_url},
                               {"text": "❌ דחה", "url": no_url}])
            return {"added": 0, "ids": [], "pending": True, "req": rid}
    ids = db.plan_add([l.model_dump() for l in body.lines],
                      created_by=_actor_name(x_admin_key, x_device_token))
    return {"added": len(ids), "ids": ids}


@app.get("/plan-action")
def plan_action(req: str, action: str, sig: str):
    """אישור/דחייה של בקשת העברה בין-סניפית מהטלגרם — באישור: נוספת ומשודרת מיד."""
    if action not in ("approve", "deny") or not _hmac.compare_digest(sig, _sig(req, f"plnreq:{action}")):
        raise HTTPException(403, "bad signature")
    raw = db.sales_state_get(f"plnreq:{req}")
    if not raw:
        raise HTTPException(404, "request not found")
    data = json_mod.loads(raw)
    if data.get("status") == "pending" and action == "approve":
        ids = db.plan_add(data["lines"], created_by=f"באישור מנהל · {data.get('device') or ''}")
        for fb in {int(l["from_branch"]) for l in data["lines"]}:
            db.plan_mark_broadcast(fb, [i for i, l in zip(ids, data["lines"])
                                        if int(l["from_branch"]) == fb])
    data["status"] = "approved" if action == "approve" else "denied"
    db.sales_state_set(f"plnreq:{req}", json_mod.dumps(data))
    msg = "✅ הבקשה אושרה ושודרה לסניף המקור" if action == "approve" else "❌ הבקשה נדחתה"
    from fastapi.responses import HTMLResponse
    return HTMLResponse(f"""<!doctype html><html dir="rtl"><head><meta charset="utf-8">
      <meta name="viewport" content="width=device-width,initial-scale=1"></head>
      <body style="font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:90vh;background:#f4f6f8">
      <div style="background:#fff;border-radius:16px;padding:34px 40px;box-shadow:0 8px 30px rgba(0,0,0,.12);text-align:center">
      <div style="font-size:44px">{'✅' if action=='approve' else '❌'}</div>
      <h2 style="margin:10px 0 4px">{msg}</h2></div></body></html>""")


class PlanReplace(BaseModel):
    product_id: str
    lines: list[PlanLine]


@app.post("/api/admin/plan/replace")
def admin_plan_replace(body: PlanReplace, x_admin_key: Optional[str] = Header(None)):
    """מחליף את שורות התוכנית למוצר (עריכה/הסרה). lines ריק = הסרת הבקשה."""
    _require_admin(x_admin_key)
    return {"count": db.plan_replace_product(body.product_id, [l.model_dump() for l in body.lines],
                                             created_by=_actor_name(x_admin_key, None))}


@app.delete("/api/admin/plan/{pid}")
def admin_plan_delete(pid: int, x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    return {"deleted": db.plan_delete(pid)}


@app.post("/api/admin/plan/clear")
def admin_plan_clear(from_branch: Optional[int] = None, x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    db.plan_clear(from_branch)
    return {"ok": True}


@app.post("/api/admin/rebalance-scan")
def admin_rebalance_scan(x_admin_key: Optional[str] = Header(None)):
    """הפעלת סריקת איזון מלאי ידנית (רצה ברקע, ~1-2 דק')."""
    _require_admin(x_admin_key)
    scheduler.add_job(_rebalance_job, "date", id="rebalance_manual",
                      run_date=datetime.now() + timedelta(seconds=1), replace_existing=True)
    return {"started": True, "last_scan": db.rebalance_last_scan()}


class RelabelIn(BaseModel):
    op_id: str
    name: str


@app.get("/api/admin/numeric-receivers")
def admin_numeric_receivers(x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    return db.numeric_receivers()


@app.post("/api/admin/relabel-receiver")
def admin_relabel_receiver(body: RelabelIn, x_admin_key: Optional[str] = Header(None)):
    """תיקון שם הקולט בהעברה (למשל אם נסרק סריאל לשדה)."""
    _require_admin(x_admin_key)
    return {"updated": db.relabel_receiver(body.op_id, body.name)}


@app.post("/api/admin/misroute/{mid}/resolve")
def admin_resolve_misroute(mid: int, x_admin_key: Optional[str] = Header(None)):
    """סגירת חריגת 'מכשיר לא במקום' ידנית ע"י מנהל."""
    _require_admin(x_admin_key)
    return {"resolved": db.resolve_misroute_by_id(mid)}


# ──────────────────────────────────────────────────────────────
# המלצות הזמנה (Order recommendations) + מאגר מכירות
# ──────────────────────────────────────────────────────────────
@app.get("/api/admin/recommendations")
def admin_recommendations(days: int = 30, branch: Optional[int] = None,
                          target_days: int = 21, x_admin_key: Optional[str] = Header(None)):
    """המלצות הזמנה לתקופה. סינון סוג/ספק/קטגוריה/חיפוש מתבצע בצד הלקוח."""
    _require_admin(x_admin_key)
    import order_recommend
    res = order_recommend.compute(days=days, branch_id=branch, target_days=target_days)
    # אם הקטלוג עוד לא נבנה (עלייה ראשונה/restart) — מפעילים בנייה ברקע, לא חוסמים את הבקשה
    if not res["meta"].get("catalog_ready"):
        scheduler.add_job(_catalog_refresh_job, "date", id="catalog_ondemand",
                          run_date=datetime.now() + timedelta(seconds=1), replace_existing=True)
    res["in_order"] = sorted(db.order_product_ids())
    res["branches"] = [{"id": b, "name": cfg.branch_name(b)} for b in (1, 2, 3, 4, 5)]
    return res


@app.post("/api/admin/catalog-refresh")
def admin_catalog_refresh(x_admin_key: Optional[str] = Header(None)):
    """רענון קטלוג המוצרים ל-DB ידנית (רץ ברקע)."""
    _require_admin(x_admin_key)
    scheduler.add_job(_catalog_refresh_job, "date", id="catalog_manual",
                      run_date=datetime.now() + timedelta(seconds=1), replace_existing=True)
    return {"started": True, "meta": db.catalog_meta()}


# ──────────────────────────────────────────────────────────────
# 🔎 מלאי חי — חיפוש מוצר (מה-DB) + קריאת מלאי/סריאלים חיה מהקופה
# ──────────────────────────────────────────────────────────────
@app.get("/api/admin/live-search/catalog")
def admin_live_catalog(x_admin_key: Optional[str] = Header(None), x_device_token: Optional[str] = Header(None)):
    """קטלוג מצומצם לצמצום תוך-כדי-הקלדה בצד הלקוח (שמות/ברקודים — לא מלאי)."""
    _require_admin_or_device(x_admin_key, x_device_token)
    meta = db.catalog_meta()
    return {"items": db.catalog_light(), "updated_at": meta.get("updated_at"),
            "count": meta.get("count")}


@app.get("/api/admin/live-search/serial")
def admin_live_serial(q: str, x_admin_key: Optional[str] = Header(None), x_device_token: Optional[str] = Header(None)):
    """איתור מוצרים לפי מספר סידורי — גם חלקי (מאינדקס סריאל→מוצר; אין ל-NewOrder חיפוש הפוך)."""
    _require_admin_or_device(x_admin_key, x_device_token)
    matches = db.serial_search((q or "").strip(), limit=50)
    # סטטוס דינמי (בהעברה/משוריין) — מוצג גם בחיפוש לפי סריאל, מבחוץ
    dyn = db.serial_dynamic_status([m["serial"] for m in matches])
    for m in matches:
        d = dyn.get(str(m["serial"]))
        m["dyn_kind"] = (d or {}).get("kind")
        m["dyn_branch"] = cfg.branch_name((d or {}).get("to_branch")) if d else ""
    return {"found": bool(matches), "matches": matches}


# micro-cache קצרצר כדי לרכך לחיצות כפולות/כמה מסכי ניהול במקביל — עדיין "חי" לכל דבר
_live_stock_cache: dict = {}
# cache משותף לכל הסניפים — עם כמה סניפים פעילים, ערך גבוה יותר חוסך הצפת
# הטוקן המשותף (100/דקה). 90ש' מספיק טרי למענה ללקוח על "יש במלאי?".
_LIVE_STOCK_TTL_SEC = 90


def _apply_dynamic(out: dict, pid: str) -> dict:
    """מזריק שכבת שריון/העברה (מ-DB שלנו) על תשובה — תמיד טרי, גם מ-cache.
    כך אישור קליטה/ביטול בקשה מתבטא מיד ולא תקוע ב-cache של הקופה."""
    bstat = db.product_branch_status(pid)
    for b in out.get("branches", []):
        st = bstat.get(b["id"])
        b["dyn_kind"] = (st or {}).get("kind")
        b["dyn_to"] = cfg.branch_name(st["to_branch"]) if st else ""
        b["dyn_n"] = (st or {}).get("n", 0)
    if "serials" in out and out["serials"]:
        dyn = db.serial_dynamic_status([s.get("serial") for s in out["serials"]])
        for s in out["serials"]:
            d = dyn.get(str(s.get("serial")))
            s["dyn_kind"] = (d or {}).get("kind")
            s["dyn_branch"] = cfg.branch_name(d["to_branch"]) if d else ""
    return out


@app.get("/api/admin/live-stock/{pid}")
def admin_live_stock(pid: str, serials: int = 0, fresh: int = 0,
                     x_admin_key: Optional[str] = Header(None), x_device_token: Optional[str] = Header(None)):
    """מלאי חי לפי סניף ישירות מהקופה, ואופציונלית גם היחידות הסריאליות (ספק+אחריות).
    נתוני הקופה ב-cache (90ש'), אבל שכבת השריון/העברה תמיד מחושבת טרי."""
    _require_admin_or_device(x_admin_key, x_device_token)
    import time as _time
    import copy as _copy
    key = (str(pid), bool(serials))
    hit = _live_stock_cache.get(key)
    if hit and not fresh and (_time.time() - hit[0]) < _LIVE_STOCK_TTL_SEC:
        return _apply_dynamic(_copy.deepcopy(hit[1]), pid)
    no = poller.client()
    try:
        stock = no.get_product_stock(pid)
    except Exception as e:  # noqa: BLE001
        logger.warning("live stock failed for %s: %s", pid, e)
        raise HTTPException(502, "לא ניתן לקרוא מהקופה כרגע")
    out = {
        "product_id": str(pid),
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "branches": [{"id": b, "name": cfg.branch_name(b), "qty": stock.get(b, 0) or 0}
                     for b in cfg.BRANCHES],
    }
    if serials:
        try:
            raw = no.get_product_serials(pid) or []
        except Exception as e:  # noqa: BLE001
            logger.warning("live serials failed for %s: %s", pid, e)
            raw = None
        if raw is None:
            out["serials_error"] = True
        else:
            ser = []
            for s in raw:
                try:
                    bid = int(s.get("branchId"))
                except (TypeError, ValueError):
                    bid = None
                sup = s.get("supplier") or {}
                war = s.get("warranty") or {}
                ser.append({
                    "serial": s.get("serial"), "status": s.get("status"),
                    "branch_id": bid, "branch_name": cfg.branch_name(bid) if bid else "",
                    "insert_date": s.get("insertDate") or "",
                    "supplier": (sup.get("name") or "").strip() if isinstance(sup, dict) else "",
                    "warranty": (war.get("name") or "").strip() if isinstance(war, dict) else "",
                    "warranty_months": war.get("duration") if isinstance(war, dict) else None,
                })
            out["serials"] = ser
    _live_stock_cache[key] = (_time.time(), out)   # ה-cache שומר נתוני קופה בלבד
    return _apply_dynamic(_copy.deepcopy(out), pid)


# ── חיבור לאתר (WooCommerce) — אייקון "אתר" בטאב מלאי חי ──────────
# מק"ט בקופה == SKU באתר. found=וריאציה/מוצר מחובר. creds מ-.env (WC_*); בלעדיהם — האייקון מוסתר.
_wc_cache: dict = {}
_WC_TTL_SEC = 900
_wc_full_cache: dict = {}   # תשובת חלונית מלאה (pos+variants) — cache קצר משותף
_WC_FULL_TTL_SEC = 180


def _wc_creds():
    u = os.getenv("WC_STORE_URL", "").rstrip("/")
    k = os.getenv("WC_CONSUMER_KEY", "")
    s = os.getenv("WC_CONSUMER_SECRET", "")
    return (u, k, s) if (u and k and s) else None


# ── העשרת שורות העברה/שידור בצבע (מ-WooCommerce לפי SKU) ──
# שם המוצר מהקופה (NewOrder) לא תמיד כולל צבע (למשל אביזרים — "Xbox Wireless
# Controller" בלי "לבן"). מושכים את הצבע מהווריאציה ב-WooCommerce לפי SKU
# (מק"ט בקופה == SKU באתר) ומציגים אותו לצד המק"ט, כדי שהסניף ידע מה להעביר.
_sku_color_cache: dict = {}
_SKU_COLOR_TTL = 24 * 3600


def _variant_color(sku) -> str:
    """צבע הווריאציה ('בחירת צבע') מ-WooCommerce לפי SKU. '' אם אין/לא וריאציה. cache 24ש."""
    sku = str(sku or "").strip()
    if not sku:
        return ""
    import time as _t
    hit = _sku_color_cache.get(sku)
    if hit and (_t.time() - hit[0]) < _SKU_COLOR_TTL:
        return hit[1]
    creds = _wc_creds()
    if not creds:
        return ""
    base, k, s = creds
    color = ""
    try:
        import requests as _rq
        r = _rq.get(base + "/wp-json/wc/v3/products", params={"sku": sku},
                    auth=(k, s), timeout=8)
        arr = r.json() if r.ok else []
        p = arr[0] if isinstance(arr, list) and arr else None
        for a in ((p or {}).get("attributes") or []):
            if a.get("name") == "בחירת צבע" and a.get("option"):
                color = a.get("option")
                break
    except Exception as e:  # noqa: BLE001
        logger.warning("variant color lookup failed for %s: %s", sku, e)
        return ""   # לא שומרים כשל ב-cache — ננסה שוב בפעם הבאה
    _sku_color_cache[sku] = (_t.time(), color)
    return color


def _enrich_color(lines):
    """מוסיף שדה color לכל שורה (ריק אם אין). לא משנה את השם. fail-soft."""
    for ln in lines or []:
        try:
            if not ln.get("color"):
                ln["color"] = _variant_color(ln.get("product_id"))
        except Exception:  # noqa: BLE001
            ln.setdefault("color", "")
    return lines


@app.get("/api/admin/wc-link/{sku}")
def admin_wc_link(sku: str, pos: int = 0, fallback: int = 0, fresh: int = 0,
                  name: Optional[str] = None,
                  x_admin_key: Optional[str] = Header(None), x_device_token: Optional[str] = Header(None)):
    """איתור הפריט באתר לפי SKU. pos=1 מוסיף מחיר קופה חי (להשוואת מחיר — כלל חובה);
    fallback=1 מנסה למצוא את עמוד המוצר הראשי לפי שם; fresh=1 עוקף את ה-cache
    (פתיחת חלונית — כדי שחיבור מק"ט טרי ייראה מיד)."""
    _require_admin_or_device(x_admin_key, x_device_token)
    creds = _wc_creds()
    if not creds:
        return {"available": False}
    import time as _time
    import requests as _rq
    base, k, s = creds
    # cache של התשובה המלאה (כולל מחיר קופה + וריאציות) — חוסך את קריאות
    # ה-NewOrder/WooCommerce הכבדות בפתיחות חוזרות מכמה סניפים. ~3 דק'.
    _full_key = (sku, bool(pos), bool(fallback))
    if not fresh:
        fhit = _wc_full_cache.get(_full_key)
        if fhit and (_time.time() - fhit[0]) < _WC_FULL_TTL_SEC:
            return dict(fhit[1])
    hit = None if fresh else _wc_cache.get(sku)
    if hit and (_time.time() - hit[0]) < _WC_TTL_SEC:
        out = dict(hit[1])
    else:
        out = {"available": True, "found": False, "sku": sku, "store": base}
        try:
            r = _rq.get(base + "/wp-json/wc/v3/products", params={"sku": sku},
                        auth=(k, s), timeout=15)
            items = r.json() if r.ok else []
        except Exception as e:  # noqa: BLE001
            logger.warning("wc lookup failed for %s: %s", sku, e)
            items = []
        p = items[0] if isinstance(items, list) and items else None
        if p:
            img = (p.get("image") or (p.get("images") or [{}])[0] or {})
            out.update({
                "found": True, "type": p.get("type"), "name": p.get("name"),
                "id": p.get("id"), "parent_id": p.get("parent_id"),
                "price": p.get("price"), "regular_price": p.get("regular_price"),
                "sale_price": p.get("sale_price"), "permalink": p.get("permalink"),
                "image": (img or {}).get("src", ""),
                "attributes": [{"name": a.get("name"), "option": a.get("option")}
                               for a in (p.get("attributes") or []) if a.get("option")],
            })
        _wc_cache[sku] = (_time.time(), dict(out))
    if fallback and not out.get("found") and name:
        # ניסיון לעמוד הראשי לפי שם (האייקון נשאר "כבוי", אבל יש לאן לפתוח)
        try:
            q = (name or "").split(" - ")[0].strip()[:60]
            r = _rq.get(base + "/wp-json/wc/v3/products",
                        params={"search": q, "per_page": 1}, auth=(k, s), timeout=15)
            arr = r.json() if r.ok else []
            if arr:
                f = arr[0]
                fimg = (f.get("images") or [{}])[0] or {}
                out["fallback"] = {"name": f.get("name"), "permalink": f.get("permalink"),
                                   "price": f.get("price"), "image": fimg.get("src", "")}
        except Exception as e:  # noqa: BLE001
            logger.warning("wc fallback search failed for %s: %s", sku, e)
    if pos:
        # מחיר קופה חי — תמיד להשוות מחיר קופה↔אתר לפני אזכור מחיר ללקוח
        try:
            prod = poller.client().get_product(sku)
            out["pos_price"] = prod.get("price") if prod else None
        except Exception as e:  # noqa: BLE001
            logger.warning("pos price lookup failed for %s: %s", sku, e)
            out["pos_price"] = None
    if pos and out.get("found") and out.get("type") == "variation":
        # וריאציות-אחיות (רק בפתיחת חלונית): אותו צבע+נפח (זהות המק"ט בקופה),
        # מאפיינים אחרים שמשנים מחיר (אחריות יבואן וכד') → כפתורי החלפה בחלונית
        _IDENT = {"בחירת צבע", "בחירת נפח אחסון"}
        try:
            parent = out.get("parent_id")
            if parent:
                r = _rq.get(base + f"/wp-json/wc/v3/products/{parent}/variations",
                            params={"per_page": 100}, auth=(k, s), timeout=20)
                vars_ = r.json() if r.ok else []
                mine = {a["name"]: a["option"] for a in out.get("attributes", [])}
                ident = {n: v for n, v in mine.items() if n in _IDENT}
                cands = []
                for v in vars_:
                    va = {a.get("name"): a.get("option") for a in (v.get("attributes") or [])}
                    if any(va.get(n) != val for n, val in ident.items()):
                        continue
                    vimg = (v.get("image") or {})
                    cands.append({"id": v.get("id"), "sku": v.get("sku") or "", "price": v.get("price"),
                                  "regular_price": v.get("regular_price"), "sale_price": v.get("sale_price"),
                                  "permalink": v.get("permalink"), "image": vimg.get("src", ""),
                                  "attrs": {n: va.get(n) for n in va if n not in ident and va.get(n)}})
                switch = {}
                for c in cands:
                    for n, vv in c["attrs"].items():
                        switch.setdefault(n, set()).add(vv)
                switch_attrs = [{"name": n, "options": sorted(vals)}
                                for n, vals in switch.items() if len(vals) > 1]
                if switch_attrs and len(cands) > 1:
                    out["variants"] = {"fixed": [{"name": n, "option": v} for n, v in ident.items()],
                                       "switch_attrs": switch_attrs, "options": cands}
        except Exception as e:  # noqa: BLE001
            logger.warning("wc sibling variations failed for %s: %s", sku, e)
    _wc_full_cache[_full_key] = (_time.time(), dict(out))
    return out


@app.get("/api/admin/wc-search")
def admin_wc_search(q: str = "", x_admin_key: Optional[str] = Header(None),
                    x_device_token: Optional[str] = Header(None)):
    """חיפוש מוצרים באתר לפי שם — להצמדת מק"ט קופה לווריאציה/מוצר שאינו מחובר.
    מחזיר רשימה שטוחה: לכל מוצר פשוט שורה אחת, לכל מוצר משתנה שורה לכל וריאציה.
    מסומן sku אם כבר תפוס (כדי שלא נדרוס בטעות)."""
    _require_admin_or_device(x_admin_key, x_device_token)
    creds = _wc_creds()
    q = (q or "").strip()
    if not creds or len(q) < 2:
        return {"results": []}
    base, k, s = creds
    import requests as _rq
    import re as _re

    def _fetch(query):
        try:
            r = _rq.get(base + "/wp-json/wc/v3/products",
                        params={"search": query, "per_page": 12, "status": "publish"},
                        auth=(k, s), timeout=15)
            if not r.ok:
                logger.warning("wc-search %s -> %s %s", query, r.status_code, r.text[:160])
                return []
            j = r.json()
            return j if isinstance(j, list) else []
        except Exception as e:  # noqa: BLE001
            logger.warning("wc-search failed for %s: %s", query, e)
            return []

    def _is_junk_sku(sku):
        """מק"ט משוכפל — שריד משכפול ליסט (WooCommerce מוסיף סיומת -1/-1-1 כשמנסים
        לשמור מק"ט כפול). מק"טי קופה אמיתיים הם מספר נקי בלי מקפים. מק"ט כזה אינו
        חיבור אמיתי — מתייחסים אליו כלא-מחובר, וההתאמה באה מהדגם/צבע/נפח, לא ממנו."""
        return bool(_re.fullmatch(r"\d+(-\d+)+", (sku or "").strip()))

    # שם המוצר בקופה הוא באנגלית וכולל צבע/נפח (למשל "Xiaomi Redmi 15C 128GB Blue"),
    # אבל שם המוצר באתר בעברית והצבע/נפח הם וריאציות — לכן המחרוזת המלאה מחזירה 0.
    # אם החיפוש המלא ריק — מסירים מילות צבע/נפח ואז מקצרים מהסוף עד שנמצא.
    _COLORS = {"black", "white", "blue", "green", "red", "gold", "silver", "gray",
               "grey", "orange", "purple", "pink", "yellow", "titanium", "graphite",
               "cream", "lavender", "mint", "navy", "beige", "rose",
               "שחור", "לבן", "כחול", "ירוק", "אדום", "זהב", "כסף", "כסוף", "אפור",
               "כתום", "סגול", "ורוד", "צהוב", "תכלת", "חום", "קרם", "טיטניום"}
    prods = _fetch(q)
    if not prods:
        toks = q.split()
        # מסירים צבעים וטוקני נפח/RAM (128GB / 256gb / 8GB / 1TB)
        core = [t for t in toks if t.lower() not in _COLORS
                and not _re.fullmatch(r"\d+(gb|tb)", t.lower())]
        if core and core != toks:
            prods = _fetch(" ".join(core))
        # עדיין ריק — מקצרים מילה-מילה מהסוף (ברנד+דגם לרוב בהתחלה)
        while not prods and len(core) > 1:
            core = core[:-1]
            prods = _fetch(" ".join(core))
    out = []
    for p in (prods or []):
        if not isinstance(p, dict):
            continue
        pid = p.get("id")
        pname = p.get("name") or ""
        img = ((p.get("images") or [{}])[0] or {}).get("src", "")
        if p.get("type") == "variable":
            try:
                rv = _rq.get(base + f"/wp-json/wc/v3/products/{pid}/variations",
                             params={"per_page": 100}, auth=(k, s), timeout=20)
                vars_ = rv.json() if rv.ok else []
            except Exception as e:  # noqa: BLE001
                logger.warning("wc-search variations failed for %s: %s", pid, e)
                vars_ = []
            for v in (vars_ or []):
                attrs = " / ".join(a.get("option") for a in (v.get("attributes") or [])
                                   if a.get("option"))
                vimg = (v.get("image") or {}).get("src", "") or img
                vsku = v.get("sku") or ""
                out.append({"product_id": pid, "variation_id": v.get("id"),
                            "name": pname, "label": attrs or "וריאציה",
                            "sku": vsku, "junk": _is_junk_sku(vsku),
                            "price": v.get("price"),
                            "image": vimg, "type": "variation"})
        else:
            psku = p.get("sku") or ""
            out.append({"product_id": pid, "variation_id": 0,
                        "name": pname, "label": "", "sku": psku,
                        "junk": _is_junk_sku(psku),
                        "price": p.get("price"), "image": img, "type": "simple"})
    # ── דירוג לפי רלוונטיות (דגם + נפח + צבע) ──
    # WC מחזיר את כל הווריאציות של כל מוצר שמתאים בשם → בלי דירוג זה "מציף" את כל
    # הצבעים/נפחים. מדרגים כל וריאציה מול מילות החיפוש המקוריות: נפח (256gb) וצבע
    # (Porcelain→פורצלן) במשקל גבוה, טוקני דגם (10/pro/xl) נמוך. match=True רק אם
    # גם הנפח וגם הצבע שביקש נמצאים — כדי שלא "יחליף" וריאציה בצבע אחר בטעות.
    _COLOR_HE = {
        "porcelain": "פורצלן", "obsidian": "אובסידיאן", "moonstone": "מונסטון",
        "hazel": "הייזל", "jade": "ג׳ייד", "black": "שחור", "white": "לבן",
        "gray": "אפור", "grey": "אפור", "green": "ירוק", "blue": "כחול",
        "red": "אדום", "pink": "ורוד", "purple": "סגול", "gold": "זהב",
        "silver": "כסף", "cream": "קרם", "lavender": "לבנדר", "mint": "מנטה",
        "titanium": "טיטניום", "graphite": "גרפיט", "navy": "כחול", "orange": "כתום",
        "yellow": "צהוב", "beige": "בז׳", "coral": "אלמוג",
    }
    _TIERS = {"pro", "xl", "ultra", "max", "plus", "fe", "mini", "air", "edge",
              "fold", "flip", "neo", "lite", "prime", "ace", "se"}
    qtl = [t for t in _re.split(r"[\s/,]+", q.lower()) if t]
    q_storage = [t for t in qtl if _re.fullmatch(r"\d+(gb|tb)", t)]
    q_colors = [t for t in qtl if t in _COLOR_HE]
    # מספרי דגם (10/9/15) וטוקני דרגה (pro/xl/ultra) — מבדילים בין Pixel 10 ל-9
    # ובין Pro ל-Pro XL. חובה שיתאימו ל-match, אחרת "החלף" ידביק לדגם הלא-נכון.
    q_models = [t for t in qtl if _re.fullmatch(r"\d{1,3}", t)]
    q_tiers = [t for t in qtl if t in _TIERS]
    for row in out:
        hay = (str(row.get("name", "")) + " " + str(row.get("label", ""))).lower()
        hay_ns = hay.replace(" ", "")
        hay_toks = set(_re.split(r"[\s/,\-]+", hay))
        sc = 0
        st_ok = bool(q_storage) and all(st in hay_ns for st in q_storage)
        for st in q_storage:
            if st in hay_ns:
                sc += 4
        col_ok = False
        for c in q_colors:
            if _COLOR_HE[c] in hay or c in hay:
                sc += 4
                col_ok = True
        # מספר דגם וטוקני דרגה — משקל הגבוה ביותר (מבדילים דגם 10 מ-9, Pro מ-XL),
        # מעל צבע, כדי שהדגם הנכון יעלה למעלה גם כשהצבע המבוקש לא קיים. נדרשים ל-match.
        model_ok = all(m in hay_toks for m in q_models)
        tier_ok = all(t in hay_toks for t in q_tiers)
        for m in q_models:
            if m in hay_toks:
                sc += 6
        for t in q_tiers:
            if t in hay_toks:
                sc += 3
        for t in qtl:
            if t in q_storage or t in q_colors or t in q_models or t in q_tiers or len(t) < 2:
                continue
            if t in hay:
                sc += 1
        row["score"] = sc
        # התאמה מלאה: נפח + צבע + מספר-דגם + דרגה שביקש (אם ביקש) — כולם תואמים
        asked = bool(q_storage or q_colors or q_models or q_tiers)
        row["match"] = bool(asked
                            and (st_ok or not q_storage)
                            and (col_ok or not q_colors)
                            and model_ok and tier_ok)
    out.sort(key=lambda r: r.get("score", 0), reverse=True)
    return JSONResponse({"results": out},
                        headers={"Cache-Control": "no-store"})


class WcConnectIn(BaseModel):
    product_id: int
    variation_id: int = 0
    sku: str


@app.post("/api/admin/wc-connect")
def admin_wc_connect(body: WcConnectIn, x_admin_key: Optional[str] = Header(None),
                     x_device_token: Optional[str] = Header(None)):
    """מצמיד מק"ט קופה (NewOrder) לווריאציה/מוצר באתר. variation_id=0 → מוצר פשוט.
    בודק שהמק"ט לא תפוס כבר במקום אחר (SKU חייב להיות ייחודי בכל החנות)."""
    actor = _actor_name(x_admin_key, x_device_token)
    _require_admin_or_device(x_admin_key, x_device_token)
    creds = _wc_creds()
    if not creds:
        raise HTTPException(503, "wc not configured")
    base, k, s = creds
    sku = (body.sku or "").strip()
    if not sku:
        raise HTTPException(400, "missing sku")
    import requests as _rq
    # בדיקת ייחודיות — אם המק"ט כבר תפוס במוצר/וריאציה אחרים, לא דורסים
    try:
        rc = _rq.get(base + "/wp-json/wc/v3/products", params={"sku": sku},
                     auth=(k, s), timeout=15)
        ex = rc.json() if rc.ok else []
        if isinstance(ex, list) and ex:
            e0 = ex[0]
            same = (e0.get("id") == body.variation_id) if body.variation_id \
                else (e0.get("id") == body.product_id)
            if not same:
                return JSONResponse(
                    {"ok": False, "reason": "taken",
                     "by": {"id": e0.get("id"), "name": e0.get("name"),
                            "permalink": e0.get("permalink")}},
                    status_code=409, headers={"Cache-Control": "no-store"})
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("wc-connect uniqueness check failed for %s: %s", sku, e)
    # כתיבת ה-SKU
    if body.variation_id:
        url = base + f"/wp-json/wc/v3/products/{body.product_id}/variations/{body.variation_id}"
    else:
        url = base + f"/wp-json/wc/v3/products/{body.product_id}"
    try:
        rp = _rq.put(url, json={"sku": sku}, auth=(k, s), timeout=20)
        if not rp.ok:
            logger.warning("wc-connect PUT %s -> %s %s", url, rp.status_code, rp.text[:200])
            return JSONResponse({"ok": False, "reason": "wc_error",
                                 "detail": rp.text[:200]},
                                status_code=502, headers={"Cache-Control": "no-store"})
        d = rp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("wc-connect PUT failed for %s: %s", sku, e)
        raise HTTPException(502, "wc write failed")
    # ניקוי cache כדי שהאייקון יידלק מיד בפתיחה הבאה
    _sku_color_cache.pop(sku, None)
    _wc_cache.pop(sku, None)
    for kk in [kx for kx in _wc_full_cache if kx[0] == sku]:
        _wc_full_cache.pop(kk, None)
    logger.info("wc-connect: sku %s -> id %s by %s", sku, d.get("id"), actor or "?")
    return JSONResponse({"ok": True, "id": d.get("id"),
                         "permalink": d.get("permalink"), "name": d.get("name")},
                        headers={"Cache-Control": "no-store"})


# ── אבטחת מכשירים (device allowlist) ───────────────────────────────
# כל דפדפן מזדהה ב-X-Device-Token. מכשיר חדש = ממתין לאישור אסי בטלגרם
# (כפתורי אשר/דחה כקישורים חתומים). מכשיר קיים עם סניף שמור = אישור אוטומטי.
# אכיפה ב-middleware על endpoints של נתוני סניפים; מופעלת עם DEVICE_ENFORCE=1.
import hashlib
import hmac as _hmac

DEVICE_ENFORCE = os.getenv("DEVICE_ENFORCE", "").strip() in ("1", "true", "yes")
TG_BOT = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_ADMIN_CHAT = os.getenv("TELEGRAM_ADMIN_CHAT", "448181407").strip()
_GATED = ("/api/transfers", "/api/scan", "/api/broadcast", "/api/stats")


def _sig(token: str, action: str) -> str:
    secret = os.getenv("SENTINEL_KEY", "") or cfg.ADMIN_PASSWORD or "gm"
    return _hmac.new(secret.encode(), f"{token}:{action}".encode(), hashlib.sha256).hexdigest()[:24]


def _tg_admin(text: str, buttons=None):
    if not TG_BOT or not TG_ADMIN_CHAT:
        logger.warning("telegram not configured — skipping device alert")
        return
    payload = {"chat_id": TG_ADMIN_CHAT, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": [buttons]}
    try:
        import requests as _rq
        _rq.post(f"https://api.telegram.org/bot{TG_BOT}/sendMessage", json=payload, timeout=15)
    except Exception as e:  # noqa: BLE001
        logger.warning("telegram send failed: %s", e)


def _ua_human(ua: str) -> str:
    ua = ua or ""
    os_n = ("iPhone" if "iPhone" in ua else "iPad" if "iPad" in ua else
            "Android" if "Android" in ua else "Mac" if "Macintosh" in ua else
            "Windows" if "Windows" in ua else "Linux" if "Linux" in ua else "?")
    br = ("Edge" if "Edg/" in ua else "Samsung" if "SamsungBrowser" in ua else
          "Chrome" if "Chrome/" in ua else "Safari" if "Safari/" in ua else
          "Firefox" if "Firefox/" in ua else "?")
    return f"{os_n} · {br}"


def _client_ip(request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else ""))


@app.middleware("http")
async def _device_gate(request, call_next):
    p = request.url.path
    if DEVICE_ENFORCE and any(p.startswith(g) for g in _GATED):
        akey = request.headers.get("x-admin-key", "")
        tok = request.headers.get("x-device-token", "")
        if cfg.ADMIN_PASSWORD and akey == cfg.ADMIN_PASSWORD:
            pass
        else:
            d = db.device_get(tok) if tok else None
            if not d or d.get("status") != "approved":
                return JSONResponse({"detail": "device not approved", "code": "device"}, status_code=401)
            # נעילת סניף: מכשיר מאושר עובד רק מול הסניף הנעול שלו
            bid = request.query_params.get("branch_id")
            if bid:
                locked = _device_branch(d)
                if locked and str(bid) != locked:
                    return JSONResponse({"detail": "המכשיר נעול לסניף אחר — החלפת סניף דורשת אישור מנהל",
                                         "code": "branch-locked"}, status_code=401)
            try:
                db.device_touch(tok, bid)
            except Exception:  # noqa: BLE001
                pass
    return await call_next(request)


class DeviceRegIn(BaseModel):
    token: str
    name: Optional[str] = ""
    had_branch: Optional[bool] = False
    branch_id: Optional[str] = ""


@app.post("/api/device/register")
def device_register(body: DeviceRegIn, request: Request):
    tok = (body.token or "").strip()
    if not tok or len(tok) < 16:
        raise HTTPException(400, "bad token")
    existing = db.device_get(tok)
    if existing:
        return {"status": existing["status"]}
    ua = request.headers.get("user-agent", "")
    ip = _client_ip(request)
    base = (cfg.APP_BASE_URL or "https://gm-transfers.onrender.com").rstrip("/")
    if body.had_branch:
        # grandfathering: דפדפן שכבר עבד עם סניף שמור — אישור אוטומטי + יידוע
        db.device_register(tok, body.name or "מכשיר קיים", ua, ip,
                           body.branch_id, "approved", auto=True)
        bn = cfg.branch_name(body.branch_id) if body.branch_id else "?"
        _tg_admin(f"🖥️ <b>מכשיר קיים אושר אוטומטית</b>\n"
                  f"סניף: <b>{bn}</b> · {_ua_human(ua)} · IP {ip}\n"
                  f"<i>דפדפן שכבר היה מחובר לפני הפעלת האבטחה</i>")
        return {"status": "approved"}
    name = (body.name or "").strip()
    if len(name) < 2:
        raise HTTPException(400, "נדרש שם מבקש")
    db.device_register(tok, name[:60], ua, ip, body.branch_id, "pending")
    ok_url = f"{base}/device-action?token={tok}&action=approve&sig={_sig(tok,'approve')}"
    no_url = f"{base}/device-action?token={tok}&action=deny&sig={_sig(tok,'deny')}"
    _tg_admin(f"🔐 <b>מכשיר חדש מבקש גישה למערכת</b>\n"
              f"👤 מבקש: <b>{name}</b>\n"
              f"💻 {_ua_human(ua)}\n🌐 IP: <code>{ip}</code>\n"
              f"לאשר כניסה?",
              buttons=[{"text": "✅ אשר", "url": ok_url},
                       {"text": "❌ דחה", "url": no_url}])
    return {"status": "pending"}


@app.get("/api/device/status")
def device_status(token: str):
    d = db.device_get(token)
    return {"status": (d or {}).get("status", "unknown")}


# ── נעילת סניף: החלפת סניף במכשיר סניפי דורשת אישור מנהל בטלגרם ──
class BranchChangeIn(BaseModel):
    token: str
    to_branch: str


@app.post("/api/device/branch-change")
def device_branch_change(body: BranchChangeIn, request: Request):
    d = db.device_get((body.token or "").strip())
    if not d or d.get("status") != "approved":
        raise HTTPException(401, "מכשיר לא מאושר")
    to = str(body.to_branch or "").strip()
    if to not in {str(b) for b in cfg.BRANCHES}:
        raise HTTPException(400, "סניף לא מוכר")
    if _device_branch(d) == to:
        return {"status": "approved"}
    db.sales_state_set(f"brchg:{d['token']}", json_mod.dumps(
        {"to": to, "status": "pending", "at": datetime.now().isoformat(timespec="seconds")}))
    base = (cfg.APP_BASE_URL or "https://gm-transfers.onrender.com").rstrip("/")
    ok_url = f"{base}/branch-action?token={d['token']}&to={to}&action=approve&sig={_sig(d['token'], f'brchg:{to}:approve')}"
    no_url = f"{base}/branch-action?token={d['token']}&to={to}&action=deny&sig={_sig(d['token'], f'brchg:{to}:deny')}"
    _tg_admin(f"🔁 <b>בקשת החלפת סניף</b>\n"
              f"🖥️ מכשיר: <b>{d.get('name') or '?'}</b> · {_ua_human(d.get('ua') or '')}\n"
              f"מסניף <b>{cfg.branch_name(_device_branch(d)) or '?'}</b> ← לסניף <b>{cfg.branch_name(to)}</b>\n"
              f"לאשר?",
              buttons=[{"text": "✅ אשר", "url": ok_url},
                       {"text": "❌ דחה", "url": no_url}])
    return {"status": "pending"}


@app.get("/api/device/branch-status")
def device_branch_status(token: str):
    raw = db.sales_state_get(f"brchg:{token}")
    if not raw:
        return {"status": "none"}
    return json_mod.loads(raw)


@app.get("/branch-action")
def branch_action(token: str, to: str, action: str, sig: str):
    """קישור האישור/דחייה מהטלגרם — חתום HMAC, נפתח בדפדפן של אסי."""
    if action not in ("approve", "deny") or not _hmac.compare_digest(sig, _sig(token, f"brchg:{to}:{action}")):
        raise HTTPException(403, "bad signature")
    d = db.device_get(token)
    if not d:
        raise HTTPException(404, "device not found")
    if action == "approve":
        db.device_set_locked(token, to)
    db.sales_state_set(f"brchg:{token}", json_mod.dumps(
        {"to": to, "status": "approved" if action == "approve" else "denied",
         "at": datetime.now().isoformat(timespec="seconds")}))
    msg = (f"✅ המכשיר הועבר לסניף {cfg.branch_name(to)}" if action == "approve"
           else "❌ הבקשה נדחתה — המכשיר נשאר בסניף הנוכחי")
    from fastapi.responses import HTMLResponse
    return HTMLResponse(f"""<!doctype html><html dir="rtl"><head><meta charset="utf-8">
      <meta name="viewport" content="width=device-width,initial-scale=1"></head>
      <body style="font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:90vh;background:#f4f6f8">
      <div style="background:#fff;border-radius:16px;padding:34px 40px;box-shadow:0 8px 30px rgba(0,0,0,.12);text-align:center">
      <div style="font-size:44px">{'✅' if action=='approve' else '❌'}</div>
      <h2 style="margin:10px 0 4px">{msg}</h2>
      <div style="color:#777">{(d.get('name') or '')} · {_ua_human(d.get('ua') or '')}</div>
      </div></body></html>""")


@app.get("/device-action")
def device_action(token: str, action: str, sig: str):
    """קישור האישור/דחייה מהטלגרם — חתום HMAC, נפתח בדפדפן של אסי."""
    if action not in ("approve", "deny") or not _hmac.compare_digest(sig, _sig(token, action)):
        raise HTTPException(403, "bad signature")
    d = db.device_get(token)
    if not d:
        raise HTTPException(404, "device not found")
    db.device_set_status(token, "approved" if action == "approve" else "denied")
    msg = "✅ המכשיר אושר — אפשר לסגור את החלון" if action == "approve" else "❌ הבקשה נדחתה"
    from fastapi.responses import HTMLResponse
    return HTMLResponse(f"""<!doctype html><html dir="rtl"><head><meta charset="utf-8">
      <meta name="viewport" content="width=device-width,initial-scale=1"></head>
      <body style="font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:90vh;background:#f4f6f8">
      <div style="background:#fff;border-radius:16px;padding:34px 40px;box-shadow:0 8px 30px rgba(0,0,0,.12);text-align:center">
      <div style="font-size:44px">{'✅' if action=='approve' else '❌'}</div>
      <h2 style="margin:10px 0 4px">{msg}</h2>
      <div style="color:#777">{(d.get('name') or '')} · {_ua_human(d.get('ua') or '')}</div>
      </div></body></html>""")


@app.get("/api/admin/devices")
def admin_devices(x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    out = db.device_list()
    for d in out:
        d["ua_human"] = _ua_human(d.get("ua") or "")
        d["branch_name"] = cfg.branch_name(d.get("branch_hint")) if d.get("branch_hint") else ""
    return {"devices": out, "enforce": DEVICE_ENFORCE}


class DeviceSetIn(BaseModel):
    token: str
    status: str


@app.post("/api/admin/devices/set")
def admin_device_set(body: DeviceSetIn, x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    if body.status not in ("approved", "denied"):
        raise HTTPException(400, "bad status")
    return {"ok": db.device_set_status(body.token, body.status)}


# ── Ops Hub: סטטוס Sentinel (דביר) ─────────────────────────────────
# ה-Sentinel (GitHub Actions, שעתי) דוחף לכאן JSON בסוף כל ריצה; ה-hub מציג.
from fastapi import Request  # noqa: E402


@app.post("/api/sentinel/report")
async def sentinel_report(request: Request, x_sentinel_key: Optional[str] = Header(None)):
    key = os.getenv("SENTINEL_KEY", "").strip()
    if not key or (x_sentinel_key or "") != key:
        raise HTTPException(401, "bad sentinel key")
    import json as _json
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "bad payload")
    body["received_at"] = datetime.now().isoformat(timespec="seconds")
    db.sales_state_set("sentinel_report", _json.dumps(body, ensure_ascii=False))
    return {"ok": True}


@app.get("/api/admin/sentinel")
def admin_sentinel(x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    import json as _json
    raw = db.sales_state_get("sentinel_report")
    if not raw:
        return {"available": False}
    try:
        rep = _json.loads(raw)
    except Exception:  # noqa: BLE001
        return {"available": False}
    stale = _is_stale(rep.get("received_at"), hours=2.5)   # ריצה שעתית — מעל שעתיים וחצי = לא מדווח
    return {"available": True, "stale": stale, "report": rep}


# ── GreenOS: הטמעת אפליקציות native (reverse proxy, לא iframe) ──────
# גישה דרך cookie חתום שנקבע אחרי אימות אדמין/מכשיר (ניווט דפדפן לא נושא הדרים).
from fastapi import Response as _Resp  # noqa: E402


def _embed_cookie_val() -> str:
    secret = os.getenv("SENTINEL_KEY", "") or cfg.ADMIN_PASSWORD or "gm"
    return _hmac.new(secret.encode(), b"embed", hashlib.sha256).hexdigest()[:24]


@app.post("/api/embed/session")
def embed_session(x_admin_key: Optional[str] = Header(None),
                  x_device_token: Optional[str] = Header(None)):
    """פותח session להטמעות — אדמין או מכשיר מאושר. מחזיר cookie."""
    _require_admin_or_device(x_admin_key, x_device_token)
    resp = JSONResponse({"ok": True})
    resp.set_cookie("gos_embed", _embed_cookie_val(), max_age=43200,
                    httponly=True, samesite="lax")
    return resp


@app.api_route("/embed/{key}", methods=["GET", "POST", "PATCH", "DELETE"])
@app.api_route("/embed/{key}/{path:path}", methods=["GET", "POST", "PATCH", "DELETE"])
async def embed_proxy_route(key: str, request: Request, path: str = ""):
    import embed_proxy
    if not embed_proxy.app_for(key):
        raise HTTPException(404, "unknown embed")
    if request.cookies.get("gos_embed") != _embed_cookie_val():
        from fastapi.responses import HTMLResponse as _H
        return _H('<!doctype html><html dir="rtl"><body style="font-family:system-ui;text-align:center;padding-top:80px">'
                  '<h2>🔒 גישה דרך GreenOS בלבד</h2><p><a href="/">חזרה ל-GreenOS</a></p></body></html>',
                  status_code=401)
    body = await request.body()
    status, content, ct, headers = embed_proxy.proxy(
        key, path, request.method, str(request.url.query),
        body, request.headers.get("content-type", ""))
    return _Resp(content=content, status_code=status, media_type=ct or None,
                 headers={k: v for k, v in headers.items() if k.lower() not in ("content-type",)})


# ── Ops Hub: דף ניהול צ'קים (מתארח אצלנו, proxy במקום טוקן חשוף) ───
@app.get("/checks")
def checks_page():
    p = os.path.join(_static_dir, "checks.html")
    if os.path.exists(p):
        return FileResponse(p)
    raise HTTPException(404)


@app.post("/api/checks/gql")
async def checks_gql(request: Request, x_admin_key: Optional[str] = Header(None),
                     x_checks_key: Optional[str] = Header(None)):
    """GraphQL passthrough לבורד הצ'קים — אדמין, או מפתח צ'קים ייעודי (לבעלים,
    גישה לצ'קים בלבד בלי שום כניסה ל-GreenOS)."""
    checks_pw = os.getenv("CHECKS_PASSWORD", "").strip()
    admin_ok = (not cfg.ADMIN_PASSWORD) or (x_admin_key or "") == cfg.ADMIN_PASSWORD
    checks_ok = bool(checks_pw) and (x_checks_key or "") == checks_pw
    if not (admin_ok or checks_ok):
        raise HTTPException(401, "checks auth required")
    import monday_proxy
    if not monday_proxy.available():
        raise HTTPException(400, "MONDAY_API_TOKEN לא מוגדר")
    body = await request.json()
    q = (body or {}).get("query", "")
    if not q:
        raise HTTPException(400, "missing query")
    import requests as _rq
    r = _rq.post("https://api.monday.com/v2", json={"query": q},
                 headers={"Authorization": os.getenv("MONDAY_API_TOKEN", ""),
                          "API-Version": "2024-10"}, timeout=40)
    return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/api/checks/upload")
async def checks_upload(request: Request, x_admin_key: Optional[str] = Header(None),
                        x_checks_key: Optional[str] = Header(None)):
    """העלאת צילום צ'ק לעמודת הקבצים במאנדיי — דרך השרת (הטוקן בצד השרת בלבד).
    מחליף את ההעלאה הישנה לוורקר Cloudflare שהפסיקה לעבוד אחרי המיגרציה
    (הקליינט שלח token=undefined). מקבל multipart: item_id + file."""
    checks_pw = os.getenv("CHECKS_PASSWORD", "").strip()
    admin_ok = (not cfg.ADMIN_PASSWORD) or (x_admin_key or "") == cfg.ADMIN_PASSWORD
    checks_ok = bool(checks_pw) and (x_checks_key or "") == checks_pw
    if not (admin_ok or checks_ok):
        raise HTTPException(401, "checks auth required")
    token = os.getenv("MONDAY_API_TOKEN", "")
    if not token:
        raise HTTPException(400, "MONDAY_API_TOKEN לא מוגדר")
    form = await request.form()
    item_id = (form.get("item_id") or "").strip()
    column_id = (form.get("column_id") or "files__1").strip()
    up = form.get("file")
    if not item_id or up is None or not hasattr(up, "read"):
        raise HTTPException(400, "חסר item_id או קובץ")
    try:
        item_id_int = int(item_id)
    except ValueError:
        raise HTTPException(400, "item_id לא תקין")
    file_bytes = await up.read()
    filename = getattr(up, "filename", None) or "check.jpg"
    content_type = getattr(up, "content_type", None) or "image/jpeg"
    mutation = (f'mutation ($file: File!) {{ add_file_to_column('
                f'item_id: {item_id_int}, column_id: "{column_id}", file: $file) {{ id }} }}')
    import requests as _rq
    try:
        r = _rq.post("https://api.monday.com/v2/file",
                     headers={"Authorization": token, "API-Version": "2024-10"},
                     data={"query": mutation},
                     files={"variables[file]": (filename, file_bytes, content_type)},
                     timeout=90)
    except Exception as e:  # noqa: BLE001
        logger.warning("checks upload to monday failed: %s", e)
        raise HTTPException(502, "העלאת הקובץ למאנדיי נכשלה")
    try:
        j = r.json()
    except Exception:  # noqa: BLE001
        logger.warning("checks upload non-json (%s): %s", r.status_code, r.text[:200])
        raise HTTPException(502, f"מאנדיי החזיר תשובה לא תקינה ({r.status_code})")
    return JSONResponse(j, status_code=r.status_code)


# ── Ops Hub: משימות סוכנים (Monday proxy — הטוקן בצד השרת בלבד) ────
@app.get("/api/admin/tasks")
def admin_tasks(force: int = 0, x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    import monday_proxy
    if not monday_proxy.available():
        return {"available": False}
    try:
        return monday_proxy.fetch_tasks(force=bool(force))
    except Exception as e:  # noqa: BLE001
        logger.warning("monday tasks fetch failed: %s", e)
        raise HTTPException(502, "שגיאה בקריאת מאנדיי")


class TaskStatusIn(BaseModel):
    label: str


@app.post("/api/admin/tasks/{item_id}/status")
def admin_task_status(item_id: int, body: TaskStatusIn,
                      x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    import monday_proxy
    if not monday_proxy.available():
        raise HTTPException(400, "MONDAY_API_TOKEN לא מוגדר")
    try:
        return monday_proxy.set_status(item_id, body.label)
    except Exception as e:  # noqa: BLE001
        logger.warning("monday status update failed: %s", e)
        raise HTTPException(502, "שגיאה בעדכון מאנדיי")


# ── הורדות מלאי מרלוג ──────────────────────────────────────────────
@app.get("/api/admin/removals")
def admin_removals(days: int = 30, from_date: Optional[str] = None, to_date: Optional[str] = None,
                   x_admin_key: Optional[str] = Header(None)):
    """רשימת הורדות מלאי מרלוג. או לפי `days`, או טווח `from_date`/`to_date` (YYYY-MM-DD)."""
    _require_admin(x_admin_key)
    from datetime import date as _date, timedelta as _td
    if from_date:
        rows = db.removals_list(from_date, to_date or _date.today().isoformat())
    else:
        since = (_date.today() - _td(days=int(days or 30))).isoformat()
        rows = db.removals_list(since)
    return {"rows": rows, "summary": db.removals_summary(),
            "branch_name": cfg.branch_name(3)}


@app.get("/api/admin/removals-status")
def admin_removals_status(x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    return {"summary": db.removals_summary(),
            "last_run": db.sales_state_get("removals_last_run"),
            "backfill_last_run": db.sales_state_get("removals_backfill_last_run")}


@app.post("/api/admin/removals-ingest")
def admin_removals_ingest(x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    scheduler.add_job(_removals_ingest_job, "date", id="removals_manual",
                      run_date=datetime.now() + timedelta(seconds=1), replace_existing=True)
    return {"started": True}


@app.post("/api/admin/removals-backfill")
def admin_removals_backfill(days: int = 90, x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    scheduler.add_job(lambda: _removals_backfill_job(days), "date", id="removals_backfill_manual",
                      run_date=datetime.now() + timedelta(seconds=1), replace_existing=True)
    return {"started": True, "days": days}


@app.get("/api/admin/sales-status")
def admin_sales_status(x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    return {"summary": db.sales_summary(),
            "last_run": db.sales_state_get("last_run"),
            "last_date": db.sales_state_get("last_date"),
            "backfill_last_run": db.sales_state_get("backfill_last_run")}


@app.post("/api/admin/sales-ingest")
def admin_sales_ingest(x_admin_key: Optional[str] = Header(None)):
    """הפעלת איסוף מכירות מצטבר ידנית (רץ ברקע)."""
    _require_admin(x_admin_key)
    scheduler.add_job(_sales_ingest_job, "date", id="sales_ingest_manual",
                      run_date=datetime.now() + timedelta(seconds=1), replace_existing=True)
    return {"started": True}


@app.post("/api/admin/sold-reconcile")
def admin_sold_reconcile(x_admin_key: Optional[str] = Header(None)):
    """הפעלה ידנית: סגירת כרטיסי קליטה למכשירים שנמכרו לפני קליטה (+התראות)."""
    _require_admin(x_admin_key)
    rows = db.reconcile_sold_transfer_items()
    for r in rows:
        try:
            to_name = cfg.branch_name(r.get("to_branch_id"))
            sold_name = cfg.branch_name(r.get("sold_branch_id"))
            if r.get("same_branch"):
                head, where = ("📦✅ <b>מכשיר נמכר לפני קליטה</b>",
                               f"נמכר בסניף היעד (<b>{to_name}</b>) לפני קליטה.")
            else:
                head, where = ("📦↪️ <b>מכשיר נמכר בסניף שונה מהיעד</b>",
                               f"היעד היה <b>{to_name}</b>, אך נמכר ב<b>{sold_name}</b>.")
            cleared = ("כרטיס הקליטה נסגר ונוקה." if r.get("transfer_closed")
                       else "הפריט סומן כנמכר ונוקה.")
            _tg_admin(f"{head}\n{r.get('name')} (סריאלי <code>{r.get('serial')}</code>)\n{where}\n{cleared}")
        except Exception:  # noqa: BLE001
            pass
    return {"closed": len(rows), "items": rows}


@app.post("/api/admin/sales-backfill")
def admin_sales_backfill(days: int = 90, max_new_docs: int = 1500,
                         x_admin_key: Optional[str] = Header(None)):
    """איסוף היסטורי לאחור (חלק אחד, resumable). רץ ברקע, מווסת."""
    _require_admin(x_admin_key)
    scheduler.add_job(lambda: _sales_backfill_job(days, max_new_docs), "date",
                      id="sales_backfill_manual",
                      run_date=datetime.now() + timedelta(seconds=1), replace_existing=True)
    return {"started": True, "days": days, "max_new_docs": max_new_docs}


# טיוטת הזמנה (order_plan) — רשימה שטוחה
class OrderLine(BaseModel):
    product_id: str
    name: Optional[str] = ""
    qty: int = 1
    supplier: Optional[str] = ""
    category: Optional[str] = ""
    kind: Optional[str] = ""


class OrderAdd(BaseModel):
    lines: list[OrderLine]


class OrderReplace(BaseModel):
    product_id: str
    lines: list[OrderLine]


@app.get("/api/admin/order-plan")
def admin_order_plan(x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    return {"lines": db.order_list(), "count": db.order_count()}


@app.post("/api/admin/order-plan")
def admin_order_add(body: OrderAdd, x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    return {"added": db.order_add([l.model_dump() for l in body.lines])}


@app.post("/api/admin/order-plan/replace")
def admin_order_replace(body: OrderReplace, x_admin_key: Optional[str] = Header(None)):
    """מחליף את שורת ההזמנה למוצר (עריכה/הסרה). lines ריק = הסרה."""
    _require_admin(x_admin_key)
    return {"count": db.order_replace_product(body.product_id, [l.model_dump() for l in body.lines])}


@app.delete("/api/admin/order-plan/{rid}")
def admin_order_delete(rid: int, x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    return {"deleted": db.order_delete(rid)}


@app.post("/api/admin/order-plan/clear")
def admin_order_clear(x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    db.order_clear()
    return {"ok": True}


# ──────────────────────────────────────────────────────────────
# 💬 WhatsApp (ConnectOp) — טאב הוואטסאפ של GreenOS. אדמין בלבד.
# ──────────────────────────────────────────────────────────────
class WaSend(BaseModel):
    phone: str
    text: str
    reply_to: str = ""       # wamid של הודעה לציטוט (reply) — נשלח דרך Meta ישיר
    reply_preview: str = ""


class WaTemplate(BaseModel):
    phone: str
    name: str = ""
    body: str


class WaFlag(BaseModel):
    phone: str
    value: bool = True


def _wa_guard(fn, *args, **kwargs):
    import wa
    try:
        return fn(*args, **kwargs)
    except wa.WaError as e:
        raise HTTPException(502, str(e))


def _wa_read_native() -> bool:
    """האם להציג שיחות מהחנות שלנו (wa_msg) במקום מקונקטופ. True אם: env
    WA_READ_NATIVE=1, או מצב cutover='live' (אז הכל native ממילא), או דגל ריצה
    wa_read_native=1 (הצצה ידנית לשיחות הבוט גם במצב בדיקה). הפיך מיידית."""
    if os.getenv("WA_READ_NATIVE", "0").strip().lower() in ("1", "true", "yes", "on"):
        return True
    try:
        import wa_bot
        if wa_bot.cutover_mode() == "live":
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        return (db.sales_state_get("wa_read_native") or "0").strip().lower() in ("1", "true", "on")
    except Exception:  # noqa: BLE001
        return False


@app.get("/api/admin/wa/conversations")
def wa_conversations(archived: int = 0, x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    import wa
    if _wa_read_native():
        convs = wa.list_conversations_native()
        if not archived:
            convs = [c for c in convs if not c.get("archived")]
        return {"conversations": convs, "source": "native"}
    return {"conversations": _wa_guard(wa.list_conversations,
                                       include_archived=bool(archived))}


@app.get("/api/admin/wa/thread/{phone}")
def wa_thread(phone: str, limit: int = 60, x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    import wa
    if _wa_read_native():
        return wa.get_thread_native(phone, limit=min(limit, 200))
    return _wa_guard(wa.get_thread, phone, limit=min(limit, 200))


def _bot_handoff_on(phone):
    """מענה אנושי → השתלטות אוטומטית: מסמן את השיחה 'agent' (הבוט משתתק) ומרענן
    חותמת. כך עצם השליחה הידנית מוציאה את הלקוח מהבוט, בלי ללחוץ כפתור."""
    try:
        from datetime import datetime, timezone
        prev = (db.bot_session_get(phone).get("data") or {}).get("note")
        db.bot_session_set(str(phone), "agent",
                           {"note": prev or "מענה אנושי",
                            "ts": datetime.now(timezone.utc).isoformat()})
    except Exception:  # noqa: BLE001
        pass


@app.post("/api/admin/wa/send")
def wa_send(body: WaSend, x_admin_key: Optional[str] = Header(None)):
    """מענה אנושי; אם מחוץ לחלון 24ש — מחזיר needs_template (לא שולח).
    עם reply_to — מענה מצוטט (reply אמיתי) דרך Meta ישיר.
    מענה ידני → הבוט מושתק אוטומטית לשיחה (השתלטות)."""
    _require_admin(x_admin_key)
    import wa
    _bot_handoff_on(body.phone)
    if body.reply_to and wa.meta_direct_ready():
        return _wa_guard(wa.send_reply_quoted, body.phone, body.text,
                         body.reply_to, body.reply_preview)
    return _wa_guard(wa.send_reply, body.phone, body.text)


@app.post("/api/admin/wa/test-order-confirm")
def wa_test_order_confirm(phone: str, order: str, name: str = "בדיקה",
                          x_admin_key: Optional[str] = Header(None)):
    """בדיקה חד-פעמית: שולח את template 'הזמנה התקבלה' (order_update_1) למספר נתון,
    עם כפתור סטטוס דינמי לאותו order. ללא קשר לדגל WA_SEND_ORDER_CONFIRM."""
    _require_admin(x_admin_key)
    import wa
    wamid = wa.send_order_confirm(phone, name, order)
    return {"ok": True, "wamid": wamid, "phone": phone, "order": order}


@app.post("/api/admin/wa/test-status")
def wa_test_status(phone: str, order: str, name: str = "בדיקה",
                   template: str = "order_update_distribution",
                   x_admin_key: Optional[str] = Header(None)):
    """בדיקה: שולח template עדכון-סטטוס (ברירת מחדל order_update_distribution — עם
    כפתור 'קיבלתי את ההזמנה'). ללא קשר לדגל WA_SEND_STATUS_AUTO."""
    _require_admin(x_admin_key)
    import wa
    wamid = wa.send_status_template(phone, name, order, template)
    return {"ok": True, "wamid": wamid, "phone": phone, "order": order, "template": template}


@app.post("/api/admin/wa/test-review")
def wa_test_review(phone: str, order: str, name: str = "בדיקה",
                   x_admin_key: Optional[str] = Header(None)):
    """בדיקה: שולח את template חוות-הדעת (order_delivered_review_request). ללא דגל."""
    _require_admin(x_admin_key)
    import wa
    wamid = wa.send_review_template(phone, name, order)
    return {"ok": True, "wamid": wamid, "phone": phone, "order": order}


_CUTOVER_LABELS = {"off": "מצב בדיקה (רק whitelist → native, השאר קונקטופ)",
                   "live": "🟢 CUTOVER פעיל — כל הלקוחות לבוט ה-native",
                   "halt": "🔴 חירום — כל הלקוחות חזרה לקונקטופ"}


@app.get("/api/admin/wa/cutover")
def wa_cutover_get(x_admin_key: Optional[str] = Header(None)):
    """מצב ה-cutover הנוכחי (off/live/halt) + האם הטאב מציג שיחות native."""
    _require_admin(x_admin_key)
    mode = (db.sales_state_get("wa_cutover") or "off").strip().lower()
    return {"mode": mode, "label": _CUTOVER_LABELS.get(mode, mode),
            "read_native": _wa_read_native()}


@app.post("/api/wc/order-webhook")
async def wc_order_webhook(request: Request):
    """webhook מ-WooCommerce על יצירת הזמנה → שולח 'הזמנה התקבלה' **מיידית** ללקוח
    (במקום להמתין לסריקה כל 5 דק'). מאומת בחתימת WC; מאחורי אותו דגל ודדופ של
    הסריקה, אז אין כפילות. ping של WC (בלי number) — מתעלמים."""
    raw = await request.body()
    secret = (db.sales_state_get("wc_webhook_secret") or os.getenv("WC_WEBHOOK_SECRET", "")).strip()
    if secret:
        import base64
        import hashlib
        import hmac as _hmac
        sig = request.headers.get("x-wc-webhook-signature", "")
        expected = base64.b64encode(_hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()
        if not _hmac.compare_digest(expected, sig or ""):
            raise HTTPException(401, "bad signature")
    try:
        order = json_mod.loads(raw)
    except Exception:  # noqa: BLE001
        return {"ok": True, "skip": "no-json"}
    if not isinstance(order, dict) or not order.get("number"):
        return {"ok": True, "skip": "ping-or-not-order"}
    # עדכון סטטוס מיידי — לכל שינוי סטטוס (בהפצה/מוכן לאיסוף/נק' מסירה), מגודר+דדופ
    try:
        _notify_order_status(order)
    except Exception as e:  # noqa: BLE001
        logger.warning("wc webhook status-notify failed: %s", e)
    # אישור 'הזמנה התקבלה' — רק הזמנות ששולמו (AUTO_TR_STATUSES, ברירת מחדל processing)
    paid = [s.strip() for s in os.getenv("AUTO_TR_STATUSES", "processing").split(",") if s.strip()]
    if order.get("status") in paid:
        try:
            import auto_transfer
            auto_transfer._send_order_confirm(order)   # מיידי (מאחורי דגל + דדופ)
        except Exception as e:  # noqa: BLE001
            logger.warning("wc order webhook confirm failed: %s", e)
    return {"ok": True, "order": order.get("number"), "status": order.get("status")}


@app.post("/api/admin/wa/active")
def wa_admin_active(x_admin_key: Optional[str] = Header(None)):
    """heartbeat: האדמין פעיל במערכת כרגע → push לטלפון מדוכא (כמו אפליקציה אמיתית)."""
    _require_admin(x_admin_key)
    import time as _t
    db.sales_state_set("admin_active_ts", str(_t.time()))
    return {"ok": True}


@app.post("/api/admin/wa/read-native")
def wa_read_native_set(on: int = 1, x_admin_key: Optional[str] = Header(None)):
    """מתג ידני: האם הטאב יציג שיחות מהבוט ה-native (wa_msg) במקום מקונקטופ.
    שימושי לניטור שיחות הבוט במצב בדיקה (במצב live זה אוטומטי)."""
    _require_admin(x_admin_key)
    db.sales_state_set("wa_read_native", "1" if on else "0")
    return {"ok": True, "read_native": _wa_read_native()}


@app.get("/api/admin/wa/bot-state/{phone}")
def wa_bot_state(phone: str, x_admin_key: Optional[str] = Header(None)):
    """מצב הבוט ללקוח — בעיקר אם הוא ב-handoff לנציג (הבוט שותק)."""
    _require_admin(x_admin_key)
    s = db.bot_session_get(phone)
    return {"phone": phone, "state": s.get("state"),
            "handoff": s.get("state") == "agent"}


@app.post("/api/admin/wa/bot-release")
def wa_bot_release(phone: str, x_admin_key: Optional[str] = Header(None)):
    """משחרר לקוח מ-handoff-לנציג בחזרה לבוט (מנקה את מצב 'agent' — הבוט יענה שוב)."""
    _require_admin(x_admin_key)
    db.bot_session_clear(phone)
    return {"ok": True, "phone": phone, "handoff": False}


@app.post("/api/admin/wa/bot-handoff")
def wa_bot_handoff(phone: str, on: int = 1, x_admin_key: Optional[str] = Header(None)):
    """מתג ידני: on=1 → משתיק את הבוט לשיחה (אדם משתלט); on=0 → מחזיר לבוט.
    כך אפשר להשתלט/לשחרר כל שיחה מהקונסולה, לא רק כשהלקוח לוחץ 'נציג'."""
    _require_admin(x_admin_key)
    if on:
        from datetime import datetime, timezone
        db.bot_session_set(phone, "agent",
                           {"note": "ידני (קונסולה)",
                            "ts": datetime.now(timezone.utc).isoformat()})
    else:
        db.bot_session_clear(phone)
    s = db.bot_session_get(phone)
    return {"ok": True, "phone": phone, "handoff": s.get("state") == "agent"}


@app.post("/api/admin/wa/cutover")
def wa_cutover_set(mode: str, x_admin_key: Optional[str] = Header(None)):
    """החלפת מצב ה-cutover **מיידית** (נשמר ב-DB, בלי redeploy):
    live = כל הלקוחות לבוט ה-native (ה-cutoff); halt = חירום, הכל לקונקטופ;
    off = חזרה למצב בדיקה (רק whitelist native). מתריע לטלגרם בכל החלפה."""
    _require_admin(x_admin_key)
    mode = (mode or "").strip().lower()
    if mode not in ("off", "live", "halt"):
        raise HTTPException(400, "mode must be off | live | halt")
    prev = (db.sales_state_get("wa_cutover") or "off").strip().lower()
    db.sales_state_set("wa_cutover", mode)
    try:
        _tg_admin(f"🔀 <b>WA cutover</b>: {prev} → <b>{mode}</b>\n{_CUTOVER_LABELS.get(mode, mode)}")
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "mode": mode, "prev": prev, "label": _CUTOVER_LABELS.get(mode, mode)}


@app.post("/api/admin/wa/send-template")
def wa_send_template(body: WaTemplate, x_admin_key: Optional[str] = Header(None)):
    """שליחה מחוץ לחלון: template מאושר new_message (שם, גוף בשורה אחת)."""
    _require_admin(x_admin_key)
    import wa
    return _wa_guard(wa.send_template, body.phone, body.name, body.body)


@app.get("/api/admin/wa/flow-analysis")
def wa_flow_analysis(x_admin_key: Optional[str] = Header(None)):
    """ניתוח כל ההיסטוריה — לבניית הבוט ה-native על בסיס נתונים אמיתיים."""
    _require_admin(x_admin_key)
    return JSONResponse(db.wa_flow_stats(), headers={"Cache-Control": "no-store"})


@app.get("/api/admin/wa/flow-sample")
def wa_flow_sample(limit: int = 30, x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    return JSONResponse({"threads": db.wa_sample_threads(limit=limit)},
                        headers={"Cache-Control": "no-store"})


@app.post("/api/admin/wa/archive-old")
def wa_archive_old(keep: int = 10, x_admin_key: Optional[str] = Header(None)):
    """מעביר לארכיון את כל השיחות חוץ מ-keep האחרונות של היום (שעון ישראל).
    אם יש פחות מ-keep שיחות היום — שומר את כל מה שיש היום."""
    _require_admin(x_admin_key)
    import wa
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(cfg.TZ)
    now = datetime.now(tz)
    today_start = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    convs = wa.list_conversations_native()           # ממוין לפי last_msg_ts יורד
    today = [c for c in convs if int(c.get("ts") or 0) >= today_start]
    keep_phones = [c["phone"] for c in today[:max(0, keep)]]
    res = db.wa_archive_all_except(keep_phones)
    logger.info("wa archive-old: kept %d (today), archived %d", res["kept"], res["archived"])
    return {"ok": True, "kept_phones": keep_phones, **res}


@app.post("/api/admin/wa/archive")
def wa_archive(body: WaFlag, x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    import wa
    return _wa_guard(wa.archive, body.phone, body.value)


@app.post("/api/admin/wa/human")
def wa_human(body: WaFlag, x_admin_key: Optional[str] = Header(None)):
    """מתג אנושי/בוט. ⚠️ עלול לשבור את ה-UI של ConnectOp אם פתוח במקביל."""
    _require_admin(x_admin_key)
    import wa
    return _wa_guard(wa.set_human, body.phone, body.value)


class WaTag(BaseModel):
    phone: str
    tag_id: str
    add: bool = True


class WaNote(BaseModel):
    phone: str
    text: str
    author: str = ""


@app.get("/api/admin/wa/contact/{phone}")
def wa_contact(phone: str, x_admin_key: Optional[str] = Header(None)):
    """כרטיס פונה: פרטי ConnectOp + תגיות + הזמנות אתר + הערות/מעקב שלנו."""
    _require_admin(x_admin_key)
    import wa
    return _wa_guard(wa.contact_card, phone)


@app.get("/api/admin/wa/search")
def wa_search(q: str = "", x_admin_key: Optional[str] = Header(None)):
    """חיפוש בכל אנשי הקשר (שם/טלפון/אימייל) — מעבר ל-200 השיחות האחרונות."""
    _require_admin(x_admin_key)
    import wa
    return {"conversations": _wa_guard(wa.search_conversations, q)}


@app.get("/api/admin/wa/media/{phone}")
def wa_media(phone: str, x_admin_key: Optional[str] = Header(None)):
    """גלריית מדיה מרוכזת מהשיחה (לפאנל פרטי פונה)."""
    _require_admin(x_admin_key)
    import wa
    return {"media": _wa_guard(wa.media_list, phone)}


@app.get("/api/admin/wa/tags")
def wa_tags(x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    import wa
    return {"tags": _wa_guard(wa.account_tags)}


@app.post("/api/admin/wa/tag")
def wa_tag(body: WaTag, x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    import wa
    return _wa_guard(wa.set_tag, body.phone, body.tag_id, body.add)


@app.post("/api/admin/wa/note")
def wa_note_add(body: WaNote, x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    nid = db.wa_note_add(body.phone, body.text.strip(), body.author)
    return {"ok": True, "id": nid}


@app.delete("/api/admin/wa/note/{nid}")
def wa_note_del(nid: int, x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    return {"deleted": db.wa_note_delete(nid)}


@app.post("/api/admin/wa/star")
def wa_star(body: WaFlag, x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    db.wa_star_set(body.phone, body.value)
    return {"ok": True}


# ── שליחת קובץ (📎) — מעלה ל-WP media ושולח ללקוח ──
@app.post("/api/admin/wa/send-file")
async def wa_send_file(request: Request, x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    form = await request.form()
    phone = str(form.get("phone") or "")
    caption = str(form.get("caption") or "")
    up = form.get("file")
    if not phone or up is None or isinstance(up, str):
        raise HTTPException(400, "phone + file required")
    content = await up.read()
    import wa
    _bot_handoff_on(phone)                 # שליחת קובץ ידנית → השתלטות (הבוט משתתק)
    return _wa_guard(wa.send_media, phone, up.filename or "file",
                     content, up.content_type or "", caption)


# ── תשובות מוכנות (⚡ canned replies) — מנוהלות ב-GreenOS ──
class WaCanned(BaseModel):
    title: str
    text: str


@app.get("/api/admin/wa/canned")
def wa_canned(x_admin_key: Optional[str] = Header(None)):
    """שלנו (ניתנות לעריכה) + התשובות השמורות של ConnectOp (read-only)."""
    _require_admin(x_admin_key)
    import wa
    try:
        connectop = wa.saved_replies()
    except Exception:  # noqa: BLE001
        connectop = []
    return {"items": db.wa_canned_list(), "connectop": connectop}


@app.get("/api/admin/wa/templates")
def wa_templates_list(x_admin_key: Optional[str] = Header(None)):
    """תבניות WhatsApp מאושרות-מטא (לשליחה מחוץ לחלון 24ש)."""
    _require_admin(x_admin_key)
    import wa
    return {"templates": _wa_guard(wa.wa_templates)}


class WaTplSend(BaseModel):
    phone: str
    template_id: str
    params: list = []


@app.post("/api/admin/wa/send-wa-template")
def wa_send_wa_template(body: WaTplSend, x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    import wa
    return _wa_guard(wa.send_wa_template, body.phone, body.template_id, body.params)


@app.post("/api/admin/wa/canned")
def wa_canned_add(body: WaCanned, x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    db.wa_canned_add(body.title.strip(), body.text.strip())
    return {"items": db.wa_canned_list()}


@app.delete("/api/admin/wa/canned/{cid}")
def wa_canned_del(cid: int, x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    return {"deleted": db.wa_canned_delete(cid)}


# ── ✨ אורי בתוך הוואטסאפ — תור משימות לגשר על המק של אסי (חיוב Max, לא API) ──
URI_BRIDGE_KEY = os.getenv("URI_BRIDGE_KEY", "")


def _require_bridge(x_bridge_key: Optional[str]):
    if not URI_BRIDGE_KEY or (x_bridge_key or "") != URI_BRIDGE_KEY:
        raise HTTPException(401, "bridge key required")


class UriAsk(BaseModel):
    phone: str
    question: str


class UriAnswer(BaseModel):
    id: int
    answer: str
    status: str = "done"


@app.post("/api/admin/wa/uri/ask")
def uri_ask(body: UriAsk, x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    q = body.question.strip()
    if not q:
        raise HTTPException(400, "empty question")
    jid = db.uri_job_add(body.phone, q)
    return {"id": jid}


@app.get("/api/admin/wa/uri/job/{jid}")
def uri_job(jid: int, x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    j = db.uri_job_get(jid)
    if not j:
        raise HTTPException(404, "job not found")
    return {"status": j["status"], "answer": j.get("answer")}


@app.get("/api/admin/wa/uri/history")
def uri_history(phone: str = "", x_admin_key: Optional[str] = Header(None)):
    """היסטוריית השיחה עם אורי לשיחה (טלפון) — להמשכיות בין מכשירים.
    מחזיר רשימת jobs שנענו (question+answer+status) מהישן לחדש; הצד-לקוח
    מפענח [DRAFT] כמו ב-poll הרגיל ובונה את חוט השיחה."""
    _require_admin(x_admin_key)
    rows = db.uri_history(phone)
    return JSONResponse({"jobs": rows}, headers={"Cache-Control": "no-store"})


@app.get("/api/admin/wa/uri/status")
def uri_status(x_admin_key: Optional[str] = Header(None)):
    """האם הגשר על המק חי (heartbeat מ-2 הדקות האחרונות)."""
    _require_admin(x_admin_key)
    last = db.sales_state_get("uri_bridge_ping")
    alive = False
    if last:
        try:
            t = datetime.fromisoformat(str(last))
            alive = (datetime.now(t.tzinfo) - t).total_seconds() < 120
        except Exception:  # noqa: BLE001
            pass
    return {"alive": alive, "last_ping": last}


@app.get("/api/uri-bridge/jobs")
def bridge_jobs(x_bridge_key: Optional[str] = Header(None)):
    _require_bridge(x_bridge_key)
    db.sales_state_set("uri_bridge_ping", db.now_iso())
    db.uri_jobs_requeue_stuck()
    return {"jobs": db.uri_jobs_pending()}


@app.get("/api/uri-bridge/thread/{phone}")
def bridge_thread(phone: str, limit: int = 8, x_bridge_key: Optional[str] = Header(None)):
    """הקשר השיחה מהחנות הנייטיב (wa_msg) — כולל את הודעות הבוט/אורי עצמן (שנשלחות
    דרך מטא ולכן חסרות בקונקטופ). זה מה שנותן לאורי המשכיות לשאלות שהוא עצמו שאל."""
    _require_bridge(x_bridge_key)
    import wa
    try:
        msgs = (wa.get_thread_native(phone, limit=max(2, min(int(limit or 8), 20)))
                or {}).get("messages", [])
    except Exception:  # noqa: BLE001
        msgs = []
    lines = []
    for m in (msgs or [])[-int(limit or 8):]:
        t = (m.get("text") or "").strip()
        if not t:
            continue
        who = "לקוח" if m.get("direction") == "in" else "אורי (אנחנו)"
        lines.append(f"{who}: {t}")
    return {"thread": "\n".join(lines)}


@app.get("/api/uri-bridge/search")
def bridge_search(q: str = "", limit: int = 8, x_bridge_key: Optional[str] = Header(None)):
    """חיפוש מוצרים לאורי (מסלול הבוט המהיר) — כך שלא יהיה תלוי רק במועמדים שהוזרקו.
    מחזיר שם/מחיר/מלאי/קישור, מדורג כמו מנוע החיפוש של הבוט."""
    _require_bridge(x_bridge_key)
    sr = bot_smart_search(q or "", limit=max(1, min(int(limit or 8), 12)))  # אותו מוח כמו הבוט
    res = sr.get("results") or []
    return {"results": [{"id": r.get("id"), "name": r.get("name"),
                         "price_from": r.get("price"),   # מחיר בסיס; לוריאציות ראה /variations
                         "type": r.get("type"),
                         "in_stock": r.get("stock_status") == "instock",
                         "url": r.get("permalink")} for r in res],
            "meta": sr.get("meta") or {}}


@app.get("/api/uri-bridge/variations/{product_id}")
def bridge_variations(product_id: int, x_bridge_key: Optional[str] = Header(None)):
    """וריאציות מוצר לאורי (נפח/צבע/מחיר/מלאי) — לשאלות ספציפיות כמו '256GB עד 700'.
    חשוב: מחיר משתנה בין וריאציות; אסור לקבוע 'אין' לפי מחיר הבסיס בלבד."""
    _require_bridge(x_bridge_key)
    vs = bot_get_variations(product_id) or []
    return {"variations": [{"storage": v.get("storage"), "color": v.get("color"),
                            "price": v.get("price"),
                            "in_stock": v.get("stock") == "instock",
                            "url": v.get("permalink") or ""}   # קישור ישיר לוריאציה (slug צבע)
                           for v in vs]}


_ATTR_TERMS_CACHE = {"at": 0.0, "data": None}
_FILTER_GENERIC = {"אוזניות", "טלפון", "סלולרי", "סלולארי", "מסך", "סוללה", "מצלמה",
                   "סמארטפון", "מכשיר", "עם", "תוך", "אוזן", "שעון", "חכם", "אלחוטיות",
                   "אלחוטי", "כבל", "מטען", "wireless", "phone", "screen", "battery"}


def _wc_attr_terms():
    """כל ערכי-התכונות (terms) מכל ה-attributes, cache שעה — לסינון לפי תכונה."""
    import time as _t
    if _ATTR_TERMS_CACHE["data"] is not None and (_t.time() - _ATTR_TERMS_CACHE["at"] < 3600):
        return _ATTR_TERMS_CACHE["data"]
    base, k, s = _wc_creds()
    import requests as _rq
    out = []
    try:
        ra = _rq.get(f"{base}/wp-json/wc/v3/products/attributes",
                     params={"per_page": 100}, auth=(k, s), timeout=25)
        for a in (ra.json() if ra.ok else []):
            rt = _rq.get(f"{base}/wp-json/wc/v3/products/attributes/{a['id']}/terms",
                         params={"per_page": 100}, auth=(k, s), timeout=20)
            for t in (rt.json() if rt.ok else []):
                if (t.get("count") or 0) > 0:
                    out.append({"attr": a.get("slug"), "id": t.get("id"),
                                "name": t.get("name") or "", "slug": t.get("slug") or ""})
        _ATTR_TERMS_CACHE.update(at=_t.time(), data=out)
    except Exception as e:  # noqa: BLE001
        logger.warning("attr terms fetch failed: %s", e)
    return out


@app.get("/api/uri-bridge/filter")
def bridge_filter(q: str = "", max_price: int = 0, min_price: int = 0,
                  x_bridge_key: Optional[str] = Header(None)):
    """סינון מוצרים לפי **תכונה + מחיר** (לא רק מילה בשם). מזהה את התכונה מהשאלה
    (ANC/'ביטול רעשים'/RGB...) מול ערכי-התכונות של WC, ומחזיר את כל המתאימים בטווח
    המחיר. לשאלות 'X עם תכונה Y עד Z₪' — זה הכלי הנכון, לא search."""
    _require_bridge(x_bridge_key)
    import re as _re2
    base, k, s = _wc_creds()
    import requests as _rq
    ql = (q or "").lower()
    if not max_price:
        mm = _re2.search(r"(?:עד|מתחת\s*ל-?|under|below|<)\s*[₪]?\s*(\d{2,5})", ql)
        if mm:
            max_price = int(mm.group(1))
    qwords = {w for w in _re2.split(r"[\s/,\-]+", ql) if len(w) >= 3 and w not in _FILTER_GENERIC}
    best, best_score = None, 0
    for t in _wc_attr_terms():
        nwords = {w for w in _re2.split(r"[\s/,\-]+", (t["name"] or "").lower())
                  if len(w) >= 3 and w not in _FILTER_GENERIC}
        score = len(qwords & nwords)
        if t["slug"] and t["slug"].lower() in qwords:
            score += 2                       # התאמת slug אנגלי (anc)
        if score > best_score:
            best, best_score = t, score
    params = {"per_page": 40, "status": "publish", "orderby": "price", "order": "asc"}
    if max_price:
        params["max_price"] = max_price
    if min_price:
        params["min_price"] = min_price
    if best and best_score > 0:
        params["attribute"], params["attribute_term"] = best["attr"], best["id"]
    try:
        r = _rq.get(f"{base}/wp-json/wc/v3/products", params=params, auth=(k, s), timeout=30)
        res = [p for p in (r.json() if r.ok else [])
               if p.get("catalog_visibility") != "hidden" and p.get("type") not in ("external", "grouped")]
    except Exception:  # noqa: BLE001
        res = []
    return {"feature": (best["name"] if best and best_score > 0 else None),
            "max_price": max_price or None, "count": len(res),
            "results": [{"name": p.get("name"), "price": p.get("price"),
                         "in_stock": p.get("stock_status") == "instock",
                         "url": p.get("permalink")} for p in res[:25]]}


@app.post("/api/uri-bridge/answer")
def bridge_answer(body: UriAnswer, x_bridge_key: Optional[str] = Header(None)):
    _require_bridge(x_bridge_key)
    db.uri_job_answer(body.id, body.answer, body.status)
    # משימת בוט (source='bot') → שולחים את תשובת אורי אוטומטית ללקוח בוואטסאפ
    try:
        job = db.uri_job_get(body.id)
        if job and job.get("source") == "bot" and body.status == "done":
            import re as _re
            ans = body.answer or ""
            m = _re.search(r"\[DRAFT\]\s*([\s\S]*?)\s*\[/DRAFT\]", ans)
            ans = (m.group(1) if m else _re.sub(r"\[/?DRAFT\]", "", ans)).strip()
            # למידה: שורות [NOTE] נשמרות בכרטיס הלקוח (ℹ️) ו**מוסרות** לפני השליחה ללקוח
            _notes = _re.findall(r"\[NOTE\]\s*(.+)", ans)
            if _notes:
                ans = _re.sub(r"\s*\[NOTE\].*", "", ans).strip()
                for _n in _notes[:2]:
                    try:
                        db.wa_note_add(job["phone"], _n.strip()[:400], "אורי ✨")
                    except Exception:  # noqa: BLE001
                        pass
            # [HANDOFF]: אורי מבקש להעביר לנציג אמיתי → מסירים את הסימן, ואחרי השליחה
            # מפעילים העברה אמיתית (משתיק את הבוט + מתריע לצוות) — לא רק "מילים".
            _handoff = "[HANDOFF]" in ans
            if _handoff:
                ans = ans.replace("[HANDOFF]", "").strip()
            # 🔗 / טקסט צמוד ל-URL בהקשר RTL שובר את הלחיצוּת בוואטסאפ — מנקים:
            # מסירים 🔗 שלפני קישור, ומכריחים כל URL לשורה נקייה משלו.
            ans = _re.sub(r"[ \t]*🔗️?[ \t]*(?=https?://)", "", ans)
            ans = _re.sub(r"(?<=\S)[ \t]+(https?://\S+)", r"\n\1", ans)
            # קיצור קישורי greenmobile עם slug עברי → gm- (כלל: עברי מקצרים; אנגלי נשאר)
            try:
                import wa_bot as _wb

                def _shorten(_m):
                    u, tail = _m.group(0), ""
                    while u and u[-1] in ".,;:!?)]⁩":
                        tail, u = u[-1] + tail, u[:-1]
                    return _wb._short_link(u) + tail
                ans = _re.sub(r"https?://greenmobile\.co\.il/\S+", _shorten, ans)
            except Exception:  # noqa: BLE001
                pass
            if ans:
                import wa
                wa.send_text(job["phone"], ans)
                logger.info("uri bot-answer sent to %s (job %s)", job["phone"], body.id)
            if _handoff:                       # העברה אמיתית: משתיק את הבוט + מתריע
                try:
                    from datetime import datetime as _dt, timezone as _tz
                    db.bot_session_set(job["phone"], "agent",
                                       {"note": "אורי העביר לנציג",
                                        "ts": _dt.now(_tz.utc).isoformat()})
                    _tg_admin(f"👤 <b>אורי העביר לנציג</b>\n{job['phone']}"
                              f"\n🤖 הבוט הושתק לשיחה הזו — טפל/י ידנית בקונסולה.")
                    logger.info("uri HANDOFF → agent for %s (job %s)", job["phone"], body.id)
                except Exception as e:  # noqa: BLE001
                    logger.warning("uri handoff trigger failed (job %s): %s", body.id, e)
    except Exception as e:  # noqa: BLE001
        logger.warning("uri bot auto-send failed (job %s): %s", body.id, e)
    return {"ok": True}


@app.get("/api/uri-bridge/context/{phone}")
def bridge_context(phone: str, x_bridge_key: Optional[str] = Header(None)):
    """הקשר מוכן לאורי: הערות GreenOS (הזיכרון המצטבר על הלקוח) + הזמנות אתר —
    מוזרק ל-prompt כדי לחסוך סבבי בדיקות (מהירות) ולשמר למידה."""
    _require_bridge(x_bridge_key)
    import wa
    try:
        orders = wa._wc_orders_by_phone(phone) or []
    except Exception:  # noqa: BLE001
        orders = []
    return {"notes": db.wa_notes_list(phone), "orders": orders,
            "star": phone in db.wa_stars()}


@app.get("/api/uri-bridge/order")
def bridge_order(phone: str = "", number: str = "", name: str = "",
                 x_bridge_key: Optional[str] = Header(None)):
    """בדיקת הזמנה לאורי (read-only): לפי טלפון / מספר הזמנה / שם לקוח. מחזיר
    status/items/total/date — כדי שאורי יענה מנתונים אמיתיים ולא ינחש סטטוס/תאריך."""
    _require_bridge(x_bridge_key)
    import wa
    if phone and not number and not name:
        return {"orders": wa._wc_orders_by_phone(phone) or []}
    import os as _os
    import requests as _rq
    base = _os.getenv("WC_STORE_URL", "").rstrip("/")
    auth = (_os.getenv("WC_CONSUMER_KEY", ""), _os.getenv("WC_CONSUMER_SECRET", ""))
    term = (number or name).strip()
    if not base or not auth[0] or not term:
        return {"orders": []}
    try:
        r = _rq.get(f"{base}/wp-json/wc/v3/orders",
                    params={"search": term, "per_page": 5}, auth=auth, timeout=25)
        rows = r.json() if r.ok else []
        if number:  # התאמה מדויקת למספר ההזמנה כשאפשר
            exact = [o for o in rows if str(o.get("number")) == str(number)
                     or str(o.get("id")) == str(number)]
            rows = exact or rows
        out = [{"id": o.get("id"), "number": o.get("number"), "status": o.get("status"),
                "total": o.get("total"), "currency": o.get("currency_symbol") or "₪",
                "date": (o.get("date_created") or "")[:16].replace("T", " "),
                "items": [i.get("name") for i in (o.get("line_items") or [])][:4],
                "customer": " ".join(filter(None, [(o.get("billing") or {}).get("first_name"),
                                                   (o.get("billing") or {}).get("last_name")]))}
               for o in rows[:5]]
        return {"orders": out}
    except Exception:  # noqa: BLE001
        return {"orders": []}


class BridgeNote(BaseModel):
    phone: str
    text: str


@app.post("/api/uri-bridge/note")
def bridge_note(body: BridgeNote, x_bridge_key: Optional[str] = Header(None)):
    """אורי לומד: שומר תובנה על הלקוח כהערה ב-GreenOS (מופיעה בפאנל ℹ️)."""
    _require_bridge(x_bridge_key)
    nid = db.wa_note_add(body.phone, body.text.strip()[:400], "אורי ✨")
    return {"ok": True, "id": nid}


# ── 🛒 הזמנה חיה מתוך הוואטסאפ: WooCommerce order + קישור תשלום PayPlus ──
PAYPLUS_BASE = "https://restapi.payplus.co.il/api/v1.0"
_PP_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")


def _payplus_headers():
    return {"api-key": os.getenv("PAYPLUS_API_KEY", ""),
            "secret-key": os.getenv("PAYPLUS_SECRET_KEY", ""),
            "Content-Type": "application/json", "User-Agent": _PP_UA}


def _payplus_link(amount: float, order_number: str, customer: dict, payments: int = 1) -> dict:
    """יוצר קישור דף תשלום PayPlus עבור ההזמנה. דורש env PAYPLUS_PAGE_UID.
    מחזיר {"link":..., "pru": page_request_uid} — ה-pru משמש לאימות IPN ולבדיקת סטטוס."""
    import requests as _rq
    page_uid = os.getenv("PAYPLUS_PAGE_UID", "").strip()
    if not (page_uid and os.getenv("PAYPLUS_API_KEY")):
        raise HTTPException(400, "PayPlus עוד לא מוגדר (PAYPLUS_PAGE_UID חסר)")
    base_url = (cfg.APP_BASE_URL or "https://gm-transfers.onrender.com").rstrip("/")
    body = {
        "payment_page_uid": page_uid,
        "charge_method": 1,                      # חיוב מיידי
        "amount": round(float(amount), 2),
        "currency_code": "ILS",
        "sendEmailApproval": True,
        "send_failure_callback": False,
        "more_info": f"GreenOS order {order_number}",
        "more_info_2": "GreenOS",
        # אכיפת 3D Secure על כל עסקה (קישור/חיוב טלפוני) — הוראת אסי 12/06.
        # הערה: גם כשה-3DS רץ, המנפיק רשאי לאשר frictionless (בלי קוד SMS) בסכומים קטנים.
        "secure3d": {"activate": True},
        "payments": max(1, int(payments or 1)),
        "payments_selected": max(1, int(payments or 1)),   # התשלומים שנבחרו בטופס — מסומנים מראש בדף
        # חזרה אלינו בסיום (נטען בתוך iframe ב-GreenOS) + callback שרת-לשרת
        "refURL_success": f"{base_url}/pay-done?ok=1",
        "refURL_failure": f"{base_url}/pay-done?ok=0",
        "refURL_cancel": f"{base_url}/pay-done?ok=0",
        "refURL_callback": f"{base_url}/api/payplus/ipn",
        "customer": {
            "customer_name": (customer.get("name") or "").strip()[:60] or "לקוח",
            "email": customer.get("email") or "",
            "phone": customer.get("phone") or "",
        },
    }
    r = _rq.post(f"{PAYPLUS_BASE}/PaymentPages/generateLink",
                 headers=_payplus_headers(), json=body, timeout=40)
    try:
        j = r.json()
    except Exception:  # noqa: BLE001
        j = {}
    data = j.get("data") or {}
    link = data.get("payment_page_link") or data.get("link") or ""
    if r.status_code != 200 or not link:
        logger.warning("payplus link failed %s: %s", r.status_code, r.text[:300])
        raise HTTPException(502, f"PayPlus דחה את הבקשה ({r.status_code}): {str(j)[:150]}")
    pru = data.get("page_request_uid") or link.rstrip("/").rsplit("/", 1)[-1]
    # utm_source — כך עמודת "מקור" בדשבורד PayPlus תציג GreenOS במקום "לא ידוע"
    link += ("&" if "?" in link else "?") + "utm_source=GreenOS&utm_medium=greenos"
    return {"link": link, "pru": pru}


def _payplus_ipn_check(pru: str) -> Optional[dict]:
    """אימות שרת-לשרת מול PayPlus: מה מצב הבקשה pru? מחזיר את ה-data אם שולם בהצלחה."""
    import requests as _rq
    if not pru:
        return None
    try:
        r = _rq.post(f"{PAYPLUS_BASE}/PaymentPages/ipn",
                     headers=_payplus_headers(),
                     json={"payment_request_uid": pru, "related_transaction": False},
                     timeout=30)
        j = r.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("payplus ipn check error: %s", e)
        return None
    res = j.get("results") or {}
    data = j.get("data") or {}
    if r.status_code == 200 and res.get("status") == "success" and str(
            data.get("status_code") or "") in ("000", "0"):
        return data
    return None


def _pp_tx_fields(d: dict) -> dict:
    """מנרמל פרטי עסקה משני המבנים של PayPlus (callback שטוח / IPN-API מקונן)."""
    tx = d.get("transaction") or {}
    card = ((d.get("data") or {}).get("card_information")
            or d.get("card_information") or {})
    g = lambda *keys: next((str(src.get(k)) for src in (d, tx, card)
                            for k in keys if src.get(k) not in (None, "")), "")
    return {
        "tx": g("transaction_uid", "uid"),
        "number": g("transaction_number", "number"),
        "approval": g("approval_num", "approval_number"),
        "voucher": g("voucher_num", "voucher_number"),
        "four_digits": g("four_digits"),
        "brand": g("brand_name"),
        "payments": g("number_of_payments", "payments"),
        "amount": g("amount"),
        "date": g("date", "transaction_date"),
        "method": g("method", "payment_method"),
        "status_code": g("status_code"),
        "pru": g("page_request_uid", "payment_page_request_uid", "payment_request_uid"),
        "more_info": g("more_info"),
    }


def _pp_mark_paid(order_id: int, f: dict) -> bool:
    """מעדכן הזמנת WC לשולם: סטטוס 'בטיפול' + פרטי העסקה בשדות meta. אידמפוטנטי."""
    import requests as _rq
    creds = _wc_creds()
    if not creds:
        return False
    base, k, s = creds
    try:
        cur = _rq.get(f"{base}/wp-json/wc/v3/orders/{order_id}", auth=(k, s), timeout=30)
        if not cur.ok:
            return False
        o = cur.json()
        already = any(m.get("key") == "greenos_payplus_tx" and m.get("value")
                      for m in (o.get("meta_data") or []))
        if already and o.get("status") in ("processing", "completed"):
            return True  # כבר עודכן (IPN כפול / poll מקביל)
        meta = [{"key": f"greenos_payplus_{mk}", "value": str(f.get(fk) or "")}
                for mk, fk in (("tx", "tx"), ("approval", "approval"),
                               ("voucher", "voucher"), ("4digits", "four_digits"),
                               ("brand", "brand"), ("payments", "payments"),
                               ("amount", "amount"), ("date", "date"))]
        r = _rq.put(f"{base}/wp-json/wc/v3/orders/{order_id}",
                    json={"status": "processing", "set_paid": True,
                          "transaction_id": f.get("tx") or "",
                          "payment_method": "payplus",
                          "payment_method_title": "כרטיס אשראי (PayPlus)",
                          "meta_data": meta},
                    auth=(k, s), timeout=30)
        if r.ok:
            try:  # הערה פנימית על ההזמנה — נראית למוקדנים ב-WC
                _rq.post(f"{base}/wp-json/wc/v3/orders/{order_id}/notes",
                         json={"note": (f"GreenOS · PayPlus: התשלום אושר ✓ "
                                        f"עסקה {f.get('tx') or '?'} · אישור {f.get('approval') or '?'} · "
                                        f"{f.get('brand') or ''} ****{f.get('four_digits') or ''} · "
                                        f"{f.get('payments') or 1} תשלומים")},
                         auth=(k, s), timeout=20)
            except Exception:  # noqa: BLE001
                pass
            logger.info("order %s marked paid (tx %s)", order_id, f.get("tx"))
            try:  # שולם → סריקת העברה-לאתר מיידית, בלי לחכות לג'וב של 5 הדק'
                scheduler.add_job(_auto_transfer_job, "date",
                                  id=f"auto_tr_now_{order_id}", replace_existing=True)
            except Exception:  # noqa: BLE001
                pass
            return True
        logger.warning("order paid-update failed %s: %s", r.status_code, r.text[:200])
    except Exception as e:  # noqa: BLE001
        logger.warning("mark-paid error for order %s: %s", order_id, e)
    return False


@app.post("/api/payplus/ipn")
async def payplus_ipn(request: Request):
    """Callback שרת-לשרת מ-PayPlus בסיום תשלום (refURL_callback).
    מאומת מול ה-IPN API של PayPlus לפני עדכון ההזמנה — לא סומכים על גוף הבקשה לבדו."""
    import json as _json
    try:
        raw = await request.body()
        try:
            payload = _json.loads(raw.decode("utf-8", "ignore") or "{}")
        except Exception:  # noqa: BLE001
            payload = dict((await request.form()) or {})
    except Exception:  # noqa: BLE001
        payload = {}
    if not payload:
        payload = dict(request.query_params)
    f = _pp_tx_fields(payload)
    pru = f.get("pru") or ""
    logger.info("payplus ipn: pru=%s status=%s tx=%s", pru, f.get("status_code"), f.get("tx"))
    # אימות אמיתי מול PayPlus (לא מסתמכים על payload שכל אחד יכול לשלוח)
    verified = _payplus_ipn_check(pru)
    if not verified:
        logger.warning("payplus ipn NOT verified (pru=%s) — ignoring", pru)
        return {"ok": False, "verified": False}
    vf = _pp_tx_fields(verified)
    for k2, v in vf.items():  # ערכים מהאימות גוברים על ה-payload
        if v:
            f[k2] = v
    order_id = db.sales_state_get(f"payplus_pru:{pru}")
    if not order_id:
        logger.warning("payplus ipn: no order mapping for pru %s (more_info=%s)", pru, f.get("more_info"))
        return {"ok": False, "order": None}
    if not str(order_id).isdigit():   # קישור כללי (standalone) — אין הזמנת WC לעדכן
        db.pay_link_mark_paid(pru, f.get("tx"), f.get("approval"), f.get("four_digits"), f.get("brand"))
        logger.info("payplus ipn: standalone payment confirmed (pru %s, tx %s)", pru, f.get("tx"))
        return {"ok": True, "order": None, "standalone": True}
    ok = _pp_mark_paid(int(order_id), f)
    return {"ok": ok, "order": int(order_id)}


# ── טאב הזמנות: חלון מלא להזמנות WooCommerce (קריאה/עריכה דרך REST של האתר) ──
# הנתונים יושבים ב-WC בלבד — אנחנו פרוקסי דק, עמוד-עמוד, בלי אחסון אצלנו.

@app.get("/api/admin/orders/latest")
def admin_orders_latest(x_admin_key: Optional[str] = Header(None)):
    """הזמנות אחרונות (קלות משקל) — להתראת 'הזמנה חדשה' ולספירת באדג'.
    רק סטטוסי כניסה אמיתיים (processing/pending/on-hold) — לא טיוטות checkout-draft."""
    _require_admin(x_admin_key)
    import requests as _rq
    creds = _wc_creds()
    if not creds:
        return {"orders": []}
    base, k, s = creds
    try:
        r = _rq.get(f"{base}/wp-json/wc/v3/orders",
                    params={"per_page": 8, "orderby": "date", "order": "desc",
                            # רק processing = שולם בפועל; ממתין-לתשלום לא מתריע (הוראת אסי 13/06)
                            "status": "processing"},
                    auth=(k, s), timeout=30)
        if not r.ok:
            return {"orders": []}
    except Exception:  # noqa: BLE001
        return {"orders": []}
    out = []
    for o in r.json():
        items = o.get("line_items") or []
        nm = (items[0].get("name") or "") if items else ""
        out.append({"id": o.get("id"), "number": o.get("number"),
                    "item": nm[:50],
                    "items_n": sum(int(li.get("quantity") or 1) for li in items),
                    "total": o.get("total"), "status": o.get("status"),
                    "date": o.get("date_created")})
    # ⚠️ no-store — אסור לקאש (אחרת זיהוי "הזמנה חדשה" מקבל תגובה ישנה מה-edge)
    return JSONResponse({"orders": out}, headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/api/admin/orders")
def admin_orders_list(page: int = 1, status: str = "", search: str = "",
                      after: str = "", before: str = "", express: int = 0,
                      x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    import requests as _rq
    creds = _wc_creds()
    if not creds:
        raise HTTPException(502, "חיבור WooCommerce לא מוגדר")
    base, k, s = creds
    params = {"per_page": 25, "page": max(1, page), "orderby": "date", "order": "desc"}
    if express:               # אקספרס יכול להיות בכל סטטוס → מושכים 100 אחרונות ומסננים בהמשך
        params["per_page"] = 100
    elif status.strip():
        params["status"] = status.strip()
    if after.strip():      # טווח תאריכים מהיומן — YYYY-MM-DD
        params["after"] = f"{after.strip()}T00:00:00"
    if before.strip():
        params["before"] = f"{before.strip()}T23:59:59"
    if search.strip():
        q = search.strip()
        # טלפון — מחפשים לפי הליבה בלי קידומת (תופס 05X וגם 972X)
        digits = "".join(ch for ch in q if ch.isdigit())
        if digits and len(digits) >= 7 and len(digits) >= len(q) - 3:
            import re as _re
            q = _re.sub(r"^(?:972|0)", "", digits)
        params["search"] = q
    r = _rq.get(f"{base}/wp-json/wc/v3/orders", params=params, auth=(k, s), timeout=45)
    if not r.ok:
        raise HTTPException(502, f"קריאת הזמנות נכשלה ({r.status_code})")
    # אילו הזמנות עם בקשת העברה משודרת (אוטו) — לפי created_by בתוכנית ההעברות.
    # מצרפים את **כל** הסניפים+כמויות (הזמנה רב-יחידתית מתפצלת בין סניפים).
    import re as _re
    bcast_map = _build_bcast_map(db.plan_list())
    # הזמנות שסומנו 'חסר בכל הסניפים' (auto_transfer) — להצגת אייקון OOS
    oos_set = set()
    partial_set = set()
    unmatched_set = set()
    return_set = set()
    try:
        import json as _json
        raw = db.sales_state_get("order_oos_list")
        for x in (_json.loads(raw) if raw else []):
            oos_set.add(str(x.get("number")))
            if x.get("partial"):
                partial_set.add(str(x.get("number")))
        rawu = db.sales_state_get("order_unmatched_list")
        for x in (_json.loads(rawu) if rawu else []):
            unmatched_set.add(str(x.get("number")))
        rawr = db.sales_state_get("order_return_list")
        for x in (_json.loads(rawr) if rawr else []):
            return_set.add(str(x.get("number")))
    except Exception:  # noqa: BLE001
        pass
    # מוצרים דיגיטליים (גיפט קארד/קוד, is_stock=False) לא יציגו OOS — סינון בתצוגה
    # (גם מנקה סימונים ישנים שנוצרו לפני התיקון). מפת is_stock מהקטלוג המקומי.
    _cat = db.catalog_load() if oos_set else {}
    out = []
    for o in r.json():
        meta = {m.get("key"): m.get("value") for m in (o.get("meta_data") or [])}
        items = o.get("line_items") or []
        out.append({
            "src": _order_source(meta),
            "ship_tag": _ship_tag(o, meta),
            "img": ((items[0].get("image") or {}).get("src") or "") if items else "",
            "bcast": bcast_map.get(str(o.get("number"))),
            "oos": (str(o.get("number")) in oos_set) and any(
                (_cat.get(str(li.get("sku") or ""), {}).get("is_stock", True))
                for li in items if str(li.get("sku") or "") in _cat),
            "nosku": str(o.get("number")) in unmatched_set,   # פריט פיזי ללא מק"ט — טיפול ידני
            "partial": str(o.get("number")) in partial_set,   # חלק שודר וחלק חסר — שודר חלקי
            "return_open": str(o.get("number")) in return_set,   # נפתחה החזרה (איסוף מהלקוח)
            "id": o.get("id"), "number": o.get("number"), "status": o.get("status"),
            "date": o.get("date_created"), "total": o.get("total"),
            "currency": o.get("currency_symbol") or "₪",
            "name": f"{o['billing'].get('first_name','')} {o['billing'].get('last_name','')}".strip(),
            "phone": o["billing"].get("phone") or "",
            "city": o["billing"].get("city") or "",
            "items_n": sum(int(li.get("quantity") or 1) for li in items),
            "items": ", ".join((li.get("name") or "")[:100] for li in items[:3]),
            "payment": o.get("payment_method_title") or "",
            "shipping": ", ".join((sl.get("method_title") or "") for sl in (o.get("shipping_lines") or [])),
            "greenos": bool(meta.get("greenos_source")),
            "cargo": bool(meta.get("cslfw_shipping")),
            "cargo_status": _cargo_status(meta, (o.get("billing") or {}).get("email") or ""),
        })
    if express:               # רק הזמנות אקספרס (מכל הסטטוסים, מתוך 100 האחרונות)
        out = [o for o in out if (o.get("ship_tag") or "").startswith("express")]
    # no-store — אסור לקאש ב-edge: אחרת סימוני שודר/חסר/חלקי מתעדכנים באיחור
    # (אותו לקח כמו /orders/latest — שורת שידור חדשה לא נראתה אחרי רענונים)
    return JSONResponse({"orders": out, "page": page,
                         "pages": 1 if express else int(r.headers.get("X-WP-TotalPages") or 1),
                         "total": len(out) if express else int(r.headers.get("X-WP-Total") or len(out)),
                         "statuses": _wc_statuses(base, k, s)},
                        headers={"Cache-Control": "no-store, max-age=0"})


_wc_statuses_cache = {"at": 0.0, "map": {}}


def _wc_statuses(base, k, s) -> dict:
    """מפת הסטטוסים האמיתית של האתר (כולל מותאמים: shipping-stage וכו'), cache שעה."""
    import time as _t
    import requests as _rq
    if _wc_statuses_cache["map"] and _t.time() - _wc_statuses_cache["at"] < 3600:
        return _wc_statuses_cache["map"]
    try:
        r = _rq.get(f"{base}/wp-json/wc/v3/reports/orders/totals", auth=(k, s), timeout=30)
        if r.ok:
            _wc_statuses_cache["map"] = {st["slug"]: st["name"] for st in r.json()}
            _wc_statuses_cache["at"] = _t.time()
    except Exception as e:  # noqa: BLE001
        logger.warning("statuses fetch failed: %s", e)
    return _wc_statuses_cache["map"]


def _build_bcast_map(plan_lines):
    """מס׳ הזמנה → מצב שידור מצרפי לכל הסניפים+כמויות (פיצול רב-יחידתי בין סניפים).
    {onum: {status: live/closed, branch: <ראשון — תאימות>, branches: [{name,qty,status}]}}"""
    import re as _re
    agg = {}
    for ln in (plan_lines or []):
        m2 = _re.search(r"הזמנת אתר #(\d+)", ln.get("created_by") or "")
        if not m2:
            continue
        onum = m2.group(1)
        bname = cfg.branch_name(ln.get("from_branch"))
        live = int(ln.get("bcast") or 0) == 1
        be = agg.setdefault(onum, {}).setdefault(bname, {"qty": 0, "live": False})
        be["qty"] += int(ln.get("qty") or 1)
        if live:
            be["live"] = True
    out = {}
    for onum, branches in agg.items():
        blist = [{"name": bn, "qty": v["qty"], "status": "live" if v["live"] else "closed"}
                 for bn, v in branches.items()]
        blist.sort(key=lambda b: 0 if b["status"] == "live" else 1)
        out[onum] = {"status": "live" if any(b["status"] == "live" for b in blist) else "closed",
                     "branch": blist[0]["name"] if blist else "", "branches": blist}
    return out


def _cargo_status(meta: dict, email: str = ""):
    """תג סטטוס המשלוח מ-Cargo (meta cslfw_shipping): {tracking, text, num, line,
    track_url}. כמה משלוחים → האחרון לפי created_at. None אם אין משלוח.
    track_url = דף מעקב Cargo ללקוח (trackingId + אימייל הלקוח), להעתקה/שליחה."""
    cs = (meta or {}).get("cslfw_shipping")
    if not isinstance(cs, dict) or not cs:
        return None
    items = [(sid, sh) for sid, sh in cs.items() if isinstance(sh, dict)]
    if not items:
        return None
    items.sort(key=lambda x: str(x[1].get("created_at") or ""))
    sid, sh = items[-1]
    st = sh.get("status") or {}
    from urllib.parse import quote
    track_url = f"https://dashboard.cargo.co.il/tracking-page?trackingId={quote(str(sid))}"
    if email:
        track_url += f"&customerField={quote(str(email))}"
    return {"tracking": str(sid), "text": str(st.get("text") or "").strip(),
            "num": st.get("number"), "line": str(sh.get("line_number") or "").strip(),
            "driver": str(sh.get("driver_name") or "").strip(),
            "track_url": track_url}


def _ship_tag(o: dict, meta: dict = None) -> str:
    """תג עדין לתצוגה חיצונית — רק כשזה מעניין: אקספרס / נק׳ מסירה ת״א / איסוף
    (כולל הסניף שנבחר, מהסניפט שלנו ב-meta ‏_gm_pickup_branch). משלוח רגיל = בלי תג."""
    meta = meta or {}
    titles = " ".join((sl.get("method_title") or "") for sl in (o.get("shipping_lines") or []))
    if o.get("status") == "tlv-pickup" or "נקודת מסירה" in titles:
        return "tlv|נק׳ מסירה ת״א"
    if "איסוף" in titles:
        br = str(meta.get("_gm_pickup_branch") or "").split(" - ")[0].replace("סניף", "").strip()
        return f"pickup|איסוף · {br}" if br else "pickup|איסוף עצמי"
    if "אותו היום" in titles or "אקספרס" in titles:
        return "express|אקספרס"
    return ""


def _li_attrs(li: dict) -> list:
    """מאפייני הוריאציה משורת ההזמנה (צבע/נפח/קישוריות...) — בלי מפתחות פנימיים."""
    out = []
    for m in (li.get("meta_data") or []):
        k = str(m.get("display_key") or m.get("key") or "")
        v = m.get("display_value") if m.get("display_value") is not None else m.get("value")
        if k.startswith("_") or not isinstance(v, (str, int, float)):
            continue
        vs = str(v).strip()
        if vs and len(vs) <= 50:
            out.append({"k": k.replace("בחירת ", ""), "v": vs})
        if len(out) >= 6:
            break
    return out


def _order_source(meta: dict) -> str:
    """תג מקור ההזמנה — מ-Order Attribution של WC (גוגל אורגני/Ads/זאפ/ישיר/GreenOS)."""
    if meta.get("greenos_source"):
        return "GreenOS"
    st = (meta.get("_wc_order_attribution_source_type") or "").lower()
    src = (meta.get("_wc_order_attribution_utm_source") or "").lower()
    med = (meta.get("_wc_order_attribution_utm_medium") or "").lower()
    if "google" in src:
        return "Google Ads" if med in ("cpc", "ppc", "paid") else "Google אורגני"
    if src:
        return src.replace("www.", "")
    if st == "typein":
        return "ישיר"
    if st == "admin":
        return "ידני (אדמין)"
    if st == "referral":
        ref = (meta.get("_wc_order_attribution_referrer") or "")
        try:
            from urllib.parse import urlparse
            return urlparse(ref).netloc.replace("www.", "") or "הפניה"
        except Exception:  # noqa: BLE001
            return "הפניה"
    return st or ""


@app.get("/api/admin/orders/{oid}")
def admin_order_detail(oid: int, x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    import requests as _rq
    base, k, s = _wc_creds()
    r = _rq.get(f"{base}/wp-json/wc/v3/orders/{oid}", auth=(k, s), timeout=45)
    if not r.ok:
        raise HTTPException(404, "הזמנה לא נמצאה")
    o = r.json()
    meta = {m.get("key"): m.get("value") for m in (o.get("meta_data") or [])}
    notes = []
    try:
        rn = _rq.get(f"{base}/wp-json/wc/v3/orders/{oid}/notes", auth=(k, s), timeout=30)
        if rn.ok:
            notes = [{"note": n.get("note"), "date": n.get("date_created"),
                      "customer": bool(n.get("customer_note")), "author": n.get("author") or ""}
                     for n in rn.json()[:15]]
    except Exception:  # noqa: BLE001
        pass
    # פרטי תשלום PayPlus — מהתוסף באתר (payplus_*) עם נסיגה ל-meta של GreenOS
    pay = {
        "approval": meta.get("payplus_approval_num") or meta.get("greenos_payplus_approval") or "",
        "four_digits": meta.get("payplus_four_digits") or meta.get("greenos_payplus_4digits") or "",
        "payments": meta.get("payplus_number_of_payments") or meta.get("greenos_payplus_payments") or "",
        "brand": meta.get("payplus_brand_name") or meta.get("greenos_payplus_brand") or "",
        "method": meta.get("payplus_method") or "",
        "clearing": meta.get("payplus_clearing_name") or "",
        "status_desc": meta.get("payplus_status_description") or "",
    }
    # בקשת העברה משודרת לאתר — כל הסניפים+כמויות (פיצול רב-יחידתי בין סניפים)
    bcast = None
    try:
        bcast = _build_bcast_map(db.plan_list()).get(str(o.get("number")))
    except Exception:  # noqa: BLE001
        pass
    oos = False
    partial = False
    nosku = False
    return_open = False
    try:
        import json as _json2
        raw = db.sales_state_get("order_oos_list")
        for x in (_json2.loads(raw) if raw else []):
            if str(x.get("number")) == str(o.get("number")):
                oos = True
                partial = bool(x.get("partial"))
                break
        rawu = db.sales_state_get("order_unmatched_list")
        nosku = any(str(x.get("number")) == str(o.get("number"))
                    for x in (_json2.loads(rawu) if rawu else []))
        rawr = db.sales_state_get("order_return_list")
        return_open = any(str(x.get("number")) == str(o.get("number"))
                          for x in (_json2.loads(rawr) if rawr else []))
    except Exception:  # noqa: BLE001
        pass
    return {
        "src": _order_source(meta),
        "ship_tag": _ship_tag(o, meta),
        "bcast": bcast,
        "oos": oos,
        "partial": partial,
        "nosku": nosku,
        "return_open": return_open,
        "pay": pay if any(pay.values()) else None,
        "id": o.get("id"), "number": o.get("number"), "status": o.get("status"),
        "date": o.get("date_created"), "date_paid": o.get("date_paid"),
        "total": o.get("total"), "shipping_total": o.get("shipping_total"),
        "discount": o.get("discount_total"), "currency": o.get("currency_symbol") or "₪",
        "payment": o.get("payment_method_title") or "", "tx": o.get("transaction_id") or "",
        "billing": o.get("billing") or {}, "shipping": o.get("shipping") or {},
        "customer_note": o.get("customer_note") or "",
        "items": [{"name": li.get("name"), "qty": li.get("quantity"),
                   "total": li.get("total"), "sku": li.get("sku") or "",
                   "img": (li.get("image") or {}).get("src") or "",
                   "attrs": _li_attrs(li)}
                  for li in (o.get("line_items") or [])],
        "shipping_lines": [sl.get("method_title") for sl in (o.get("shipping_lines") or [])],
        "greenos": {kk: vv for kk, vv in meta.items() if str(kk).startswith("greenos")},
        "cargo": meta.get("cslfw_shipping") or None,
        "cargo_status": _cargo_status(meta, (o.get("billing") or {}).get("email") or ""),
        "notes": notes,
        "admin_url": f"{base}/wp-admin/post.php?post={oid}&action=edit",
    }


@app.post("/api/admin/orders/{oid}/return")
def admin_order_return(oid: int, close: int = 0,
                       x_admin_key: Optional[str] = Header(None)):
    """פתיחת/סגירת החזרה (איסוף מהלקוח). פתיחה: הערה + דגל 'החזרה' + פרטי לקוח
    לביצוע בדשבורד Cargo (אופציה ב׳). close=1: מסיר את הדגל ומתעד שנסגרה."""
    _require_admin(x_admin_key)
    import requests as _rq
    import json as _json
    base, k, s = _wc_creds()
    r = _rq.get(f"{base}/wp-json/wc/v3/orders/{oid}", auth=(k, s), timeout=30)
    if not r.ok:
        raise HTTPException(404, "הזמנה לא נמצאה")
    o = r.json()
    b = o.get("billing") or {}
    sh = o.get("shipping") or {}
    num = str(o.get("number"))
    note = ("↩️ החזרה נסגרה — דרך GreenOS" if close
            else "↩️ נפתחה החזרה (איסוף מהלקוח) — דרך GreenOS")
    try:
        _rq.post(f"{base}/wp-json/wc/v3/orders/{oid}/notes", auth=(k, s),
                 json={"note": note, "customer_note": False}, timeout=20)
    except Exception:  # noqa: BLE001
        pass
    try:   # דגל 'החזרה': הוספה/הסרה
        raw = db.sales_state_get("order_return_list")
        lst = _json.loads(raw) if raw else []
        lst = [x for x in lst if str(x.get("number")) != num]   # תמיד מסירים קודם
        if not close:
            lst.insert(0, {"number": num})
        db.sales_state_set("order_return_list", _json.dumps(lst[:200], ensure_ascii=False))
    except Exception:  # noqa: BLE001
        pass
    if close:
        return {"ok": True, "closed": True}
    src = sh if (sh.get("address_1") or sh.get("city")) else b
    addr = ", ".join(x for x in [src.get("address_1"), src.get("city")] if x).strip(", ")
    return {"ok": True, "customer": {
        "name": f"{(src.get('first_name') or b.get('first_name') or '')} {(src.get('last_name') or b.get('last_name') or '')}".strip(),
        "phone": b.get("phone") or "", "address": addr, "email": b.get("email") or ""}}


@app.post("/api/admin/orders/{oid}/auto-transfer")
def admin_order_auto_transfer(oid: int, force: int = 0,
                              x_admin_key: Optional[str] = Header(None)):
    """מריץ מחדש את auto_transfer על הזמנה בודדת — לשימוש אחרי הצמדת/תיקון SKU
    למוצר שהיה מנותק. משדר/מסמן לפי מלאי. force=1: מנקה שידור קיים ומשדר מחדש
    (לתיקון שיריון של צבע שגוי). בלי force — לא יוצר כפילות אם כבר שודר."""
    _require_admin(x_admin_key)
    import requests as _rq
    import auto_transfer
    import re as _re3
    base, k, s = _wc_creds()
    r = _rq.get(f"{base}/wp-json/wc/v3/orders/{oid}", auth=(k, s), timeout=45)
    if not r.ok:
        raise HTTPException(404, "הזמנה לא נמצאה")
    o = r.json()
    catalog = db.catalog_load()
    if not catalog:
        raise HTTPException(503, "קטלוג לא טעון — נסה שוב בעוד דקה")
    onum = str(o.get("number"))
    existing = [ln for ln in db.plan_list()
                if _re3.search(rf"הזמנת אתר #{onum}(?!\d)", ln.get("created_by") or "")]
    if existing and not force:
        return {"ok": True, "already": True, "lines": len(existing)}
    cleared = 0
    if force and existing:
        for ln in existing:
            try:
                db.plan_delete(ln.get("id"))
                cleared += 1
            except Exception:  # noqa: BLE001
                pass
    created = auto_transfer._handle_order(o, catalog)
    db.sales_state_set(f"auto_tr_seen:{o.get('id')}", "rebroadcast")
    return {"ok": True, "created": created, "cleared": cleared,
            "items": [li.get("sku") for li in (o.get("line_items") or [])]}


class OrderStatusIn(BaseModel):
    status: str


@app.post("/api/admin/orders/{oid}/status")
def admin_order_status(oid: int, body: OrderStatusIn, x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    import requests as _rq
    base, k, s = _wc_creds()
    # אימות מול רשימת הסטטוסים האמיתית של האתר (כולל מותאמים: shipping-stage,
    # order-ready, delivered, send-cargo...) ולא רשימה קשיחה. ליבה כ-fallback.
    core = {"pending", "processing", "on-hold", "completed", "cancelled", "refunded", "failed"}
    valid = set(_wc_statuses(base, k, s).keys()) | core
    st = (body.status or "").strip()
    st = st[3:] if st.startswith("wc-") else st   # תמיכה גם אם נשלח עם תחילית wc-
    if st not in valid:
        raise HTTPException(400, f"סטטוס לא מוכר: {st}")
    r = _rq.put(f"{base}/wp-json/wc/v3/orders/{oid}", json={"status": st},
                auth=(k, s), timeout=45)
    if not r.ok:
        raise HTTPException(502, f"עדכון הסטטוס נכשל ({r.status_code}: {r.text[:150]})")
    return {"ok": True, "status": r.json().get("status")}


def _normalize_status(st: str) -> str:
    """מאמת סטטוס מול רשימת הסטטוסים האמיתית של האתר (כולל מותאמים)."""
    base, k, s = _wc_creds()
    core = {"pending", "processing", "on-hold", "completed", "cancelled", "refunded", "failed"}
    valid = set(_wc_statuses(base, k, s).keys()) | core
    st = (st or "").strip()
    st = st[3:] if st.startswith("wc-") else st
    if st not in valid:
        raise HTTPException(400, f"סטטוס לא מוכר: {st}")
    return st


class StatusScheduleIn(BaseModel):
    status: str
    run_at: str                     # ISO; אם בלי אזור-זמן — מניחים שעון ישראל


@app.post("/api/admin/orders/{oid}/status/schedule")
def admin_order_status_schedule(oid: int, body: StatusScheduleIn,
                                x_admin_key: Optional[str] = Header(None)):
    """תזמון שינוי סטטוס הזמנה לזמן עתידי — רץ בשרת (תמיד פעיל), שורד הפעלות מחדש.
    run_at: ISO. אם נשלח בלי offset — מתפרש כשעון ישראל (cfg.TZ)."""
    _require_admin(x_admin_key)
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    st = _normalize_status(body.status)
    tz = ZoneInfo(cfg.TZ)
    try:
        dt = _dt.fromisoformat((body.run_at or "").strip())
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "run_at לא תקין (נדרש ISO, למשל 2026-06-15T16:00)")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    run_at = dt.astimezone(tz).isoformat()
    label = _wc_statuses(*_wc_creds()).get(st, st)
    onum = ""
    try:
        base, k, s = _wc_creds()
        rr = _rq_mod().get(f"{base}/wp-json/wc/v3/orders/{oid}",
                           params={"_fields": "number"}, auth=(k, s), timeout=20)
        if rr.ok:
            onum = str(rr.json().get("number") or "")
    except Exception:  # noqa: BLE001
        pass
    sid = db.sched_status_add(oid, st, run_at, order_number=onum,
                              status_label=label, created_by="קונסולת ניהול")
    logger.info("scheduled status #%s: order %s -> %s at %s", sid, oid, st, run_at)
    return {"ok": True, "id": sid, "order_id": oid, "status": st,
            "status_label": label, "run_at": run_at}


@app.get("/api/admin/scheduled-status")
def admin_scheduled_status_list(x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    return JSONResponse({"pending": db.sched_status_pending()},
                        headers={"Cache-Control": "no-store"})


@app.delete("/api/admin/scheduled-status/{sid}")
def admin_scheduled_status_cancel(sid: int, x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    n = db.sched_status_cancel(sid)
    return {"ok": True, "canceled": n}


def _rq_mod():
    import requests as _rq
    return _rq


def bot_get_variations(product_id) -> list:
    """וריאציות מוצר לבוט: [{id, price, color, storage, stock}]. ריק = מוצר פשוט."""
    creds = _wc_creds()
    if not creds:
        return []
    base, k, s = creds
    import requests as _rq
    try:
        r = _rq.get(base + f"/wp-json/wc/v3/products/{int(product_id)}/variations",
                    params={"per_page": 100}, auth=(k, s), timeout=20)
        vs = r.json() if r.ok else []
    except Exception as e:  # noqa: BLE001
        logger.warning("bot_get_variations failed for %s: %s", product_id, e)
        return []
    # מפת slug→שם-תצוגה לכל תכונה (ה-option בוריאציה הוא לעיתים slug, כמו 'esim-only')
    term_disp = {}
    for a in ((vs[0].get("attributes") if vs else []) or []):
        nm, aid = a.get("name"), a.get("id")
        if not (nm and aid) or nm in term_disp:
            continue
        term_disp[nm] = {}
        try:
            rt = _rq.get(base + f"/wp-json/wc/v3/products/attributes/{aid}/terms",
                         params={"per_page": 100}, auth=(k, s), timeout=12)
            for t in (rt.json() if rt.ok else []):
                term_disp[nm][t.get("slug")] = t.get("name")
                term_disp[nm][t.get("name")] = t.get("name")
        except Exception:  # noqa: BLE001
            pass

    def _disp(nm, opt):
        return term_disp.get(nm, {}).get(opt, opt)

    out = []
    for v in (vs or []):
        attr_order = [a.get("name") for a in (v.get("attributes") or []) if a.get("option")]
        attrs = {a.get("name"): a.get("option") for a in (v.get("attributes") or []) if a.get("option")}
        attrs_disp = {nm: _disp(nm, opt) for nm, opt in attrs.items()}
        out.append({"id": v.get("id"), "price": v.get("price"),
                    "color": attrs.get("בחירת צבע") or "",
                    "storage": attrs.get("בחירת נפח אחסון") or attrs.get("נפח אחסון") or "",
                    "attrs": attrs, "attrs_disp": attrs_disp,   # ערך לחיפוש + שם לתצוגה
                    "attr_order": attr_order,
                    "stock": v.get("stock_status"),
                    "permalink": v.get("permalink") or ""})  # מכיל את ה-attributes ל-add-to-cart
    return out


def bot_repair_status(phone: str = "", fix_id=None) -> list:
    """סטטוס תיקוני מעבדה ללקוח — מ-NewOrder /api/Fixes. זיהוי לפי טלפון הלקוח
    (customer.phoneNumber) או מספר תיקון (fixId). מחזיר רשימת תיקונים תואמים."""
    import poller
    cli = poller.client()
    # ⚠️ /api/Fixes בלי branchId מחזיר רק סניף אחד (ה-100 עם ה-fixId הגבוה). הספרה
    # הראשונה ב-fixId = הסניף (1=גן העיר,2=סטאר,3=סיטי,4=עד הלום), אז שואלים את כל
    # הסניפים וממזגים — אחרת תיקונים מסניפים אחרים (כמו 216417 בסניף 2) לא נמצאים.
    fixes = []
    branches = (1, 2, 3, 4, 5)
    if fix_id is not None and str(fix_id) and str(fix_id)[0].isdigit():
        b0 = int(str(fix_id)[0])             # אופטימיזציה: לפי מספר → קודם הסניף שלו
        if b0 in branches:
            branches = (b0,) + tuple(b for b in branches if b != b0)
    for br in branches:
        try:
            res = cli.get_fixes(status=-1, branch_id=br)
            fixes.extend(res if isinstance(res, list)
                         else (res.get("data") or res.get("fixes") or []))
        except Exception as e:  # noqa: BLE001
            logger.warning("get_fixes branch %s failed: %s", br, e)
        if fix_id is not None and any(str(f.get("fixId")) == str(fix_id)
                                      for f in fixes if isinstance(f, dict)):
            break                            # נמצא לפי מספר → לא צריך להמשיך לסניפים

    def _norm(p):
        d = "".join(ch for ch in str(p or "") if ch.isdigit())
        return d[-9:] if len(d) >= 9 else d
    target = _norm(phone)
    out = []
    for f in (fixes or []):
        if not isinstance(f, dict):
            continue
        cust = f.get("customer") or {}
        if fix_id is not None:
            if str(f.get("fixId")) != str(fix_id):
                continue
        elif not (target and _norm(cust.get("phoneNumber")) == target):
            continue
        dev = f.get("deviceInfo") or {}
        _model = (dev.get("model") or "").strip()
        _color = (dev.get("color") or "").strip()
        _device = _model if (not _color or _color in _model) else f"{_model} {_color}".strip()
        out.append({
            "fixId": f.get("fixId"), "status": (f.get("statusName") or "").strip(),
            "device": _device,
            "created": f.get("creationDate"), "fixed": f.get("fixedDate"),
            "delivered": f.get("deliveredDate"), "name": (cust.get("name") or "").strip(),
            "estimated": f.get("estimatedCharge"), "invoice": f.get("invoiceCharge"),
        })
    out.sort(key=lambda r: r.get("fixId") or 0, reverse=True)   # אחרון קודם
    return out


_REPAIR_CACHE = {"at": 0.0, "data": None}
_REPAIR_HE = {"אייפון": "iphone", "גלקסי": "galaxy", "סמסונג": "samsung",
              "אייפד": "ipad", "וואטש": "watch", "שעון": "watch", "פרו": "pro",
              "מקס": "max", "מאקס": "max", "מיני": "mini", "פלוס": "plus",
              "אולטרה": "ultra", "פלוו": "plus", "שיאומי": "xiaomi", "שאומי": "xiaomi",
              "רדמי": "redmi", " רדמי": "redmi", "פוקו": "poco", "נוט": "note",
              "נוט ": "note "}
# מילות מותג/יצרן — לא חובה שיופיעו במפתח הדגם (הרבה שורות בגיליון בלי קידומת מותג)
_REPAIR_BRAND_W = {"iphone", "ipad", "galaxy", "samsung", "xiaomi", "redmi",
                   "poco", "pocophone", "apple", "mi"}


def _repair_prices():
    """מחירון תיקונים מהגיליון המפורסם (CSV), cache 30 דק'."""
    import time as _t
    if _REPAIR_CACHE["data"] is not None and (_t.time() - _REPAIR_CACHE["at"] < 1800):
        return _REPAIR_CACHE["data"]
    try:
        import repair_prices
        data = repair_prices.fetch_and_parse()
        if data.get("devices"):
            _REPAIR_CACHE.update(at=_t.time(), data=data)
    except Exception as e:  # noqa: BLE001
        logger.warning("repair prices fetch failed: %s", e)
    return _REPAIR_CACHE["data"] or {"devices": {}, "services": []}


def bot_repair_quote(query: str) -> list:
    """מחיר תיקון לפי דגם. מחזיר רשימת דגמים תואמים ({display, repairs}) —
    התאמה אחת = הצג; כמה = בקש בחירה; ריק = לא נמצא."""
    import re as _re
    data = _repair_prices()
    devices = data.get("devices") or {}
    if not devices:
        return []
    q = (query or "").lower()
    for he, en in _REPAIR_HE.items():
        q = q.replace(he, en)
    _stop = ("של", "תיקון", "מחיר", "כמה", "עולה", "רגיל", "רגילה", "פשוט",
             "בסיסי", "basic", "standard", "דגם", "ה",
             # מילות פעולה/מילוי בניסוח טבעי ("מחיר להחלפת מסך לגלקסי A34")
             "החלפת", "החלפה", "החליף", "תקן", "צריך", "רוצה", "אפשר",
             "את", "עבור", "הזמין", "לי")
    def _depref(w):              # תחילית 'ל' דבוקה: לgalaxy→galaxy, להחלפת→החלפת
        return _re.sub(r"^ל(?=.{2})", "", w)
    qwords = [w for w in (_depref(w) for w in _re.split(r"[\s/]+", q)) if w and w not in _stop]
    # מילות מהות-תיקון (מסך/סוללה/שקע...) אינן חלק מהדגם — מסירים, כדי שהדגם יימצא
    # גם כשהלקוח כתב 'מסך וגב אייפון 14' (כמה תיקונים + דגם יחד) בשלב הדגם.
    _parts = {w.lower() for syns in _REPAIR_PART_SYN.values() for w in syns}
    _parts |= {k.lower() for k in _REPAIR_PART_SYN}

    def _is_part(w):                          # גם עם ו' חיבור ('וגב' → 'גב')
        return w in _parts or (w.startswith("ו") and len(w) > 2 and w[1:] in _parts)
    qwords = [w for w in qwords if not _is_part(w)]
    if not qwords:
        return []
    q_norm = " ".join(qwords)
    if q_norm in devices:                    # התאמה מדויקת = הדגם הבסיסי (לא הפרו)
        return [devices[q_norm]]

    def _match(words):
        return sorted((k for k, v in devices.items() if words and all(w in k for w in words)),
                      key=len)
    keys = _match(qwords)
    if not keys:                             # נפילה: הרבה שורות בגיליון בלי קידומת מותג
        core = [w for w in qwords if w not in _REPAIR_BRAND_W]   # ("note 12" בלי "xiaomi")
        if core and core != qwords:
            keys = _match(core)
    return [devices[k] for k in keys[:9]]


_REPAIR_PART_SYN = {
    "מסך": ["מסך", "צג", "תצוגה", "זכוכית", "מסכ", "screen", "glass", "טאץ", "טצ"],
    "סוללה": ["סוללה", "בטריה", "battery", "נגמרת", "מתרוקנת", "לא מחזיק", "מתנפח"],
    "שקע": ["שקע", "טעינה", "נטען", "כבל", "charging", "port", "מחבר", "לא נכנס"],
    "גב": ["גב", "כיסוי אחורי", "back", "מכסה אחורי", "זכוכית אחורית"],
    "מצלמה אחורית": ["מצלמה אחורית", "מצלמה ראשית", "rear", "מצלמה מאחור", "מצלמה"],
    "מצלמה קדמית": ["מצלמה קדמית", "סלפי", "selfie", "מצלמה קדמ", "מצלמה"],
    "מסגרת קומפלט": ["מסגרת", "קומפלט", "frame", "שלדה"],
    "עדשות": ["עדשה", "עדשות", "זכוכית מצלמה", "lens"],
    "אפכרסת": ["אפרכסת", "אפכרסת", "רמקול שיחה", "לא שומע בשיחה", "earpiece", "אוזנית"],
    "כפתור בית": ["כפתור בית", "הום", "home", "טביעת אצבע"],
    "הדלקה": ["הדלקה", "לא נדלק", "כפתור הפעלה", "power", "לא עולה"],
    "ווליום": ["ווליום", "עוצמה", "כפתור עוצמה", "volume", "וליום"],
    "צלצלן": ["צלצלן", "רמקול תחתון", "רמקול", "buzzer", "ringer", "לא שומעים"],
}


def bot_repair_match_part(device: dict, query: str) -> list:
    """מהות התיקון שהלקוח כתב → סוגי התיקון התקפים (מתוך כלל הסוגים המוכרים, לא רק
    אלה שיש להם מחיר לדגם — כדי להבחין בין 'לא מוכר' ל'אין מחיר לדגם')."""
    q = (query or "").lower()
    matched = []
    for rep, syns in _REPAIR_PART_SYN.items():
        if rep.lower() in q or any(s.lower() in q for s in syns):
            matched.append(rep)
    return matched


def bot_create_order(product_id, variation_id, price, name, phone) -> dict:
    """יוצר הזמנת WC (pending) + קישור תשלום PayPlus — לבוט ה-native. מחזיר
    {number, total, pay_link}. מקושר ל-IPN דרך payplus_pru:."""
    base, k, s = _wc_creds()
    import requests as _rq
    li = {"product_id": int(product_id), "quantity": 1}
    if variation_id:
        li["variation_id"] = int(variation_id)
    if price not in (None, "", "0"):
        tot = str(round(float(price), 2))
        li["subtotal"] = tot
        li["total"] = tot
    payload = {"status": "pending",
               "billing": {"first_name": (name or "לקוח")[:40], "phone": str(phone), "country": "IL"},
               "line_items": [li],
               "meta_data": [{"key": "greenos_source", "value": "whatsapp-bot"},
                             {"key": "greenos_wa_phone", "value": str(phone)}]}
    r = _rq.post(f"{base}/wp-json/wc/v3/orders", json=payload, auth=(k, s), timeout=45)
    if r.status_code not in (200, 201):
        logger.warning("bot order create failed %s: %s", r.status_code, r.text[:200])
        raise RuntimeError("order create failed")
    o = r.json()
    pp = _payplus_link(float(o.get("total") or 0), str(o.get("number")),
                       {"name": name, "phone": str(phone)}, payments=1)
    try:
        db.sales_state_set(f"payplus_pru:{pp['pru']}", str(o.get("id")))
    except Exception:  # noqa: BLE001
        pass
    return {"number": o.get("number"), "total": o.get("total"), "pay_link": pp["link"]}


def bot_product_search(q: str, limit: int = 8) -> list:
    """חיפוש מוצרים לבוט ה-native — שם/מחיר/תמונה/קישור, **מדורג לפי רלוונטיות**
    (מספר דגם + דרגה Pro/Ultra במשקל גבוה → S26 Ultra עולה ראשון). עם נפילה
    מקצרת אם 0 תוצאות (כמו ב-wc-search)."""
    creds = _wc_creds()
    q = (q or "").strip()
    if not creds or len(q) < 2:
        return []
    base, k, s = creds
    import requests as _rq
    import re as _re

    # מילות מילוי שמזהמות את החיפוש ("עם","אני מחפש"...) — WC מתאים עליהן בתיאורים
    _FILLERS = {"אני", "מחפש", "מחפשת", "רוצה", "רוצים", "צריך", "צריכה", "את", "עם",
                "של", "יש", "לכם", "לי", "אפשר", "מה", "המחיר", "חפש", "למצוא",
                "מעוניין", "מעוניינת", "בבקשה", "היי", "שלום", "הי", "כמה", "עולה",
                "במלאי", "זמין", "עוד", "גם", "כן", "תודה", "גיגה", "ג'יגה",
                "gb", "ram", "רם", "mb", "tb"}
    _HEB_TIER = {"פרו": "pro", "מקס": "max", "אולטרה": "ultra", "אלטרה": "ultra",
                 "פלוס": "plus", "מיני": "mini", "אייר": "air", "לייט": "lite",
                 "פולד": "fold", "פליפ": "flip"}
    _HEB_BRAND = {"אייפון": "iphone", "אפל": "apple", "גלקסי": "galaxy", "סמסונג": "samsung",
                  "שיאומי": "xiaomi", "רדמי": "redmi", "אופו": "oppo", "הונור": "honor",
                  "וואנפלוס": "oneplus"}
    _TIERS = {"pro", "max", "ultra", "plus", "fe", "mini", "air", "edge", "fold",
              "flip", "lite", "neo", "ace", "prime"}
    # נפחי אחסון — תכונת וריאציה, לא בשם המוצר-האב. מוציאים מחיפוש WC (פוגע בתוצאות).
    _STORAGE = {"32", "64", "128", "256", "512", "1024", "2048"}

    raw = [t for t in _re.split(r"[\s/,]+", q.lower()) if t and t not in _FILLERS]

    def _en(t):
        return _HEB_BRAND.get(t) or _HEB_TIER.get(t) or t
    search_he = " ".join(t for t in raw if t not in _STORAGE).strip()
    search_en = " ".join(_en(t) for t in raw if t not in _STORAGE).strip()
    search_q = search_he or q.strip()

    def _fetch(query):
        if not query:
            return []
        try:
            r = _rq.get(base + "/wp-json/wc/v3/products",
                        params={"search": query, "per_page": 20, "status": "publish"},
                        auth=(k, s), timeout=15)
            return r.json() if r.ok else []
        except Exception:  # noqa: BLE001
            return []

    # שתי שאילתות — עברית (כפי שהוקלד) + אנגלית ממופה — ומיזוג. שמות מוצרים מעורבים:
    # אייפון בעברית, סמסונג/גלקסי באנגלית בלבד. כך מוצאים את שניהם.
    prods, _seen = [], set()
    for _sq in [search_q] + ([search_en] if search_en and search_en != search_q else []):
        for p in _fetch(_sq):
            if isinstance(p, dict) and p.get("id") not in _seen:
                _seen.add(p.get("id"))
                prods.append(p)
    if not prods:                       # נפילה אחרונה: מקצרים מילה מהסוף
        toks = (search_en or search_q).split()
        while not prods and len(toks) > 1:
            toks = toks[:-1]
            prods = _fetch(" ".join(toks))

    q_models = [t for t in raw if _re.fullmatch(r"\d{1,4}", t) and t not in _STORAGE]

    def _forms(t):
        f = {t}
        if t in _HEB_TIER:
            f.add(_HEB_TIER[t])
        if t in _HEB_BRAND:
            f.add(_HEB_BRAND[t])
        return f

    scored = []
    for p in (prods or []):
        if not isinstance(p, dict):
            continue
        # מסננים: (1) מוצרים נסתרים (catalog_visibility=hidden) — אלה "מוצרי צל לזאפ"
        # שלא אמורים להיראות ללקוח; (2) external/grouped — לא ניתנים לקנייה בעגלה.
        if (p.get("catalog_visibility") == "hidden"
                or p.get("type") in ("external", "grouped")):
            continue
        name = (p.get("name") or "").lower()
        sc, model_hit = 0, False
        for t in raw:
            hit = any(f in name for f in _forms(t))
            if t in q_models:
                if hit:
                    sc += 6
                    model_hit = True
            elif t in _TIERS or t in _HEB_TIER:
                if hit:
                    sc += 4
            elif len(t) >= 2 and hit:
                sc += 2
        # סינון זבל: אם בשאילתה יש מספר דגם — חובה שיתאים; אחרת לפחות התאמה אחת
        if q_models and not model_hit:
            continue
        if sc <= 0:
            continue
        img = ((p.get("images") or [{}])[0] or {}).get("src", "")
        scored.append((sc, {"id": p.get("id"), "name": p.get("name") or "",
                            "price": p.get("price"), "permalink": p.get("permalink") or "",
                            "sku": p.get("sku") or "", "type": p.get("type"),
                            "image": img, "stock_status": p.get("stock_status"),
                            # מותג המוצר (taxonomy native) — לחילוץ מדויק של הדגם לכותרת
                            "brand": ((p.get("brands") or [{}])[0] or {}).get("name", ""),
                            # קטגוריות — לקישור "עוד באתר" חכם (לפי קטגוריית המוצרים שנמצאו)
                            "cats": [{"slug": c.get("slug"), "name": c.get("name")}
                                     for c in (p.get("categories") or []) if c.get("slug")]}))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [p for _sc, p in scored][:limit]


_CATS_CACHE = {"at": 0.0, "data": None}


def _wc_categories():
    """כל קטגוריות המוצרים (slug→{name,count}), cache שעה — לבחירת קטגוריה לקישור."""
    import time as _t
    if _CATS_CACHE["data"] is not None and (_t.time() - _CATS_CACHE["at"] < 3600):
        return _CATS_CACHE["data"]
    base, k, s = _wc_creds()
    import requests as _rq
    out, page = {}, 1
    try:
        while page <= 12:
            r = _rq.get(f"{base}/wp-json/wc/v3/products/categories",
                        params={"per_page": 100, "page": page}, auth=(k, s), timeout=25)
            js = r.json() if r.ok else []
            for c in js:
                out[c.get("slug")] = {"name": c.get("name") or "", "count": c.get("count") or 0,
                                      "id": c.get("id")}
            if len(js) < 100:
                break
            page += 1
        _CATS_CACHE.update(at=_t.time(), data=out)
    except Exception as e:  # noqa: BLE001
        logger.warning("categories fetch failed: %s", e)
    return out


_TERM_LINK_CACHE: dict = {}


def _wc_term_link(taxonomy: str, slug: str) -> str:
    """הכתובת הקנונית (permalink) של ארכיון taxonomy term — דרך wp/v2, cache.
    קריטי לקישורי הבוט: קישור `?product_cat=slug` עושה redirect קנוני, והדפדפן
    הפנימי של וואטסאפ נכשל ב-navigation הראשון על redirect קר ('אין אינטרנט').
    קישור ישיר לכתובת הקנונית = בלי redirect = נטען בלחיצה הראשונה."""
    key = f"{taxonomy}:{slug}"
    if key in _TERM_LINK_CACHE:
        return _TERM_LINK_CACHE[key]
    link = ""
    try:
        base = _wc_creds()[0]
        import requests as _rq
        r = _rq.get(f"{base}/wp-json/wp/v2/{taxonomy}",
                    params={"slug": slug}, auth=_wp_app_auth(), timeout=20)
        if r.ok and r.json():
            link = r.json()[0].get("link") or ""
    except Exception as e:  # noqa: BLE001
        logger.warning("term link fetch failed %s/%s: %s", taxonomy, slug, e)
    _TERM_LINK_CACHE[key] = link
    return link


def bot_best_category(cat_freq, min_count=0):
    """בוחר קטגוריה לקישור 'עוד באתר'. cat_freq = {slug: כמה מהמוצרים שנמצאו שייכים
    לקטגוריה}. הכלל: לא קטגוריית מותג (שם אנגלי); הקטגוריה ה**משותפת לרוב** (freq
    גבוה — כך נבחר 'אוזניות' ולא תת-סוג 'open ear'), ובין שוות-freq הספציפית ביותר
    (count נמוך — 'סטרימרים' ולא 'מוצרי חשמל'). מחזיר {slug, name, count} או None."""
    cats = _wc_categories()
    cand = []
    for sl, freq in (cat_freq or {}).items():
        info = cats.get(sl)
        if not info:
            continue
        nm = info.get("name") or ""
        if nm.isascii():          # קטגוריות מותג/אנגלית (AMAZON, Xiaomi...) — מדלגים
            continue
        cnt = info.get("count") or 0
        if cnt > min_count:
            cand.append((-int(freq), cnt, sl, nm))   # freq גבוה קודם, ואז count נמוך
    if not cand:
        return None
    cand.sort()
    return {"slug": cand[0][2], "name": cand[0][3], "count": cand[0][1]}


# ════════════════ חיפוש חכם (Smart Search v2) ════════════════
# מנוע שמבין כוונה: ממפה את שאילתת הלקוח אל אוצר-המילים החי של החנות
# (קטגוריות/מותגים/תכונות) ומסנן עם הפילטרים האמיתיים של WooCommerce — במקום
# ניחוש מילולי. שכבת Claude Haiku (אם יש ANTHROPIC_API_KEY) מבינה ניסוח חופשי;
# בלעדיה נופל להתאמת-vocabulary היוריסטית. שתיהן מסתנכרנות לבד עם החנות.
_BRANDS_CACHE = {"at": 0.0, "data": None}
_HE_BRAND_ALIAS = {"אנקר": "anker", "סמסונג": "samsung", "אפל": "apple", "שיאומי": "xiaomi",
                   "סוני": "sony", "וואווי": "huawei", "אופו": "oppo", "ריאלמי": "realme",
                   "הונור": "honor", "וויוו": "vivo", "מרשל": "marshall", "בוס": "bose",
                   "נטינג": "nothing", "גוגל": "google", "וואנפלוס": "oneplus"}
_SEARCH_STOP = {"עם", "של", "את", "אני", "מחפש", "מחפשת", "רוצה", "צריך", "יש", "לכם",
                "תחת", "עד", "מתחת", "מעל", "the", "a", "for", "with", "and", "מכשיר", "דגם"}


def _wc_brands():
    """כל מותגי החנות (taxonomy product_brand) — [{name, slug, id}], cache שעה."""
    import time as _t
    if _BRANDS_CACHE["data"] is not None and (_t.time() - _BRANDS_CACHE["at"] < 3600):
        return _BRANDS_CACHE["data"]
    base, k, s = _wc_creds()
    import requests as _rq
    out, page = [], 1
    try:
        while page <= 6:
            r = _rq.get(f"{base}/wp-json/wc/v3/products/brands",
                        params={"per_page": 100, "page": page}, auth=(k, s), timeout=25)
            js = r.json() if r.ok else []
            for b in js:
                if (b.get("count") or 0) > 0:
                    out.append({"name": b.get("name") or "", "slug": b.get("slug") or "",
                                "id": b.get("id")})
            if len(js) < 100:
                break
            page += 1
        _BRANDS_CACHE.update(at=_t.time(), data=out)
    except Exception as e:  # noqa: BLE001
        logger.warning("brands fetch failed: %s", e)
    return out


def _search_vocab():
    """אוצר-המילים המובנה של החנות לחיפוש החכם (מותגים/קטגוריות/תכונות)."""
    cats = [{"slug": sl, "name": info.get("name") or "", "count": info.get("count") or 0,
             "id": info.get("id")}
            for sl, info in (_wc_categories() or {}).items() if info.get("id")]
    return {"brands": _wc_brands(), "cats": cats, "terms": _wc_attr_terms()}


def _parse_price(ql):
    import re as _re2
    mx = _re2.search(r"(?:עד|תחת|מתחת\s*ל-?|under|below|<)\s*[₪]?\s*(\d{2,6})", ql)
    mn = _re2.search(r"(?:מעל|above|over|>)\s*[₪]?\s*(\d{2,6})", ql)
    return (int(mx.group(1)) if mx else 0, int(mn.group(1)) if mn else 0)


def _understand_heuristic(q, vocab):
    """מיפוי שאילתה→מבנה ע"י התאמה לאוצר-המילים החי של החנות (בלי AI)."""
    import re as _re2
    ql = (q or "").lower().strip()
    max_price, min_price = _parse_price(ql)
    ql2 = _re2.sub(r"(?:עד|תחת|מתחת\s*ל-?|מעל|under|below|above|over|<|>)\s*[₪]?\s*\d{2,6}",
                   " ", ql)
    toks = [t for t in _re2.split(r"[\s/,]+", ql2) if t and t not in _SEARCH_STOP]
    tokset = set(toks)
    brand = None
    for b in vocab["brands"]:
        bl = (b["name"] or "").lower()
        if bl and (bl in ql or b["slug"] in tokset):
            brand = b
            break
    if not brand:
        for he, en in _HE_BRAND_ALIAS.items():
            if he in ql:
                brand = next((b for b in vocab["brands"]
                              if b["slug"] == en or (b["name"] or "").lower() == en),
                             {"name": en.title(), "slug": en, "id": None})
                break
    def _cat_hit(cnl):
        if cnl in ql:                          # שם הקטגוריה במלואו בשאילתה
            return True
        cwords = cnl.split()
        if len(cwords) == 1 and len(cwords[0]) >= 4:   # קטגוריה חד-מילתית — התאמת תחילית
            cw = cwords[0]                              # (סטרימר↔סטרימרים, יחיד↔רבים)
            return any(len(t) >= 4 and (cw.startswith(t) or t.startswith(cw)) for t in toks)
        return False
    cmatch = [c for c in vocab["cats"]
              if c["name"] and not c["name"].isascii() and _cat_hit(c["name"].lower())]
    cmatch.sort(key=lambda c: c["count"])      # הקטגוריה הספציפית ביותר קודם
    cat = cmatch[0] if cmatch else None
    # תכונות — התאמה **קשיחה** בלבד (היוריסטיקה לא מנחשת תכונות; ניחוש שגוי גרוע
    # מכלום): slug אנגלי מדויק (anc) או כל מילות-העברית של ה-term קיימות בשאילתה.
    bname = (brand["name"] or "").lower() if brand else ""
    cat_words = set((cat["name"] or "").lower().split()) if cat else set()
    term_hits = []
    for t in vocab["terms"]:
        tn = (t["name"] or "").lower()
        if bname and tn == bname:              # שם מותג אינו תכונה
            continue
        he_words = {w for w in _re2.split(r"[\s/,\-]+", tn)
                    if len(w) >= 3 and not w.isascii() and w not in cat_words}
        slug_hit = bool(t["slug"]) and t["slug"].lower() in tokset
        # דורש ביטוי בן 2+ מילות-עברית (מילה גנרית בודדת כמו 'אלחוטי' לא מסננת)
        phrase_hit = len(he_words) >= 2 and he_words <= tokset
        if slug_hit or phrase_hit:
            term_hits.append((len(he_words) + (2 if slug_hit else 0), t))
    term_hits.sort(key=lambda x: x[0], reverse=True)
    terms = [t for _n, t in term_hits[:1]]     # WC מסנן תכונה אחת — הטובה ביותר
    used = set((brand["name"] or "").lower().split()) if brand else set()
    used |= set((cat["name"] or "").lower().split()) if cat else set()
    keywords = " ".join(t for t in toks if t not in used).strip()
    return {"brand": brand, "cat": cat, "cat_id": (cat or {}).get("id"),
            "terms": terms, "max_price": max_price, "min_price": min_price,
            "keywords": keywords, "via": "heuristic"}


def _understand_claude(q, vocab):
    """שכבת Claude Haiku — מיפוי ניסוח חופשי אל ה-taxonomy. None אם אין מפתח/נכשל."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    import requests as _rq
    import json as _json
    import re as _re2
    cat_names = [c["name"] for c in vocab["cats"] if c["name"] and not c["name"].isascii()]
    brand_names = [b["name"] for b in vocab["brands"] if b["name"]]
    term_list = [{"id": t["id"], "name": t["name"]} for t in vocab["terms"] if t["name"]]
    sys = ("אתה ממפה שאילתת קונה (עברית או אנגלית) בחנות אלקטרוניקה ישראלית אל "
           "הטקסונומיה של החנות. החזר אך ורק JSON תקין, בלי טקסט נוסף. "
           "השתמש רק בערכים שמופיעים ברשימות. אם הקונה מציין מותג שלא ברשימה — "
           "החזר אותו ב-brand עם brand_unknown=true. price_max/price_min רק אם צוין במפורש.")
    schema = ('{"category": <שם קטגוריה מהרשימה או null>, "brand": <שם מותג מהרשימה '
              'או null>, "brand_unknown": <true/false>, "term_ids": [<מזהי תכונות '
              'מהרשימה>], "price_max": <מספר או null>, "price_min": <מספר או null>, '
              '"keywords": "<מילות דגם/חיפוש שנותרו, או ריק>"}')
    user = (f"שאילתה: {q!r}\n\nקטגוריות: {_json.dumps(cat_names, ensure_ascii=False)}\n\n"
            f"מותגים: {_json.dumps(brand_names, ensure_ascii=False)}\n\n"
            f"תכונות: {_json.dumps(term_list, ensure_ascii=False)}\n\n"
            f"החזר JSON במבנה: {schema}")
    try:
        r = _rq.post("https://api.anthropic.com/v1/messages", timeout=12,
                     headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                              "content-type": "application/json"},
                     json={"model": "claude-haiku-4-5-20251001", "max_tokens": 400,
                           "system": sys, "messages": [{"role": "user", "content": user}]})
        if not r.ok:
            logger.warning("claude understand HTTP %s: %s", r.status_code, r.text[:200])
            return None
        txt = "".join(b.get("text", "") for b in r.json().get("content", []))
        m = _re2.search(r"\{[\s\S]*\}", txt)
        data = _json.loads(m.group(0)) if m else {}
    except Exception as e:  # noqa: BLE001
        logger.warning("claude understand failed: %s", e)
        return None
    cat = next((c for c in vocab["cats"] if c["name"] == data.get("category")), None)
    brand = None
    if data.get("brand"):
        brand = next((b for b in vocab["brands"]
                      if (b["name"] or "").lower() == str(data["brand"]).lower()), None)
        if not brand:
            sl = str(data["brand"]).lower().strip().replace(" ", "-")
            brand = {"name": data["brand"], "slug": sl, "id": None, "unknown": True}
    tid = set(data.get("term_ids") or [])
    terms = [t for t in vocab["terms"] if t["id"] in tid]
    return {"brand": brand, "cat": cat, "cat_id": (cat or {}).get("id"), "terms": terms,
            "max_price": int(data["price_max"]) if data.get("price_max") else 0,
            "min_price": int(data["price_min"]) if data.get("price_min") else 0,
            "keywords": (data.get("keywords") or "").strip(), "via": "claude"}


def _smart_pack(p):
    img = ((p.get("images") or [{}])[0] or {}).get("src", "")
    return {"id": p.get("id"), "name": p.get("name") or "", "price": p.get("price"),
            "permalink": p.get("permalink") or "", "sku": p.get("sku") or "",
            "type": p.get("type"), "image": img, "stock_status": p.get("stock_status"),
            "brand": ((p.get("brands") or [{}])[0] or {}).get("name", ""),
            "cats": [{"slug": c.get("slug"), "name": c.get("name")}
                     for c in (p.get("categories") or []) if c.get("slug")]}


def bot_wc_title_search(query: str, limit: int = 20) -> list:
    """חיפוש כותרת ישיר ב-WooCommerce (פרמטר search) — בלי מנוע ה-facet/relaxation.
    לכניסה מעמוד מוצר, שבה הכותרת בהודעה כמעט זהה לכותרת המוצר באתר. החיפוש של WC
    הוא AND על המילים, אז כותרת ארוכה (חבילה/תיאור) עלולה להחזיר 0 — לכן **משחררים**:
    מורידים מילים מהסוף עד שיש תוצאות. מחזיר packed; הדירוג לפי כיסוי בצד הבוט."""
    creds = _wc_creds()
    query = (query or "").strip()
    if not creds or len(query) < 2:
        return []
    base, k, s = creds
    import requests as _rq

    def _fetch(q):
        try:
            r = _rq.get(f"{base}/wp-json/wc/v3/products",
                        params={"search": q, "per_page": limit, "status": "publish"},
                        auth=(k, s), timeout=20)
            return r.json() if r.ok else []
        except Exception:  # noqa: BLE001
            return []
    toks = query.split()
    prods = []
    for cut in range(len(toks), 1, -1):          # מלא → פחות מילים, עד שיש תוצאות
        prods = _fetch(" ".join(toks[:cut]))
        if prods:
            break
    return [_smart_pack(p) for p in prods
            if p.get("catalog_visibility") != "hidden"
            and p.get("type") not in ("external", "grouped")]


def bot_smart_search(q: str, limit: int = 20) -> dict:
    """חיפוש חכם: מבין כוונה (Claude Haiku אם יש מפתח, אחרת היוריסטיקה), ממפה
    אל ה-taxonomy של החנות, ומסנן עם WooCommerce. מחזיר {results, meta}.
    אם לא זוהה אף facet מובנה — נופל לחיפוש המילולי הקיים (שמות/דגמים)."""
    q = (q or "").strip()
    if len(q) < 2:
        return {"results": [], "meta": {}}
    vocab = _search_vocab()
    facets = _understand_claude(q, vocab) or _understand_heuristic(q, vocab)
    structured = bool(facets.get("cat_id") or facets.get("brand") or facets.get("terms")
                      or facets.get("max_price") or facets.get("min_price"))
    if not structured:
        # שאילתת שם/דגם טהורה — החיפוש המילולי הקיים מצוין לזה
        return {"results": bot_product_search(q, limit=limit),
                "meta": {"via": facets.get("via"), "facets": facets}}
    base, k, s = _wc_creds()
    import requests as _rq
    terms = facets.get("terms") or []
    brand = facets.get("brand")
    cat_name = ((facets.get("cat") or {}).get("name") or "").lower()
    kw = [w for w in (facets.get("keywords") or "").lower().split() if w]
    if not kw:                                   # Haiku לפעמים מרוקן keywords — נפילה
        import re as _kre                        # למילות השאילתה (לא מותג/קטגוריה) לדירוג
        _used = set((brand.get("name", "") or "").lower().split()) if brand else set()
        _used |= set(cat_name.split())
        kw = [w for w in _kre.split(r"[\s/,]+", q.lower())
              if w and w not in _used and w not in _SEARCH_STOP and len(w) >= 3
              and not w.isdigit()]

    def _cat_related(p):
        # שייך לקטגוריית-היעד או לתת-קטגוריה שלה — לפי **שם** מול כל קטגוריות המוצר
        # (לא ID בודד צר; כך AirPods Max ב'אוזניות ורמקולים' נשאר תחת 'אוזניות').
        if not cat_name:
            return True
        for c in (p.get("categories") or []):
            cn = (c.get("name") or "").lower()
            if cn and (cat_name in cn or cn in cat_name):
                return True
        return False

    def _fetch(search=None, use_cat_id=False, use_term=True):
        params = {"per_page": 60, "status": "publish"}
        if use_cat_id and facets.get("cat_id"):
            params["category"] = facets["cat_id"]
        if facets.get("max_price"):
            params["max_price"] = facets["max_price"]
        if facets.get("min_price"):
            params["min_price"] = facets["min_price"]
        if use_term and terms:
            params["attribute"], params["attribute_term"] = terms[0]["attr"], terms[0]["id"]
        if search:
            params["search"] = search
        try:
            r = _rq.get(base + "/wp-json/wc/v3/products", params=params, auth=(k, s), timeout=20)
            raw = r.json() if r.ok else []
        except Exception:  # noqa: BLE001
            raw = []
        out = []
        for p in (raw or []):
            if not isinstance(p, dict):
                continue
            if (p.get("catalog_visibility") == "hidden"
                    or p.get("type") in ("external", "grouped")):
                continue
            if brand and brand.get("slug"):
                bn = (brand.get("name", "") or "").lower()
                name = (p.get("name") or "").lower()
                pb = [(b.get("slug"), (b.get("name") or "").lower()) for b in (p.get("brands") or [])]
                if not (any(brand["slug"] == bs or bn == bnm for bs, bnm in pb)
                        or (bn and bn in name)):
                    continue
            out.append(p)
        return out

    def _rank_pack(raw):
        # צמצום-קטגוריה רך: שומר רק קשורי-קטגוריה; אם מצמצם לאפס — מוותר (לא מאבד הכל)
        pool = [p for p in raw if _cat_related(p)] if cat_name else list(raw)
        if cat_name and not pool:
            pool = list(raw)
        scored = []
        for p in pool:
            name = (p.get("name") or "").lower()
            sc = sum(2 for w in kw if w in name)
            if _cat_related(p):
                sc += 1
            if p.get("stock_status") == "instock":
                sc += 1
            scored.append((sc, _smart_pack(p)))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [p for _sc, p in scored][:limit]

    def _gather(use_term=True):
        # **מיזוג** של שתי שאילתות → לא צר מדי ולא רחב מדי:
        # (1) דפדוף הקטגוריה (מבטיח את פריטי הקטגוריה — מקלדות, לא פדים),
        # (2) חיפוש מותג/דגם (מבטיח פריטים חוצי-קטגוריה — AirPods על פני תתי-קטגוריות).
        merged = {}
        if facets.get("cat_id"):
            for p in _fetch(use_cat_id=True, use_term=use_term):
                merged[p.get("id")] = p
        srch = brand.get("name") if brand else (" ".join(kw) if kw else q)
        if srch:
            for p in _fetch(search=srch, use_term=use_term):
                merged[p.get("id")] = p
        return list(merged.values())

    results = _rank_pack(_gather())
    if not results and terms:                     # שחרור: הורד תכונה שמצמצמת מדי
        results = _rank_pack(_gather(use_term=False))
    if not results:                               # אחרון: חיפוש מילולי עם קיצור מילים
        results = bot_product_search(q, limit=limit)
    note = None
    if brand and not results:                     # מותג שצוין אך אין לו מוצר בתוצאה
        note = f"no_brand:{brand.get('name')}"
    return {"results": results,
            "meta": {"brand": (brand or {}).get("name") if brand else None,
                     "cat": (facets.get("cat") or {}).get("name") if facets.get("cat") else None,
                     "max_price": facets.get("max_price") or None,
                     "terms": [t["name"] for t in terms], "note": note,
                     "via": facets.get("via")}}


# ── חשבוניות לקוח (נקלטות ממייל הקופה; לשליחה חוזרת ללקוח בוואטסאפ) ──
@app.get("/api/admin/invoices")
def admin_invoices(phone: str = "", q: str = "", order: str = "",
                   x_admin_key: Optional[str] = Header(None),
                   x_device_token: Optional[str] = Header(None)):
    """חיפוש חשבוניות שנקלטו — לפי טלפון לקוח / מספר הזמנה / טקסט (מספר/שם/סכום)."""
    _require_admin_or_device(x_admin_key, x_device_token)
    return JSONResponse({"invoices": db.invoice_search(phone=phone, q=q, order=order),
                         "total": db.invoice_count()},
                        headers={"Cache-Control": "no-store"})


@app.get("/api/admin/invoices/{iid}/pdf")
def admin_invoice_pdf(iid: int, x_admin_key: Optional[str] = Header(None),
                      x_device_token: Optional[str] = Header(None)):
    """מוריד/מציג את ה-PDF של חשבונית שנקלטה (לתצוגה מקדימה בקונסולה)."""
    _require_admin_or_device(x_admin_key, x_device_token)
    inv = db.invoice_get(iid, with_pdf=True)
    if not inv or not inv.get("pdf_b64"):
        raise HTTPException(404, "חשבונית/קובץ לא נמצא")
    import base64 as _b64
    pdf = _b64.b64decode(inv["pdf_b64"])
    fn = inv.get("filename") or f"invoice-{inv.get('doc_number') or iid}.pdf"
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{fn}"',
                             "Cache-Control": "no-store"})


class InvoiceSendIn(BaseModel):
    phone: str
    caption: str = ""


@app.post("/api/admin/invoices/{iid}/send")
def admin_invoice_send(iid: int, body: InvoiceSendIn,
                       x_admin_key: Optional[str] = Header(None)):
    """שולח את ה-PDF של החשבונית ללקוח בוואטסאפ (חלון 24ש נאכף בצד wa)."""
    _require_admin(x_admin_key)
    inv = db.invoice_get(iid, with_pdf=True)
    if not inv or not inv.get("pdf_b64"):
        raise HTTPException(404, "חשבונית/קובץ לא נמצא")
    import base64 as _b64
    pdf = _b64.b64decode(inv["pdf_b64"])
    fn = inv.get("filename") or f"invoice-{inv.get('doc_number') or iid}.pdf"
    cap = (body.caption or "").strip() or "מצורף עותק החשבונית. Green Mobile 🟢"
    import wa
    try:
        res = wa.send_document(body.phone, pdf, filename=fn, caption=cap)
    except wa.WaError as e:
        raise HTTPException(502, str(e))
    return res


@app.post("/api/admin/invoices/capture")
def admin_invoices_capture(probe: int = 0, reset: int = 0,
                           x_admin_key: Optional[str] = Header(None)):
    """הפעלה ידנית של קליטת חשבוניות ממייל. probe=1 — אבחון בלבד (מה בתיבה).
    reset=1 — מוחק את כל מה שנקלט וקולט מחדש (לכיוונון פענוח)."""
    _require_admin(x_admin_key)
    import invoice_capture
    if not invoice_capture.configured():
        return {"ok": False, "reason": "חסר INVOICE_IMAP_USER/INVOICE_IMAP_PASS ב-env"}
    if probe:
        return invoice_capture.probe()
    cleared = db.invoices_reset() if reset else 0
    out = invoice_capture.capture()
    if reset:
        out["cleared"] = cleared
    return out


def _invoice_capture_job():
    """קליטת חשבוניות ממייל הקופה — כל 10 דק' (אם מוגדר IMAP)."""
    try:
        import invoice_capture
        if invoice_capture.configured():
            r = invoice_capture.capture()
            if r.get("new"):
                logger.info("invoice capture: %s new", r.get("new"))
    except Exception as e:  # noqa: BLE001
        logger.warning("invoice capture job error: %s", e)


@app.delete("/api/admin/orders/{oid}")
def admin_order_trash(oid: int, x_admin_key: Optional[str] = Header(None)):
    """העברת הזמנה לפח (לא מחיקה סופית) — מותר רק על בוטלו/נכשל/הוחזר."""
    _require_admin(x_admin_key)
    import requests as _rq
    base, k, s = _wc_creds()
    cur = _rq.get(f"{base}/wp-json/wc/v3/orders/{oid}", auth=(k, s), timeout=30)
    if not cur.ok:
        raise HTTPException(404, "הזמנה לא נמצאה")
    st = cur.json().get("status")
    if st not in ("cancelled", "failed", "refunded"):
        raise HTTPException(400, f"לפח אפשר להעביר רק הזמנות שבוטלו/נכשלו (הסטטוס: {st})")
    r = _rq.delete(f"{base}/wp-json/wc/v3/orders/{oid}", auth=(k, s), timeout=30)  # בלי force = פח
    if not r.ok:
        raise HTTPException(502, "ההעברה לפח נכשלה")
    return {"ok": True}


def _wp_app_auth():
    """Basic Auth של WP Application Password — לקריאות גשר ה-Cargo (gm-cargo/v1)."""
    u, p = os.getenv("WP_USERNAME", ""), os.getenv("WP_APP_PASSWORD", "")
    if not (u and p):
        raise HTTPException(502, "חיבור WP (App Password) לא מוגדר")
    return (u, p)


class CargoCreateIn(BaseModel):
    pickup: bool = False        # נק׳ איסוף (shipping_type=2)
    double: bool = False        # משלוח כפול


_STATUS_NOTIFY_TPL = {                       # סטטוס → template מאושר (2 פרמטרים)
    "shipping-stage": "order_update_distribution",
    "send-cargo": "order_update_distribution",
    "order-ready": "order_ready_for_pickup",
    "tlv-pickup": "messege_tlv_pickup",
}


def _il_phone(raw) -> str:
    d = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if d.startswith("972"):
        return d
    if d.startswith("0"):
        return "972" + d[1:]
    if len(d) == 9 and d.startswith("5"):
        return "972" + d
    return d


def bot_confirm_received(num):
    """לקוח לחץ 'קיבלתי את ההזמנה' → מסמן 'נמסרה' + שולח חוו"ד (דדופ עם זרימת Cargo)."""
    try:
        base, k, s = _wc_creds()
        import requests as _rq
        r = _rq.get(f"{base}/wp-json/wc/v3/orders", params={"search": str(num), "per_page": 3},
                    auth=(k, s), timeout=20)
        orders = [o for o in (r.json() if r.ok else []) if str(o.get("number")) == str(num)]
        if not orders:
            return False
        o = orders[0]
        oid = o.get("id")
        if o.get("status") not in ("delivered", "completed"):
            try:
                _rq.put(f"{base}/wp-json/wc/v3/orders/{oid}", json={"status": "delivered"},
                        auth=(k, s), timeout=20)
            except Exception:  # noqa: BLE001
                pass
        # פעולת לקוח מפורשת — שולחים חוו"ד תמיד (לא מגודר ב-WA_SEND_REVIEW), בדדופ עם
        # זרימת Cargo. מחזיר True רק אם חוו"ד נשלחה בקריאה זו (כדי שהבוט לא יוסיף תודה).
        if not db.sales_state_get(f"review_sent:{num}"):
            b = o.get("billing") or {}
            ph = _il_phone(b.get("phone"))
            if len(ph) >= 11:
                import wa
                wa.send_review_template(ph, b.get("first_name") or "", str(num), "נמסרה")
                db.sales_state_set(f"review_sent:{num}", "1")
                return True
        return False
    except Exception as e:  # noqa: BLE001
        logger.warning("confirm received failed for %s: %s", num, e)
        return False


def _status_notify_job():
    """עוקב אחרי שינויי סטטוס ושולח ללקוח את ה-template המאושר המתאים (בהפצה/מוכן
    לאיסוף/נק' מסירה) נייטיב — מחליף את זרימות הסטטוס של קונקטופ. ריצה ראשונה רק
    רושמת סטטוסים (בלי לשלוח — מונע backfill). מאחורי דגל WA_SEND_STATUS_AUTO.
    דדופ פר הזמנה+סטטוס. 'delivered' לא כאן (מטופל ב-cargo_delivery → חוו"ד)."""
    try:
        base, k, s = _wc_creds()
        import requests as _rq
        r = _rq.get(f"{base}/wp-json/wc/v3/orders",
                    params={"per_page": 50, "orderby": "modified", "order": "desc"},
                    auth=(k, s), timeout=40)
        orders = r.json() if r.ok else []
    except Exception as e:  # noqa: BLE001
        logger.warning("status notify fetch failed: %s", e)
        return
    init = db.sales_state_get("status_notify_init")
    enabled = os.getenv("WA_SEND_STATUS_AUTO", "0") == "1"
    for o in (orders or []):
        if isinstance(o, dict):
            _notify_order_status(o, enabled=enabled, init=init)
    if not init:
        db.sales_state_set("status_notify_init", datetime.now().isoformat(timespec="seconds"))


def _classify_cancellation(o: dict) -> str:
    """מסווג ביטול הזמנה: 'abandoned' (אי-תשלום) מול 'real_cancel' (חוסר מלאי/ביטול אמיתי).
    משקף את לוגיקת ה-CF worker (classify-cancellation) כדי שה-cutover ישנה רק את הצינור
    (קונקטופ→Meta ישיר) ולא את התוצאה: לא שולם ובוטל מהר/היה pending → abandoned."""
    has_pay = bool(str(o.get("transaction_id") or "").strip()) or bool(
        o.get("date_paid") or o.get("date_paid_gmt"))
    mins = -1
    try:
        cs = (o.get("date_created") or "").replace("Z", "")
        ms = (o.get("date_modified") or "").replace("Z", "")
        if cs and ms:
            mins = round((datetime.fromisoformat(ms) - datetime.fromisoformat(cs)).total_seconds() / 60)
    except Exception:  # noqa: BLE001
        mins = -1
    return "abandoned" if (not has_pay and (0 <= mins <= 30)) else "real_cancel"


def _notify_order_status(o, enabled=None, init="__fetch__"):
    """שולח ללקוח template סטטוס מאושר אם הסטטוס *השתנה* (בהפצה/מוכן לאיסוף/נק' מסירה).
    משמש גם ב-job (כל 3 דק') וגם ב-webhook (מיידי). מגודר WA_SEND_STATUS_AUTO + דדופ
    פר הזמנה+סטטוס. ריצה ראשונה (init=None) רק רושמת — מונע backfill.
    ביטול (cancelled): מגודר בדגל *נפרד* WA_SEND_CANCEL_AUTO — שולח native את תבנית
    הביטול המתאימה (cart_recovery/order_cancelled_stock), מחליף את נתיב קונקטופ."""
    num = str(o.get("number") or "")
    if not num:
        return
    st = o.get("status")
    if enabled is None:
        enabled = os.getenv("WA_SEND_STATUS_AUTO", "0") == "1"
    if init == "__fetch__":
        init = db.sales_state_get("status_notify_init")
    key = f"order_last_status:{num}"
    last = db.sales_state_get(key)
    db.sales_state_set(key, st)
    if not init or last is None or last == st:
        return                               # ריצה ראשונה / לא ידוע / ללא שינוי → דלג
    # --- ביטול הזמנה native (דגל נפרד, רדום עד cutover קונקטופ — מונע כפילות) ---
    if st == "cancelled":
        if os.getenv("WA_SEND_CANCEL_AUTO", "0").strip() != "1":
            return
        if db.sales_state_get(f"status_sent:{num}:cancelled"):
            return
        b = o.get("billing") or {}
        phone = _il_phone(b.get("phone"))
        if len(phone) < 11:
            return
        name = (b.get("first_name") or "").strip()
        cls = _classify_cancellation(o)
        try:
            import wa
            if cls == "abandoned":
                items = o.get("line_items") or []
                prod = (items[0].get("name") if items else "") or "המוצר שהזמנת"
                wa.send_cancel_template(phone, name, str(prod)[:200], "cart_recovery")
                tplname = "cart_recovery"
            else:
                wa.send_cancel_template(phone, name, num, "order_cancelled_stock")
                tplname = "order_cancelled_stock"
            db.sales_state_set(f"status_sent:{num}:cancelled", "1")
            logger.info("cancel notify %s -> %s (%s)", num, cls, tplname)
        except Exception as e:  # noqa: BLE001
            logger.warning("cancel notify send failed %s: %s", num, e)
        return
    tpl = _STATUS_NOTIFY_TPL.get(st)
    if not tpl or not enabled or db.sales_state_get(f"status_sent:{num}:{st}"):
        return
    b = o.get("billing") or {}
    phone = _il_phone(b.get("phone"))
    if len(phone) < 11:
        return
    try:
        import wa
        wa.send_status_template(phone, (b.get("first_name") or "").strip(), num, tpl)
        db.sales_state_set(f"status_sent:{num}:{st}", "1")
        logger.info("status notify %s -> %s (%s)", num, st, tpl)
    except Exception as e:  # noqa: BLE001
        logger.warning("status notify send failed %s: %s", num, e)


def _cargo_delivery_sync_job():
    """סנכרון מסירת Cargo → סטטוס הזמנה (רץ על ה-worker כל 30 דק'):
    הזמנות ב-'shipping-stage' שה-Cargo סימן נמסר (status.number==3):
    • נמסרו לאחרונה (date_modified ≤ 2 ימים) → 'delivered' — מפעיל את הודעת חוות
      הדעת ללקוח (זרימה קיימת) — ואז תזמון +24ש → 'completed'.
    • נמסרו מזמן (catch-up של ישנות) → ישר 'completed', בלי להציף חוו"ד ללקוחות ישנים.
    אידמפוטנטי: ברגע ששונה הסטטוס ההזמנה יוצאת מ-shipping-stage ולא נסרקת שוב."""
    try:
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        import requests as _rq
        base, k, s = _wc_creds()
        r = _rq.get(f"{base}/wp-json/wc/v3/orders",
                    params={"status": "shipping-stage", "per_page": 100,
                            "_fields": "id,number,date_modified_gmt,meta_data,billing"},
                    auth=(k, s), timeout=40)
        if not r.ok:
            return
        now_utc = datetime.utcnow()
        tz = ZoneInfo(cfg.TZ)
        for o in (r.json() or []):
            try:
                meta = {m.get("key"): m.get("value") for m in (o.get("meta_data") or [])}
                cs = _cargo_status(meta)
                if not cs or int(cs.get("num") or 0) != 3:     # 3 = נמסר
                    continue
                oid, onum = o.get("id"), o.get("number")
                recent = False
                try:
                    dmt = datetime.fromisoformat((o.get("date_modified_gmt") or "").replace("Z", ""))
                    recent = (now_utc - dmt).total_seconds() < 2 * 86400
                except Exception:  # noqa: BLE001
                    pass
                if recent:
                    pr = _rq.put(f"{base}/wp-json/wc/v3/orders/{oid}",
                                 json={"status": "delivered"}, auth=(k, s), timeout=30)
                    if not pr.ok:
                        logger.warning("cargo-delivery delivered failed %s: %s", onum, pr.text[:120])
                        continue
                    run_at = (datetime.now(tz) + timedelta(hours=24)).isoformat()
                    db.sched_status_add(oid, "completed", run_at, order_number=str(onum),
                                        status_label="הושלם", created_by="cargo-delivery-auto")
                    logger.info("cargo-delivery: %s -> delivered (+24h completed)", onum)
                    _tg_admin(f"📦 <b>הזמנה נמסרה</b>\n#{onum} → נמסרה (חוו\"ד ללקוח). הושלם בעוד 24ש.")
                    # אחרי cutover קונקטופ: אנחנו שולחים את template הביקורת (כל עוד
                    # קונקטופ חי הוא שולח — לכן מאחורי דגל, ברירת מחדל כבוי, בלי כפילות).
                    if (os.getenv("WA_SEND_REVIEW", "0").strip() == "1"
                            and not db.sales_state_get(f"review_sent:{onum}")):
                        try:
                            bl = o.get("billing") or {}
                            ph = _il_phone(bl.get("phone"))
                            if len(ph) >= 11:
                                import wa
                                wa.send_review_template(ph, bl.get("first_name") or "", onum, "נמסרה")
                                db.sales_state_set(f"review_sent:{onum}", "1")
                        except Exception as _e:  # noqa: BLE001
                            logger.warning("review template send failed %s: %s", onum, _e)
                else:
                    pr = _rq.put(f"{base}/wp-json/wc/v3/orders/{oid}",
                                 json={"status": "completed"}, auth=(k, s), timeout=30)
                    if pr.ok:
                        logger.info("cargo-delivery: %s -> completed (ישן, בלי חוו\"ד)", onum)
            except Exception as e:  # noqa: BLE001
                logger.warning("cargo-delivery order %s: %s", o.get("number"), e)
    except Exception as e:  # noqa: BLE001
        logger.warning("cargo delivery sync error: %s", e)


def _advance_to_shipping(oid: int):
    """אחרי הפקת תווית Cargo — מקדם את ההזמנה ל'בשלב הפצה' (shipping-stage),
    אלא אם היא כבר בסטטוס מתקדם/סופי יותר (לא מורידים אחורה). best-effort:
    כשל כאן לעולם לא מפיל את הפקת התווית. מחזיר את הסטטוס הסופי (או None)."""
    try:
        base, k, s = _wc_creds()
        import requests as _rq
        cur = _rq.get(f"{base}/wp-json/wc/v3/orders/{oid}",
                      params={"_fields": "id,status"}, auth=(k, s), timeout=20)
        st = (cur.json().get("status") if cur.ok else "") or ""
        # ⚠️ 'completed' אינו סטטוס סופי אצל Green Mobile — NewOrder קובע אותו
        # אוטומטית בהנפקת חשבונית (מצב מוקדם). לכן הוא **כן** מתקדם לבהפצה.
        later = {"shipping-stage", "delivered", "order-ready",
                 "tlv-pickup", "cancelled", "refunded"}
        if st in later:
            return st     # כבר בשלב הזה או מעבר לו — לא נוגעים
        r = _rq.put(f"{base}/wp-json/wc/v3/orders/{oid}",
                    json={"status": "shipping-stage"}, auth=(k, s), timeout=30)
        return (r.json().get("status") if r.ok else st)
    except Exception as e:  # noqa: BLE001
        logger.warning("auto shipping-stage failed for %s: %s", oid, e)
        return None


@app.post("/api/admin/orders/{oid}/cargo")
def admin_order_cargo(oid: int, body: CargoCreateIn, x_admin_key: Optional[str] = Header(None)):
    """יצירת משלוח Cargo להזמנה — דרך תוסף הגשר באתר (מפעיל את תוסף Cargo הרשמי)."""
    _require_admin(x_admin_key)
    import requests as _rq
    base, _, _ = _wc_creds()
    auth = _wp_app_auth()
    r = _rq.post(f"{base}/wp-json/gm-cargo/v1/create",
                 json={"order_id": oid,
                       "shipping_type": 2 if body.pickup else 1,
                       "double_delivery": 2 if body.double else 1},
                 auth=auth, headers={"User-Agent": _PP_UA}, timeout=90)
    try:
        j = r.json()
    except Exception:  # noqa: BLE001
        j = {}
    if not r.ok or not j.get("ok"):
        msg = (j.get("message") or j.get("code") or f"שגיאה {r.status_code}")
        logger.warning("cargo create failed for %s: %s %s", oid, r.status_code, str(j)[:300])
        raise HTTPException(502, f"יצירת המשלוח נכשלה: {msg}")
    out = {"ok": True, "existing": j.get("existing"), "shipments": j.get("shipments") or j.get("shipment")}
    try:  # תווית — מנסים מיד; אם נכשל מחזירים בלי, אפשר לבקש שוב
        rl = _rq.get(f"{base}/wp-json/gm-cargo/v1/label/{oid}", auth=auth,
                     headers={"User-Agent": _PP_UA}, timeout=60)
        if rl.ok and rl.json().get("ok"):
            out["pdf"] = rl.json().get("pdf")
            out["status"] = _advance_to_shipping(oid)   # יש תווית → ההזמנה בשלב הפצה
    except Exception:  # noqa: BLE001
        pass
    return out


@app.get("/api/admin/orders/{oid}/cargo-label")
def admin_order_cargo_label(oid: int, x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    import requests as _rq
    base, _, _ = _wc_creds()
    r = _rq.get(f"{base}/wp-json/gm-cargo/v1/label/{oid}", auth=_wp_app_auth(),
                headers={"User-Agent": _PP_UA}, timeout=60)
    try:
        j = r.json()
    except Exception:  # noqa: BLE001
        j = {}
    if not r.ok or not j.get("ok"):
        raise HTTPException(502, "התווית לא זמינה — ודא שקיים משלוח להזמנה")
    new_status = _advance_to_shipping(oid)   # הדפסת תווית → ההזמנה בשלב הפצה
    return {"ok": True, "pdf": j.get("pdf"), "status": new_status}


class OrderNoteIn(BaseModel):
    note: str
    customer_note: bool = False


@app.post("/api/admin/orders/{oid}/note")
def admin_order_note(oid: int, body: OrderNoteIn, x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    import requests as _rq
    if not (body.note or "").strip():
        raise HTTPException(400, "הערה ריקה")
    base, k, s = _wc_creds()
    r = _rq.post(f"{base}/wp-json/wc/v3/orders/{oid}/notes",
                 json={"note": body.note.strip(), "customer_note": bool(body.customer_note)},
                 auth=(k, s), timeout=30)
    if not r.ok:
        raise HTTPException(502, "הוספת ההערה נכשלה")
    return {"ok": True}


class CustomerEditIn(BaseModel):
    first_name: str = ""
    last_name: str = ""
    phone: str = ""
    email: str = ""
    address_1: str = ""          # כתובת חיוב
    city: str = ""               # עיר חיוב
    # כתובת משלוח נפרדת (אופציונלי). אם ship_same=True או שלא נשלחה כתובת משלוח →
    # המשלוח מועתק מהחיוב (התנהגות קודמת, תאימות לאחור).
    ship_same: bool = False
    ship_first_name: str = ""
    ship_last_name: str = ""
    ship_address_1: str = ""
    ship_city: str = ""


@app.post("/api/admin/orders/{oid}/customer")
def admin_order_customer(oid: int, body: CustomerEditIn,
                         x_admin_key: Optional[str] = Header(None)):
    """עריכת פרטי הלקוח בהזמנה (שם/טלפון/אימייל/כתובת חיוב) + כתובת משלוח נפרדת."""
    _require_admin(x_admin_key)
    import requests as _rq
    base, k, s = _wc_creds()
    fn = (body.first_name or "").strip()
    ln = (body.last_name or "").strip()
    billing = {"first_name": fn, "last_name": ln, "phone": (body.phone or "").strip(),
               "email": (body.email or "").strip(), "address_1": (body.address_1 or "").strip(),
               "city": (body.city or "").strip()}
    s_addr = (body.ship_address_1 or "").strip()
    s_city = (body.ship_city or "").strip()
    if body.ship_same or (not s_addr and not s_city):
        # משלוח = חיוב (ברירת מחדל / תאימות לאחור)
        shipping = {"first_name": fn, "last_name": ln,
                    "address_1": billing["address_1"], "city": billing["city"]}
    else:
        shipping = {"first_name": (body.ship_first_name or "").strip() or fn,
                    "last_name": (body.ship_last_name or "").strip() or ln,
                    "address_1": s_addr, "city": s_city}
    r = _rq.put(f"{base}/wp-json/wc/v3/orders/{oid}",
                json={"billing": billing, "shipping": shipping}, auth=(k, s), timeout=45)
    if not r.ok:
        raise HTTPException(502, f"עדכון פרטי הלקוח נכשל ({r.status_code}: {r.text[:150]})")
    o = r.json()
    return {"ok": True, "billing": o.get("billing", {}), "shipping": o.get("shipping", {})}


# ── תשלום כללי (ללא הזמנת WC): קישור PayPlus חופשי מהמגירה ──
class PayQuick(BaseModel):
    desc: str
    amount: float
    name: str = ""
    phone: str = ""
    email: str = ""
    installments: int = 1


@app.post("/api/admin/pay/standalone")
def pay_standalone(body: PayQuick, x_admin_key: Optional[str] = Header(None)):
    """קישור תשלום PayPlus לפריט/שירות כללי — בלי הזמנת WooCommerce."""
    _require_admin(x_admin_key)
    desc = (body.desc or "").strip()
    if not desc or float(body.amount or 0) <= 0:
        raise HTTPException(400, "חסר תיאור או סכום")
    pp = _payplus_link(float(body.amount), f"כללי: {desc[:40]}",
                       {"name": body.name, "phone": body.phone, "email": body.email},
                       payments=body.installments)
    db.sales_state_set(f"payplus_pru:{pp['pru']}", "standalone")
    db.pay_link_add(pp["pru"], desc, float(body.amount), body.name, body.phone)
    return {"pay_link": pp["link"], "pru": pp["pru"],
            "amount": float(body.amount), "desc": desc}


@app.get("/api/admin/pay/status/{pru}")
def pay_standalone_status(pru: str, x_admin_key: Optional[str] = Header(None)):
    """מצב תשלום של קישור כללי — polling מה-frontend בזמן שה-iframe פתוח."""
    _require_admin(x_admin_key)
    v = _payplus_ipn_check(pru)
    if not v:
        return {"paid": False}
    f = _pp_tx_fields(v)
    db.pay_link_mark_paid(pru, f.get("tx"), f.get("approval"), f.get("four_digits"), f.get("brand"))
    return {"paid": True, "tx": f.get("tx"), "approval": f.get("approval"),
            "four_digits": f.get("four_digits"), "brand": f.get("brand")}


@app.get("/api/admin/pay/links")
def pay_links(q: str = "", x_admin_key: Optional[str] = Header(None)):
    """קונסולת התשלומים המהירים — קישורים ידניים עם סטטוס/אישור/4 ספרות וחיפוש."""
    _require_admin(x_admin_key)
    return {"links": db.pay_links_list(q)}


# ── /pay: עמוד נחיתה ציבורי לכפתור התשלום הקבוע בתבנית הוואטסאפ ──
# ConnectOp לא מעבירים פרמטר כפתור דינמי, לכן הכפתור בתבנית מוביל לכאן —
# הלקוח מקליד את מספר ההזמנה (שמופיע בגוף ההודעה) ומועבר לדף PayPlus שלו.
_PAY_HITS: dict = {}   # rate-limit פשוט בזיכרון: ip -> [timestamps]


def _pay_rate_ok(ip: str) -> bool:
    import time as _t
    now = _t.time()
    hits = [t for t in _PAY_HITS.get(ip, []) if now - t < 60]
    hits.append(now)
    _PAY_HITS[ip] = hits
    if len(_PAY_HITS) > 2000:
        _PAY_HITS.clear()
    return len(hits) <= 10


@app.get("/pay")
def pay_landing(err: str = ""):
    from fastapi.responses import HTMLResponse
    msg = ('<div style="color:#dc2626;font-size:13.5px;margin-bottom:10px">'
           'לא נמצאה הזמנה פתוחה עם המספר הזה — בדקו את המספר או דברו איתנו בוואטסאפ</div>') if err else ""
    return HTMLResponse(f"""<!doctype html><html dir="rtl" lang="he"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>תשלום מאובטח — גרין מובייל</title></head>
<body style="margin:0;font-family:-apple-system,Segoe UI,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;background:#f0f4f1">
<form method="get" action="/pay/go" style="background:#fff;border-radius:18px;padding:30px 26px;box-shadow:0 8px 30px rgba(0,0,0,.10);text-align:center;width:min(340px,88vw)">
  <div style="font-size:34px">💚</div>
  <h2 style="margin:8px 0 4px;color:#1f2937;font-size:20px">תשלום מאובטח</h2>
  <div style="color:#6b7280;font-size:13.5px;margin-bottom:16px">הקלידו את מספר ההזמנה שקיבלתם בהודעה</div>
  {msg}
  <input name="order" inputmode="numeric" pattern="[0-9]*" required autofocus placeholder="מספר הזמנה"
    style="width:100%;box-sizing:border-box;text-align:center;font-size:22px;letter-spacing:2px;padding:12px;border-radius:12px;border:1.5px solid #d1d5db;outline-color:#16a34a">
  <button type="submit" style="width:100%;margin-top:12px;padding:13px;border:0;border-radius:12px;background:#16a34a;color:#fff;font-size:16px;font-weight:600;cursor:pointer">המשך לתשלום 💳</button>
  <div style="color:#9ca3af;font-size:11px;margin-top:12px">התשלום מתבצע בדף מאובטח של PayPlus · גרין מובייל</div>
</form></body></html>""")


@app.get("/pay/go")
def pay_go(order: str = "", request: Request = None):
    """מאתר את קישור PayPlus של ההזמנה ומעביר אליו. רק הזמנות GreenOS פתוחות."""
    import requests as _rq
    from fastapi.responses import RedirectResponse
    ip = _client_ip(request) if request else ""
    if not _pay_rate_ok(ip):
        raise HTTPException(429, "יותר מדי ניסיונות — נסו שוב בעוד דקה")
    oid = "".join(ch for ch in (order or "") if ch.isdigit())
    creds = _wc_creds()
    if not (oid and creds):
        return RedirectResponse("/pay?err=1", status_code=302)
    base, k, s = creds
    try:
        r = _rq.get(f"{base}/wp-json/wc/v3/orders/{oid}", auth=(k, s), timeout=25)
        if r.ok:
            o = r.json()
            meta = {m.get("key"): m.get("value") for m in (o.get("meta_data") or [])}
            link = meta.get("greenos_payplus_link") or ""
            # רק הזמנות שלנו שעדיין ממתינות לתשלום — בלי לחשוף דפי תשלום של אחרים
            if link and meta.get("greenos_source") and o.get("status") == "pending":
                return RedirectResponse(link, status_code=302)
    except Exception as e:  # noqa: BLE001
        logger.warning("pay lookup failed for %s: %s", oid, e)
    return RedirectResponse("/pay?err=1", status_code=302)


@app.get("/pay-done")
def pay_done(ok: str = "1"):
    """עמוד הנחיתה בסיום תשלום — נטען בתוך ה-iframe ב-GreenOS ומאותת להורה."""
    from fastapi.responses import HTMLResponse
    good = str(ok) == "1"
    icon = "✓" if good else "✕"
    color = "#16a34a" if good else "#dc2626"
    msg = "התשלום התקבל בהצלחה" if good else "התשלום לא הושלם"
    sub = "אפשר לסגור את החלון — ההזמנה מתעדכנת" if good else "אפשר לנסות שוב או לשלוח קישור ללקוח"
    return HTMLResponse(f"""<!doctype html><html dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>GreenOS</title></head>
<body style="margin:0;font-family:-apple-system,Segoe UI,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;background:#f4f6f8">
<div style="text-align:center;padding:30px">
  <div style="width:74px;height:74px;border-radius:50%;background:{color};color:#fff;font-size:38px;line-height:74px;margin:0 auto 14px">{icon}</div>
  <h2 style="margin:0 0 6px;color:#1f2937">{msg}</h2>
  <div style="color:#6b7280;font-size:14px">{sub}</div>
</div>
<script>try{{ (window.parent!==window?window.parent:window.opener||{{postMessage:function(){{}}}}).postMessage({{type:'payplus-done',ok:{str(good).lower()}}},'*'); }}catch(e){{}}</script>
</body></html>""")


@app.get("/api/admin/wa/order/paystatus/{order_id}")
def wa_order_paystatus(order_id: int, x_admin_key: Optional[str] = Header(None)):
    """בדיקת מצב תשלום של הזמנה — ל-polling מה-frontend בזמן שה-iframe פתוח.
    אם ה-IPN לא הגיע (נדיר), בודק אקטיבית מול PayPlus ומעדכן בעצמו."""
    _require_admin(x_admin_key)
    import requests as _rq
    creds = _wc_creds()
    if not creds:
        raise HTTPException(502, "חיבור WooCommerce לא מוגדר")
    base, k, s = creds
    r = _rq.get(f"{base}/wp-json/wc/v3/orders/{order_id}", auth=(k, s), timeout=30)
    if not r.ok:
        raise HTTPException(404, "הזמנה לא נמצאה")
    o = r.json()
    meta = {m.get("key"): m.get("value") for m in (o.get("meta_data") or [])}
    paid = bool(meta.get("greenos_payplus_tx")) or o.get("status") in ("processing", "completed")
    if not paid:
        pru = meta.get("greenos_payplus_pru") or ""
        verified = _payplus_ipn_check(pru) if pru else None
        if verified:
            f = _pp_tx_fields(verified)
            if _pp_mark_paid(order_id, f):
                paid = True
                meta.update({"greenos_payplus_tx": f.get("tx"),
                             "greenos_payplus_approval": f.get("approval"),
                             "greenos_payplus_4digits": f.get("four_digits"),
                             "greenos_payplus_brand": f.get("brand")})
                o["status"] = "processing"
    return {"order_id": order_id, "status": o.get("status"), "paid": paid,
            "tx": meta.get("greenos_payplus_tx") or "",
            "approval": meta.get("greenos_payplus_approval") or "",
            "four_digits": meta.get("greenos_payplus_4digits") or "",
            "brand": meta.get("greenos_payplus_brand") or ""}


def _wc_pos_anchor(base: str, k: str, s: str) -> int:
    """מוצר עוגן מוסתר (sku GM-POS-ITEM, פרטי) לשורות של פריטי קופה שאינם באתר.
    נוצר חד-פעמית; ה-id נשמר ב-kv. נוצר בפועל 12/06/2026 — id 46880."""
    v = db.sales_state_get("wc_pos_anchor")
    if v and str(v).isdigit():
        return int(v)
    import requests as _rq
    pid = None
    try:
        r = _rq.get(f"{base}/wp-json/wc/v3/products", params={"sku": "GM-POS-ITEM"},
                    auth=(k, s), timeout=30)
        if r.ok and r.json():
            pid = r.json()[0].get("id")
        if not pid:
            r = _rq.post(f"{base}/wp-json/wc/v3/products", auth=(k, s), timeout=40, json={
                "name": "פריט קופה (GreenOS)", "type": "simple", "sku": "GM-POS-ITEM",
                "regular_price": "0", "catalog_visibility": "hidden", "status": "private",
                "description": "מוצר עוגן טכני לשורות הזמנה של פריטי קופה שאינם באתר."})
            pid = r.json().get("id") if r.ok else None
    except Exception as e:  # noqa: BLE001
        logger.warning("pos anchor lookup failed: %s", e)
    if not pid:
        raise HTTPException(502, "מוצר העוגן לפריטי קופה לא זמין")
    db.sales_state_set("wc_pos_anchor", str(pid))
    return int(pid)


class WaOrderItem(BaseModel):
    product_id: int = 0             # parent (או המוצר עצמו אם simple); 0 = פריט קופה שאינו באתר
    variation_id: int = 0
    quantity: int = 1
    price: float = -1               # מחיר ליחידה — חובה לפריט שאינו באתר; דריסה לפריט רגיל
    name: str = ""                  # שם לשורה מותאמת (פריט קופה בלבד)
    sku: str = ""                   # מק"ט קופה — נשמר על השורה


class WaOrderCustomer(BaseModel):
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    phone: str = ""
    email: str = ""
    address_1: str = ""
    city: str = ""


class WaOrderShipTo(BaseModel):
    first_name: str = ""
    last_name: str = ""
    address_1: str = ""
    city: str = ""


class WaOrderCreate(BaseModel):
    phone: str = ""                 # טלפון השיחה (ריק כשמגיעים ממלאי חי/טלפוני)
    customer: WaOrderCustomer
    ship_same: bool = True          # כתובת משלוח = כתובת חיוב
    ship_to: WaOrderShipTo = WaOrderShipTo()
    items: list[WaOrderItem]
    shipping_title: str = ""
    shipping_total: float = 0
    note: str = ""
    payment: str = "none"           # none | link
    installments: int = 1           # תשלומים (לקישור/חיוב)


@app.post("/api/admin/wa/order/create")
def wa_order_create(body: WaOrderCreate, x_admin_key: Optional[str] = Header(None)):
    """יוצר הזמנת WooCommerce חיה (סטטוס pending) ואופציונלית קישור תשלום PayPlus."""
    _require_admin(x_admin_key)
    import requests as _rq
    creds = _wc_creds()
    if not creds:
        raise HTTPException(502, "חיבור WooCommerce לא מוגדר")
    base, k, s = creds
    if not body.items:
        raise HTTPException(400, "אין פריטים")
    line_items = []
    for it in body.items:
        qty = max(1, int(it.quantity))
        if it.product_id:
            li = {"product_id": int(it.product_id), "quantity": qty}
            if it.variation_id:
                li["variation_id"] = int(it.variation_id)
            if it.price is not None and float(it.price) >= 0:
                tot = round(float(it.price) * qty, 2)   # דריסת מחיר ידנית מהטופס
                li["subtotal"] = str(tot)
                li["total"] = str(tot)
        else:
            # פריט קופה שאינו מחובר לאתר — שורה על מוצר העוגן עם שם ומחיר דרוסים
            # (WC דורש הפניית מוצר בכל שורה — woocommerce_rest_required_product_reference)
            if not (it.price and float(it.price) > 0):
                raise HTTPException(400, f"חסר מחיר לפריט '{it.name or it.sku}'")
            tot = round(float(it.price) * qty, 2)
            nm = (it.name or "פריט קופה")[:100] + (f' (מק"ט {it.sku})' if it.sku else "")
            li = {"product_id": _wc_pos_anchor(base, k, s), "quantity": qty,
                  "name": nm, "subtotal": str(tot), "total": str(tot)}
        line_items.append(li)
    cust = body.customer
    billing = {"first_name": cust.first_name, "last_name": cust.last_name,
               "phone": cust.phone or body.phone, "country": "IL"}
    # WC דוחה email/כתובת ריקים (400) — מוסיפים רק אם מולאו
    if (cust.email or "").strip():
        billing["email"] = cust.email.strip()
    if (cust.address_1 or "").strip():
        billing["address_1"] = cust.address_1.strip()
    if (cust.city or "").strip():
        billing["city"] = cust.city.strip()
    if (cust.company or "").strip():
        billing["company"] = cust.company.strip()   # חשבונית על חברה
    payload = {
        "status": "pending",
        "billing": billing,
        "shipping": ({k2: billing.get(k2, "") for k2 in ("first_name", "last_name", "company", "address_1", "city", "country")}
                     if body.ship_same else
                     {"first_name": body.ship_to.first_name or cust.first_name,
                      "last_name": body.ship_to.last_name or cust.last_name,
                      "address_1": body.ship_to.address_1, "city": body.ship_to.city,
                      "country": "IL"}),
        "line_items": line_items,
        "customer_note": body.note or "",
        "meta_data": [{"key": "greenos_source", "value": "whatsapp"},
                      {"key": "greenos_wa_phone", "value": body.phone}],
    }
    if body.shipping_total or body.shipping_title:
        payload["shipping_lines"] = [{"method_id": "flat_rate",
                                      "method_title": body.shipping_title or "משלוח",
                                      "total": str(body.shipping_total or 0)}]
    r = _rq.post(f"{base}/wp-json/wc/v3/orders", json=payload, auth=(k, s), timeout=45)
    if r.status_code not in (200, 201):
        logger.warning("wc order create failed %s: %s", r.status_code, r.text[:300])
        raise HTTPException(502, f"יצירת הזמנה נכשלה ({r.status_code})")
    o = r.json()
    out = {"order_id": o.get("id"), "number": o.get("number"),
           "total": o.get("total"), "currency": o.get("currency_symbol") or "₪",
           "admin_url": f"{base}/wp-admin/post.php?post={o.get('id')}&action=edit",
           "pay_link": ""}
    if body.payment == "link":
        pp = _payplus_link(float(o.get("total") or 0), str(o.get("number")), {
            "name": f"{cust.first_name} {cust.last_name}".strip(),
            "email": cust.email, "phone": cust.phone or body.phone},
            payments=body.installments)
        out["pay_link"] = pp["link"]
        out["pru"] = pp["pru"]
        import wa as _wa
        # איך תישלח ההודעה ללקוח: button = תבנית payment_link עם כפתור (Meta ישיר)
        out["pay_mode"] = "button" if (_wa.meta_direct_ready() or _wa.pay_template_ready()) else "text"
        try:  # מיפוי pru→order ל-IPN + שמירת הקישור על ההזמנה
            db.sales_state_set(f"payplus_pru:{pp['pru']}", str(o["id"]))
            _rq.put(f"{base}/wp-json/wc/v3/orders/{o['id']}",
                    json={"meta_data": [{"key": "greenos_payplus_link", "value": pp["link"]},
                                        {"key": "greenos_payplus_pru", "value": pp["pru"]}]},
                    auth=(k, s), timeout=30)
        except Exception:  # noqa: BLE001
            pass
    return out




class WaSendSure(BaseModel):
    phone: str
    text: str
    name: str = ""          # שם פרטי — לפנייה בתבנית new_message / payment_link
    # הקשר תשלום (אופציונלי): כשמלא ותבנית payment_link מאושרת — נשלחת תבנית
    # עם כפתור URL לחיץ במקום טקסט (פתרון לקישור לא-לחיץ אצל לקוחות חדשים)
    order_number: str = ""
    total: str = ""
    pru: str = ""
    desc: str = ""          # תשלום כללי (ללא הזמנה) — תיאור הפריט לתבנית payment_general


@app.post("/api/admin/wa/send-guaranteed")
def wa_send_guaranteed(body: WaSendSure, x_admin_key: Optional[str] = Header(None)):
    """שליחה מובטחת בוואטסאפ: קישור תשלום → תבנית payment_link עם כפתור (אם מאושרת);
    אחרת בתוך חלון 24ש׳ → הודעה רגילה; מחוץ לחלון → תבנית new_message. עוברת תמיד."""
    _require_admin(x_admin_key)
    import re as _re
    import wa
    phone = _re.sub(r"\D", "", body.phone or "")
    if phone.startswith("0"):
        phone = "972" + phone[1:]
    if len(phone) < 11:
        raise HTTPException(400, "מספר טלפון לא תקין")
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "הודעה ריקה")
    if body.pru and wa.meta_direct_ready():
        # המסלול המועדף: Meta ישיר — תבנית עם הקישור האישי בכפתור.
        # עם מס׳ הזמנה → payment_link; תשלום כללי (מהמגירה) → payment_general.
        try:
            # גם תשלום כללי (מהמגירה) יוצא בתבנית payment_link — הוראת אסי 12/06:
            # התיאור נכנס בפרמטר של מס׳ ההזמנה ("הזמנה מס׳ {desc}")
            wa.send_pay_template_direct(phone, body.name,
                                        body.order_number or (body.desc or "כללי"),
                                        body.total, body.pru)
            return {"sent": True, "via": "pay-template", "phone": phone}
        except wa.WaError as e:
            logger.warning("meta-direct pay send failed (%s) — falling back", e)
    if body.pru and wa.pay_template_ready():
        try:
            wa.send_pay_template(phone, body.name, body.order_number, body.total, body.pru)
            return {"sent": True, "via": "pay-template", "phone": phone}
        except wa.WaError as e:
            logger.warning("pay-template send failed (%s) — falling back", e)
    try:
        r = wa.send_reply(phone, text)
        if r.get("sent"):
            return {"sent": True, "via": "text", "phone": phone}
    except wa.WaError as e:
        # חסימות מכוונות (test/ping) נשארות שגיאה; כשל קריאת שיחה → ננסה תבנית
        if "test/ping" in str(e):
            raise HTTPException(400, str(e))
        logger.info("send-guaranteed: direct path failed (%s) — using template", e)
    except Exception as e:  # noqa: BLE001
        logger.info("send-guaranteed: direct path error (%s) — using template", e)
    r = _wa_guard(wa.send_template, phone, body.name or "לקוח/ה יקר/ה", text)
    return {"sent": True, "via": "template", "phone": phone}


# ── שליחה מתוזמנת: "שלח בשעה X" — רץ בצד שרת (GreenOS תמיד פעיל), לא תלוי בסשן/אורי ──
class WaSchedule(BaseModel):
    phone: str
    text: str
    at: str                 # "HH:MM" (שעון ישראל) או ISO מלא
    name: str = ""
    order_number: str = ""
    total: str = ""
    pru: str = ""
    desc: str = ""


def _resolve_send_at(at: str) -> str:
    """ממיר "HH:MM" לזמן הקרוב (היום/מחר) בשעון ישראל. ISO מוחזר כמו שהוא."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    import re as _re
    tz = ZoneInfo(cfg.TZ)
    now = datetime.now(tz)
    s = (at or "").strip()
    m = _re.match(r"^(\d{1,2}):(\d{2})$", s)
    if m:
        t = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if t <= now:
            t = t + timedelta(days=1)
        return t.isoformat()
    return s


@app.post("/api/admin/wa/schedule")
def wa_schedule(body: WaSchedule, x_admin_key: Optional[str] = Header(None)):
    """מתזמן שליחת הודעה ללקוח בשעה מסוימת — השרת ישלח בפועל גם אחרי שהסשן ייסגר."""
    _require_admin(x_admin_key)
    import re as _re
    phone = _re.sub(r"\D", "", body.phone or "")
    if phone.startswith("0"):
        phone = "972" + phone[1:]
    if len(phone) < 11:
        raise HTTPException(400, "מספר טלפון לא תקין")
    if not (body.text or "").strip():
        raise HTTPException(400, "הודעה ריקה")
    send_at = _resolve_send_at(body.at)
    sid = db.wa_sched_add(phone, body.text, send_at, body.name, body.order_number,
                          body.total, body.pru, body.desc, created_by=_actor_name(x_admin_key, None))
    return {"scheduled": True, "id": sid, "send_at": send_at, "phone": phone}


@app.get("/api/admin/wa/scheduled")
def wa_scheduled_list(x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    return {"scheduled": db.wa_sched_pending()}


@app.delete("/api/admin/wa/scheduled/{sid}")
def wa_scheduled_cancel(sid: int, x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    return {"canceled": db.wa_sched_cancel(sid)}


def _wa_scheduled_job():
    """כל 30ש: שולח הודעות מתוזמנות שהגיע זמנן (דרך השליחה המובטחת)."""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now_iso = datetime.now(ZoneInfo(cfg.TZ)).isoformat()
        for s in db.wa_sched_due(now_iso):
            try:
                res = wa_send_guaranteed(WaSendSure(
                    phone=s["phone"], text=s["text"], name=s.get("name") or "",
                    order_number=s.get("order_number") or "", total=s.get("total") or "",
                    pru=s.get("pru") or "", desc=s.get("descr") or ""),
                    x_admin_key=cfg.ADMIN_PASSWORD)
                db.wa_sched_mark(s["id"], "sent", via=res.get("via", ""))
                logger.info("scheduled send fired: #%s -> %s", s["id"], s["phone"])
                _tg_admin(f"✅ <b>הודעה מתוזמנת נשלחה</b>\nל-{s['phone']}:\n{(s['text'] or '')[:200]}")
            except Exception as e:  # noqa: BLE001
                db.wa_sched_mark(s["id"], "failed", err=str(e))
                logger.warning("scheduled send #%s failed: %s", s["id"], e)
    except Exception as e:  # noqa: BLE001
        logger.warning("wa_scheduled job error: %s", e)


def _scheduled_status_job():
    """כל 30ש: מחיל שינויי-סטטוס הזמנה מתוזמנים שהגיע זמנם (רץ בשרת — אמין)."""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        import requests as _rq
        now_iso = datetime.now(ZoneInfo(cfg.TZ)).isoformat()
        due = db.sched_status_due(now_iso)
        if not due:
            return
        base, k, s = _wc_creds()
        for it in due:
            oid = it["order_id"]
            st = it["status"]
            try:
                r = _rq.put(f"{base}/wp-json/wc/v3/orders/{oid}",
                            json={"status": st}, auth=(k, s), timeout=45)
                if not r.ok:
                    raise RuntimeError(f"{r.status_code}: {r.text[:120]}")
                db.sched_status_mark(it["id"], "done")
                logger.info("scheduled status fired: #%s order %s -> %s", it["id"], oid, st)
                lbl = it.get("status_label") or st
                onum = it.get("order_number") or oid
                _tg_admin(f"✅ <b>סטטוס הזמנה שונה (מתוזמן)</b>\nהזמנה #{onum} → {lbl}")
            except Exception as e:  # noqa: BLE001
                db.sched_status_mark(it["id"], "failed", err=str(e))
                logger.warning("scheduled status #%s failed: %s", it["id"], e)
                _tg_admin(f"⚠️ <b>תזמון סטטוס נכשל</b>\nהזמנה #{it.get('order_number') or oid}: {e}")
    except Exception as e:  # noqa: BLE001
        logger.warning("scheduled_status job error: %s", e)


class WaCharge(BaseModel):
    order_id: int
    card_number: str
    card_exp: str                   # MMYY או MM/YY
    cvv: str = ""
    holder_id: str = ""             # ת.ז. בעל הכרטיס (נדרש בעסקות טלפוניות בישראל)
    holder_name: str = ""
    installments: int = 1


@app.post("/api/admin/wa/order/charge")
def wa_order_charge(body: WaCharge, x_admin_key: Optional[str] = Header(None)):
    """חיוב טלפוני ישיר (MOTO) דרך PayPlus Transactions/Charge — בלי מעבר חיצוני.
    פרטי הכרטיס עוברים ל-PayPlus בלבד ולא נשמרים אצלנו."""
    _require_admin(x_admin_key)
    import requests as _rq
    term = os.getenv("PAYPLUS_TERMINAL_UID", "").strip()
    if not term:
        raise HTTPException(400, "חיוב טלפוני עוד לא מוגדר — חסר PAYPLUS_TERMINAL_UID")
    creds = _wc_creds()
    base, k, sct = creds
    r = _rq.get(f"{base}/wp-json/wc/v3/orders/{body.order_id}", auth=(k, sct), timeout=30)
    if not r.ok:
        raise HTTPException(404, "הזמנה לא נמצאה")
    o = r.json()
    amount = float(o.get("total") or 0)
    exp = body.card_exp.replace("/", "").replace(" ", "")
    payload = {
        "terminal_uid": term,
        "amount": round(amount, 2),
        "currency_code": "ILS",
        "credit_card_number": body.card_number.replace(" ", "").replace("-", ""),
        "card_date_mmyy": exp,
        "payments": max(1, int(body.installments or 1)),
        "more_info": f"GreenOS order {o.get('number')}",
        "customer_name": body.holder_name or f"{o['billing'].get('first_name','')} {o['billing'].get('last_name','')}".strip(),
    }
    if body.cvv:
        payload["cvv"] = body.cvv
    if body.holder_id:
        payload["identification_number"] = body.holder_id
    cr = _rq.post(f"{PAYPLUS_BASE}/Transactions/Charge",
                  headers=_payplus_headers(), json=payload, timeout=60)
    try:
        j = cr.json()
    except Exception:  # noqa: BLE001
        j = {}
    res = (j.get("results") or {})
    data = (j.get("data") or {})
    approved = cr.status_code == 200 and res.get("status") == "success"
    if not approved:
        logger.warning("payplus charge failed %s: %s", cr.status_code, cr.text[:300])
        raise HTTPException(502, f"החיוב נדחה: {res.get('description') or cr.status_code}")
    tx = data.get("transaction_uid") or data.get("number") or ""
    # עדכון ההזמנה לשולם
    try:
        _rq.put(f"{base}/wp-json/wc/v3/orders/{body.order_id}",
                json={"status": "processing", "set_paid": True,
                      "meta_data": [{"key": "greenos_payplus_tx", "value": str(tx)}]},
                auth=(k, sct), timeout=30)
    except Exception as e:  # noqa: BLE001
        logger.warning("order paid-update failed: %s", e)
    return {"ok": True, "transaction": tx, "amount": amount,
            "approval": data.get("approval_number") or data.get("voucher_number") or ""}


# ── Web Push (PWA) — התראות וואטסאפ כשהאפליקציה סגורה ──
class WaPushSub(BaseModel):
    sub: dict
    ua: str = ""


@app.get("/api/admin/wa/push/key")
def wa_push_key(x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    import wa_push
    return {"key": wa_push.VAPID_PUBLIC, "devices": len(db.wa_push_subs())}


@app.post("/api/admin/wa/push/subscribe")
def wa_push_subscribe(body: WaPushSub, x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    import wa_push
    return wa_push.subscribe(body.sub, body.ua)


@app.post("/api/admin/wa/push/test")
def wa_push_test(x_admin_key: Optional[str] = Header(None)):
    """שולח push בדיקה לכל המכשירים הרשומים — לאימות מהאייפון."""
    _require_admin(x_admin_key)
    import wa_push
    n = wa_push.send_to_all("GreenOS ✅", "התראות הוואטסאפ פעילות במכשיר הזה")
    return {"sent": n}


# ──────────────────────────────────────────────────────────────
# Frontend (SPA)
# ──────────────────────────────────────────────────────────────
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")


@app.get("/")
def index():
    idx = os.path.join(_static_dir, "index.html")
    if os.path.exists(idx):
        # no-cache: ה-PWA באייפון נוטה להגיש HTML ישן מה-cache — מאלץ revalidation
        # בכל פתיחה (ETag → 304 כשאין שינוי, זול). בלי זה דיפלויים לא מגיעים לטלפון.
        return FileResponse(idx, headers={"Cache-Control": "no-cache"})
    return JSONResponse({"app": cfg.APP_TITLE, "note": "frontend not built yet"})


@app.get("/sw.js")
def service_worker():
    """ה-service worker חייב להיות מוגש מהשורש כדי לקבל scope '/'. """
    return FileResponse(os.path.join(_static_dir, "sw.js"),
                        media_type="application/javascript")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
