"""
Transfers app — FastAPI backend.
מגיש את ה-SPA, חושף API לקליטה/לוח-בהעברה, ומריץ poller + התראות ברקע (APScheduler).
"""

import os
import logging
from typing import Optional
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import FileResponse, JSONResponse
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


@app.on_event("startup")
def _startup():
    db.init_db()
    logger.info("DB ready (%s)", "Postgres" if cfg.DATABASE_URL else "SQLite")
    # פיתוח מקומי: DISABLE_BACKGROUND_JOBS=1 מכבה את כל עבודות הרקע (פולר/סנכרונים),
    # כדי לא להעמיס על הטוקן המשותף במקביל לפרודקשן. ה-API וה-UI עובדים רגיל.
    if os.getenv("DISABLE_BACKGROUND_JOBS", "").strip() in ("1", "true", "yes"):
        logger.warning("background jobs DISABLED (DISABLE_BACKGROUND_JOBS)")
        return
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
    if _is_stale(db.catalog_meta().get("updated_at"), hours=6):
        scheduler.add_job(_catalog_refresh_job, "date", id="catalog_initial",
                          run_date=datetime.now() + timedelta(seconds=150))
    else:
        logger.info("catalog fresh — skipping initial refresh")
    # ניטור טוקן ConnectOp: בדיקה יומית 09:30 + בדיקת boot (לוג בלבד כשהכל תקין)
    scheduler.add_job(_token_watch_job, "cron", id="token_watch",
                      hour=9, minute=30, max_instances=1)
    scheduler.add_job(_token_watch_job, "date", id="token_watch_initial",
                      run_date=datetime.now() + timedelta(seconds=45))
    scheduler.start()
    # סבב ראשוני סינכרוני קצר כדי שהלוח לא יהיה ריק בהפעלה
    try:
        poller.poll_once()
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


def _require_admin_or_device(x_admin_key, x_device_token):
    """גישה למנהל (סיסמה) או למכשיר סניף מאושר — לפיצ'רים שפתוחים לסניפים
    (מלאי חי, בקשת משיכה). הניהול המלא נשאר בסיסמה בלבד."""
    if not cfg.ADMIN_PASSWORD or (x_admin_key or "") == cfg.ADMIN_PASSWORD:
        return
    d = db.device_get(x_device_token or "") if x_device_token else None
    if d and d.get("status") == "approved":
        return
    raise HTTPException(401, "admin or approved device required")


@app.get("/health")
def health():
    return {"ok": True, "stats": db.stats()}


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
    _require_admin_or_device(x_admin_key, x_device_token)
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
    return {"lines": lines, "branches": [{"id": b, "name": cfg.branch_name(b)} for b in (1, 2, 3, 4)]}


@app.post("/api/admin/plan")
def admin_plan_add(body: PlanAdd, x_admin_key: Optional[str] = Header(None), x_device_token: Optional[str] = Header(None)):
    _require_admin_or_device(x_admin_key, x_device_token)
    ids = db.plan_add([l.model_dump() for l in body.lines])
    return {"added": len(ids), "ids": ids}


class PlanReplace(BaseModel):
    product_id: str
    lines: list[PlanLine]


@app.post("/api/admin/plan/replace")
def admin_plan_replace(body: PlanReplace, x_admin_key: Optional[str] = Header(None)):
    """מחליף את שורות התוכנית למוצר (עריכה/הסרה). lines ריק = הסרת הבקשה."""
    _require_admin(x_admin_key)
    return {"count": db.plan_replace_product(body.product_id, [l.model_dump() for l in body.lines])}


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
                "parent_id": p.get("parent_id"),
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
                    cands.append({"sku": v.get("sku") or "", "price": v.get("price"),
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
            try:
                db.device_touch(tok, request.query_params.get("branch_id"))
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


@app.get("/api/admin/wa/conversations")
def wa_conversations(archived: int = 0, x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    import wa
    return {"conversations": _wa_guard(wa.list_conversations,
                                       include_archived=bool(archived))}


@app.get("/api/admin/wa/thread/{phone}")
def wa_thread(phone: str, limit: int = 60, x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    import wa
    return _wa_guard(wa.get_thread, phone, limit=min(limit, 200))


@app.post("/api/admin/wa/send")
def wa_send(body: WaSend, x_admin_key: Optional[str] = Header(None)):
    """מענה אנושי; אם מחוץ לחלון 24ש — מחזיר needs_template (לא שולח)."""
    _require_admin(x_admin_key)
    import wa
    return _wa_guard(wa.send_reply, body.phone, body.text)


@app.post("/api/admin/wa/send-template")
def wa_send_template(body: WaTemplate, x_admin_key: Optional[str] = Header(None)):
    """שליחה מחוץ לחלון: template מאושר new_message (שם, גוף בשורה אחת)."""
    _require_admin(x_admin_key)
    import wa
    return _wa_guard(wa.send_template, body.phone, body.name, body.body)


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


# ──────────────────────────────────────────────────────────────
# Frontend (SPA)
# ──────────────────────────────────────────────────────────────
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")


@app.get("/")
def index():
    idx = os.path.join(_static_dir, "index.html")
    if os.path.exists(idx):
        return FileResponse(idx)
    return JSONResponse({"app": cfg.APP_TITLE, "note": "frontend not built yet"})


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
