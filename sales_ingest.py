"""
איסוף מכירות למאגר מקומי (Sales Cache).

NewOrder לא חושף דוח מכירות פר-מוצר, ו-/api/Documents מחזיר רק כותרות.
לכן אנחנו בונים מאגר משלנו: עוברים על מסמכים (חשבוניות) ולכל מסמך מושכים את שורות
הפריטים (/api/Documents/line-items?invoiceId=) וצוברים לטבלת `sales`.
qty חתום: מכירה (docType 0) חיובי, החזרה/זיכוי (docType 5) שלילי → SUM(qty)=מכירה נטו.

⚠️ עובדות API קריטיות (נבדק 09/06/2026):
  • /api/Documents ממוין מהחדש לישן.
  • פילטר fromDate/toDate (DD/MM/YYYY) עובד — ברמת יום.
  • ה-pagination שבור: page_num מתעלם, כל עמוד מחזיר את אותם 200 ראשונים.
  → לכן אוספים **יום-יום** (כל יום ≤~120 מסמכים, לעולם לא נחתך מ-200). זה גם מדויק וגם resumable.

שני מצבים:
  ingest_incremental() — יומי. חלון קצר (מאז ה-cursor) יום-יום, מדלג על מסמכים שכבר נקלטו.
  backfill(days, max_new_docs) — חד-פעמי/מתוזמן. מהחדש לישן, מווסת, resumable.
"""

import logging
from datetime import date, timedelta, datetime

import db
import poller

logger = logging.getLogger("transfers.sales")

DEFAULT_INCREMENTAL_LOOKBACK = 3   # ימים אחורה לחפיפה (לתפוס מסמכים מאוחרים)
_running = False


def _dd(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def _day_docs(no, d: date) -> list:
    """כל מסמכי יום בודד (קריאה אחת; ≤200 ⇒ שלם). מתריע אם נחתך."""
    docs = no.get_documents(from_date=_dd(d), to_date=_dd(d), page_size=200, page_num=1) or []
    if len(docs) >= 200:
        logger.warning("sales: day %s returned %d docs — possible truncation!", d, len(docs))
    return docs


def _ingest_range(no, start: date, end: date, skip: set, max_new_docs=None) -> dict:
    """אוסף יום-יום מ-end עד start (כולל), מהחדש לישן. מדלג על doc_ids ב-skip.
    max_new_docs: עוצר אחרי N מסמכים חדשים (לחלקי backfill). resumable כי מדלג על קיימים."""
    processed = 0
    lines_in = 0
    docs_seen = 0
    buf = []
    stopped_early = False
    day = end
    while day >= start:
        docs = _day_docs(no, day)
        docs_seen += len(docs)
        for d in docs:
            doc_id = str(d.get("id"))
            if doc_id in skip:
                continue
            if max_new_docs is not None and processed >= max_new_docs:
                stopped_early = True
                break
            try:
                items = no._get("/api/Documents/line-items", {"invoiceId": doc_id}) or []
            except Exception as e:  # noqa: BLE001
                logger.warning("line-items failed for %s: %s", doc_id, e)
                continue
            branch_id = d.get("branchId")
            doc_type = d.get("documentType")
            sale_date = d.get("createDate") or ""
            for i, it in enumerate(items):
                buf.append({
                    "doc_id": doc_id, "line_no": i,
                    "product_id": it.get("id"), "name": it.get("name") or "",
                    "qty": it.get("quantity") or 0, "price": it.get("price") or 0,
                    "serial": it.get("serial") or "",
                    "branch_id": branch_id, "doc_type": doc_type, "sale_date": sale_date,
                })
            skip.add(doc_id)
            processed += 1
            if len(buf) >= 500:
                lines_in += db.sales_insert(buf); buf = []
        if stopped_early:
            break
        day -= timedelta(days=1)
    if buf:
        lines_in += db.sales_insert(buf)
    return {"docs_seen": docs_seen, "docs_processed": processed,
            "lines": lines_in, "stopped_early": stopped_early}


def ingest_incremental() -> dict:
    """איסוף יומי: מאז ה-cursor (עם חפיפה) עד היום, יום-יום. מדלג על קיימים."""
    global _running
    if _running:
        return {"skipped": "already running"}
    _running = True
    try:
        no = poller.client()
        today = date.today()
        last = db.sales_state_get("last_date")
        if last:
            try:
                start = datetime.fromisoformat(last).date() - timedelta(days=DEFAULT_INCREMENTAL_LOOKBACK)
            except Exception:
                start = today - timedelta(days=DEFAULT_INCREMENTAL_LOOKBACK)
        else:
            start = today - timedelta(days=DEFAULT_INCREMENTAL_LOOKBACK)
        skip = db.sales_docids_since(start.isoformat())
        res = _ingest_range(no, start, today, skip)
        db.sales_state_set("last_date", today.isoformat())
        db.sales_state_set("last_run", db.now_iso())
        logger.info("sales incremental %s..%s: %s", start, today, res)
        return res
    finally:
        _running = False


def backfill(days: int = 90, max_new_docs: int = None) -> dict:
    """איסוף היסטורי לאחור (מהחדש לישן), יום-יום. resumable. max_new_docs=חלק אחד.
    לא נוגע ב-cursor של ה-incremental."""
    no = poller.client()
    today = date.today()
    start = today - timedelta(days=days)
    skip = db.sales_docids_since(start.isoformat())
    res = _ingest_range(no, start, today, skip, max_new_docs=max_new_docs)
    res["window"] = f"{start.isoformat()}..{today.isoformat()}"
    db.sales_state_set("backfill_last_run", db.now_iso())
    logger.info("sales backfill %dd: %s", days, res)
    return res
