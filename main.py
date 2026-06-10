"""
Transfers app — FastAPI backend.
מגיש את ה-SPA, חושף API לקליטה/לוח-בהעברה, ומריץ poller + התראות ברקע (APScheduler).
"""

import os
import logging
from typing import Optional
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Header
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
    # אינדקס סריאל→מוצר: סבב baseline כל 3 שעות + ריצה ראשונית ~60ש' אחרי עליה
    scheduler.add_job(_serial_sync_job, "interval", hours=3, id="serial_sync", max_instances=1)
    scheduler.add_job(_serial_sync_job, "date", id="serial_sync_initial",
                      run_date=datetime.now() + timedelta(seconds=60))
    # איזון מלאי: פעמיים ביום (כך שתמיד נופל בתוך שעות הפעילות של איזה יום) — תחילת/סוף יום
    scheduler.add_job(_rebalance_job, "cron", id="rebalance_am", hour=8, minute=30, max_instances=1)
    scheduler.add_job(_rebalance_job, "cron", id="rebalance_pm", hour=21, minute=0, max_instances=1)
    # איסוף מכירות מצטבר: כל 3 שעות (מושך רק מסמכים חדשים מאז ה-cursor) + ריצה ראשונית
    scheduler.add_job(_sales_ingest_job, "interval", hours=3, id="sales_ingest", max_instances=1)
    scheduler.add_job(_sales_ingest_job, "date", id="sales_ingest_initial",
                      run_date=datetime.now() + timedelta(seconds=120))
    # הורדות מלאי מרלוג: כל 3 שעות + ריצה ראשונית (~180ש', מרווח מהשאר)
    scheduler.add_job(_removals_ingest_job, "interval", hours=3, id="removals_ingest", max_instances=1)
    scheduler.add_job(_removals_ingest_job, "date", id="removals_initial",
                      run_date=datetime.now() + timedelta(seconds=180))
    # קטלוג מוצרים ל-DB: רענון כל 6 שעות + ריצה ראשונית (~150ש' אחרי עליה, מרווח מ-sales)
    scheduler.add_job(_catalog_refresh_job, "interval", hours=6, id="catalog_refresh", max_instances=1)
    scheduler.add_job(_catalog_refresh_job, "date", id="catalog_initial",
                      run_date=datetime.now() + timedelta(seconds=150))
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


# ── שידור בקשת העברה למסך הסניף ──
class BroadcastIn(BaseModel):
    branch_id: int


@app.post("/api/admin/broadcast")
def admin_broadcast(body: BroadcastIn, x_admin_key: Optional[str] = Header(None)):
    """משדר את בקשת ההעברה של סניף המקור למסך הקליטה שלו (תצוגה בלבד)."""
    _require_admin(x_admin_key)
    db.broadcast_set(body.branch_id)
    return {"ok": True, "lines": len(db.plan_for_branch(body.branch_id))}


@app.get("/api/admin/broadcasts")
def admin_broadcasts(x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    return {"branches": db.broadcast_branches()}


@app.get("/api/broadcast")
def get_broadcast(branch_id: int):
    """ציבורי — מסך הסניף בודק אם יש בקשת העברה משודרת אליו (תצוגה בלבד)."""
    at = db.broadcast_get(branch_id)
    lines = db.plan_for_branch(branch_id)
    for ln in lines:
        ln["from_name"] = cfg.branch_name(ln.get("from_branch"))
        ln["to_name"] = cfg.branch_name(ln.get("to_branch"))
    active = bool(at) and len(lines) > 0
    return {"active": active, "broadcast_at": at,
            "from_name": cfg.branch_name(branch_id), "lines": lines}


@app.post("/api/broadcast/dismiss")
def dismiss_broadcast(body: BroadcastIn):
    """ציבורי — הסניף מאשר שראה את הבקשה."""
    db.broadcast_clear(body.branch_id)
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
def admin_plan_add(body: PlanAdd, x_admin_key: Optional[str] = Header(None)):
    _require_admin(x_admin_key)
    return {"added": db.plan_add([l.model_dump() for l in body.lines])}


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
def admin_live_catalog(x_admin_key: Optional[str] = Header(None)):
    """קטלוג מצומצם לצמצום תוך-כדי-הקלדה בצד הלקוח (שמות/ברקודים — לא מלאי)."""
    _require_admin(x_admin_key)
    meta = db.catalog_meta()
    return {"items": db.catalog_light(), "updated_at": meta.get("updated_at"),
            "count": meta.get("count")}


@app.get("/api/admin/live-search/serial")
def admin_live_serial(q: str, x_admin_key: Optional[str] = Header(None)):
    """איתור מוצר לפי מספר סידורי (מאינדקס סריאל→מוצר). אין ל-NewOrder חיפוש הפוך."""
    _require_admin(x_admin_key)
    rec = db.serial_product((q or "").strip())
    if not rec:
        return {"found": False}
    return {"found": True, "serial": (q or "").strip(),
            "product_id": rec.get("product_id"), "product_name": rec.get("product_name")}


# micro-cache קצרצר כדי לרכך לחיצות כפולות/כמה מסכי ניהול במקביל — עדיין "חי" לכל דבר
_live_stock_cache: dict = {}
_LIVE_STOCK_TTL_SEC = 20


@app.get("/api/admin/live-stock/{pid}")
def admin_live_stock(pid: str, serials: int = 0, fresh: int = 0,
                     x_admin_key: Optional[str] = Header(None)):
    """מלאי חי לפי סניף ישירות מהקופה, ואופציונלית גם היחידות הסריאליות (ספק+אחריות)."""
    _require_admin(x_admin_key)
    import time as _time
    key = (str(pid), bool(serials))
    hit = _live_stock_cache.get(key)
    if hit and not fresh and (_time.time() - hit[0]) < _LIVE_STOCK_TTL_SEC:
        return hit[1]
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
    _live_stock_cache[key] = (_time.time(), out)
    return out


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
