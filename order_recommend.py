"""
לוגיקת המלצות הזמנה.

משלב מכירות מהמאגר (sales) עם מלאי נוכחי + ספק/קטגוריה מקטלוג NewOrder,
ומחשב לכל מוצר שנמכר בתקופה: כמות שנמכרה, מלאי נוכחי, ימי כיסוי, וכמות מומלצת להזמנה.

מומלץ = max(0, ceil(קצב_יומי * ימי_יעד) - מלאי). רשימת "נמכר וגם אוזל".

קטלוג המוצרים נשמר ב-cache עם TTL (get_all_products כבד) — מתרענן ברקע/לפי דרישה.
"""

import logging
import math
import time
from datetime import date, timedelta, datetime

import db
import poller

logger = logging.getLogger("transfers.recommend")

_DEFAULT_TARGET_DAYS = 21
_refreshing = False


def refresh_catalog_to_db() -> dict:
    """בונה את הקטלוג מ-NewOrder (get_all_products, ~28 קריאות) ושומר ב-DB.
    מורץ ע"י job מתוזמן — לא בכל בקשת המלצות. כך אין הצפת טוקן/429."""
    global _refreshing
    if _refreshing:
        return {"skipped": "already refreshing"}
    _refreshing = True
    try:
        no = poller.client()
        prods = no.get_all_products() or []   # ללא branch_id → currentStock = סך כל הסניפים
        rows = []
        for p in prods:
            sup = p.get("supplier") or {}
            isser = bool(p.get("isSerial"))
            bc = (p.get("barcode") or "").strip()
            rows.append({
                "product_id": str(p.get("id")),
                "name": p.get("name") or "",
                "stock": p.get("currentStock") or 0,
                "supplier": (sup.get("name") or "").strip() if isinstance(sup, dict) else "",
                "category": ((p.get("category") or {}).get("name") or "").strip(),
                "kind": "serial" if isser else ("barcode" if bc else "other"),
                "barcode": bc,
                "active": bool(p.get("isActive")),
            })
        if rows:
            db.catalog_replace(rows)
            logger.info("catalog refreshed to DB: %d products", len(rows))
        return {"products": len(rows)}
    finally:
        _refreshing = False


def compute(days: int = 30, branch_id=None, target_days: int = None) -> dict:
    """המלצות הזמנה לתקופה. מחזיר {items, meta}. סינון נוסף (סוג/ספק/קטגוריה/חיפוש) בצד הלקוח."""
    days = int(days or 30)
    target_days = int(target_days or _DEFAULT_TARGET_DAYS)
    today = date.today()
    since = (today - timedelta(days=days)).isoformat()
    agg = db.sales_aggregate(since, branch_id=branch_id)   # {pid: {qty,last_date,branches}}
    rem = db.removals_aggregate(since, branch_id=branch_id)  # מרלוג: הורדת מלאי = מכירה בפועל
    catalog = db.catalog_load()                            # מ-DB — 0 קריאות NewOrder
    summary = db.sales_summary()
    cat_meta = db.catalog_meta()
    # מאחדים מכירות-חשבונית (נטו) + הורדות-מרלוג לכל מוצר
    combined = {}
    for pid, s in agg.items():
        combined[pid] = {"qty": s["qty"], "last_date": s.get("last_date", "")}
    for pid, q in rem.items():
        d = combined.setdefault(pid, {"qty": 0.0, "last_date": ""})
        d["qty"] += q

    # מכנה אפקטיבי: אם יש לנו פחות היסטוריה מהחלון המבוקש, מחלקים במספר הימים שבאמת נאספו
    # (אחרת קצב המכירה מוערך בחסר בזמן רולאאוט). מקסימום = days.
    data_days = days
    if summary.get("min_date"):
        try:
            span = (today - datetime.fromisoformat(summary["min_date"]).date()).days + 1
            data_days = max(1, min(days, span))
        except Exception:
            data_days = days

    items = []
    for pid, s in combined.items():
        sold = round(s["qty"], 2)
        if sold <= 0:
            continue                       # נטו אפס/שלילי (החזרות) — לא ממליצים
        c = catalog.get(pid)
        if not c:
            continue                       # מוצר שלא בקטלוג (שירות/תיקון) — מדלגים
        if c["kind"] == "other":
            continue                       # רק פריטים סריאליים/ברקוד (מלאי אמיתי)
        stock = c["stock"] or 0
        stock_eff = max(0, stock)          # מלאי שלילי (oversold ברקוד) → 0 לחישוב, שלא יפוצץ המלצה
        per_day = sold / data_days if data_days else 0
        cover = round(stock_eff / per_day, 1) if per_day > 0 else None
        rec = max(0, math.ceil(per_day * target_days) - stock_eff)
        items.append({
            "product_id": pid, "name": c["name"], "kind": c["kind"],
            "supplier": c["supplier"], "category": c["category"], "barcode": c["barcode"],
            "sold": sold, "stock": stock, "cover_days": cover,
            "recommended": rec, "last_sold": (s["last_date"] or "")[:10],
        })
    # מיון: כמות מומלצת יורד, ואז נמכר יורד (הדחוף ביותר למעלה)
    items.sort(key=lambda x: (-x["recommended"], -x["sold"]))
    return {
        "items": items,
        "meta": {"days": days, "data_days": data_days, "target_days": target_days,
                 "branch_id": branch_id, "count": len(items),
                 "catalog_count": cat_meta["count"], "catalog_updated": cat_meta["updated_at"],
                 "catalog_ready": cat_meta["count"] > 0,
                 "sales_summary": summary},
    }
