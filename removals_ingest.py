"""
איסוף הורדות מלאי מסניף מרלוג (branch 3, operationType=2) למאגר מקומי.

מרלוג עובד בחצי-קופה (ניהול מלאי + קופה ידנית): כשמוכרים מכשיר סריאלי הם מבצעים
**הורדת מלאי**. כלומר הורדה בסניף הזה = מכירה בפועל. אנחנו צוברים אותן לטבלת `removals`
וממזגים אותן כמכירות בהמלצות ההזמנה.

stock-operations: הפילטר לפי branch_id + תאריך עובד, וה-pagination תקין (שלא כמו Documents).
נפח קטן (~4 הורדות/יום בסניף 3), אז מושכים את כל הטווח עם דפדוף עד עמוד קצר.
"""

import logging
from datetime import date, timedelta, datetime

import db
import poller

logger = logging.getLogger("transfers.removals")

REMOVAL_BRANCH = 3        # מרלוג/מחסן
REMOVAL_OP_TYPE = 2       # 'הורדה מהמלאי'
DEFAULT_INCREMENTAL_LOOKBACK = 3
_running = False


def _dd(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def _fetch_ops(no, start: date, end: date) -> list:
    """כל תנועות המלאי של סניף מרלוג בטווח (כל הדפים)."""
    out = []
    for pn in range(1, 60):
        batch = no.get_stock_operations(branch_id=REMOVAL_BRANCH, from_date=_dd(start),
                                        to_date=_dd(end), page_size=200, page_num=pn)
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 200:
            break
    return out


def _ingest_range(no, start: date, end: date, skip_ops: set) -> dict:
    ops = _fetch_ops(no, start, end)
    removals = [o for o in ops if o.get("operationType") == REMOVAL_OP_TYPE]
    rows = []
    new_ops = 0
    for o in removals:
        op_id = str(o.get("id"))
        if op_id in skip_ops:
            continue
        new_ops += 1
        when = o.get("createDate") or ""
        emp = o.get("employee") or ""
        # ההערה אינה נחשפת כיום ב-API הציבורי (Monday 2983951787); נתפוס אותה אוטומטית
        # ברגע שרפי יוסיף אותה — בודקים מספר שמות אפשריים ברמת הפעולה.
        note = (o.get("note") or o.get("remark") or o.get("comment")
                or o.get("description") or o.get("documentNumber") or "").strip()
        for i, it in enumerate(o.get("stockItems") or []):
            sers = it.get("serials") or []
            rows.append({
                "op_id": op_id, "line_no": i,
                "product_id": it.get("id"), "name": it.get("name") or "",
                "qty": abs(it.get("quantity") or 0),
                "serials": ",".join(str(s) for s in sers),
                "employee": emp,
                "note": note or (it.get("note") or it.get("remark") or "").strip(),
                "branch_id": REMOVAL_BRANCH, "removed_at": when,
            })
        skip_ops.add(op_id)
    lines = db.removals_insert(rows)
    return {"ops_seen": len(ops), "removals": len(removals), "ops_new": new_ops, "lines": lines}


def ingest_incremental() -> dict:
    global _running
    if _running:
        return {"skipped": "already running"}
    _running = True
    try:
        no = poller.client()
        today = date.today()
        last = db.sales_state_get("removals_last_date")
        if last:
            try:
                start = datetime.fromisoformat(last).date() - timedelta(days=DEFAULT_INCREMENTAL_LOOKBACK)
            except Exception:
                start = today - timedelta(days=DEFAULT_INCREMENTAL_LOOKBACK)
        else:
            start = today - timedelta(days=DEFAULT_INCREMENTAL_LOOKBACK)
        skip = db.removals_opids_since(start.isoformat())
        res = _ingest_range(no, start, today, skip)
        db.sales_state_set("removals_last_date", today.isoformat())
        db.sales_state_set("removals_last_run", db.now_iso())
        logger.info("removals incremental %s..%s: %s", start, today, res)
        return res
    finally:
        _running = False


def backfill(days: int = 90) -> dict:
    no = poller.client()
    today = date.today()
    start = today - timedelta(days=days)
    skip = db.removals_opids_since(start.isoformat())
    res = _ingest_range(no, start, today, skip)
    res["window"] = f"{start.isoformat()}..{today.isoformat()}"
    db.sales_state_set("removals_backfill_last_run", db.now_iso())
    logger.info("removals backfill %dd: %s", days, res)
    return res
