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

_CATALOG = {"at": 0.0, "data": {}}     # pid -> {name, stock, supplier, category, kind, barcode, active}
_CATALOG_TTL = 1800                     # שנייה (30 דק')
_DEFAULT_TARGET_DAYS = 21


def _build_catalog(force: bool = False) -> dict:
    """{pid: {...}} מלאי כולל + ספק/קטגוריה/סוג. cache עם TTL."""
    now = time.time()
    if not force and _CATALOG["data"] and (now - _CATALOG["at"] < _CATALOG_TTL):
        return _CATALOG["data"]
    no = poller.client()
    prods = no.get_all_products() or []      # ללא branch_id → currentStock = סך כל הסניפים
    cat = {}
    for p in prods:
        pid = str(p.get("id"))
        sup = p.get("supplier") or {}
        isser = bool(p.get("isSerial"))
        bc = (p.get("barcode") or "").strip()
        cat[pid] = {
            "name": p.get("name") or "",
            "stock": p.get("currentStock") or 0,
            "supplier": (sup.get("name") or "").strip() if isinstance(sup, dict) else "",
            "category": ((p.get("category") or {}).get("name") or "").strip(),
            "kind": "serial" if isser else ("barcode" if bc else "other"),
            "barcode": bc,
            "active": bool(p.get("isActive")),
        }
    _CATALOG["data"] = cat
    _CATALOG["at"] = now
    logger.info("catalog cache built: %d products", len(cat))
    return cat


def refresh_catalog():
    _build_catalog(force=True)


def compute(days: int = 30, branch_id=None, target_days: int = None) -> dict:
    """המלצות הזמנה לתקופה. מחזיר {items, meta}. סינון נוסף (סוג/ספק/קטגוריה/חיפוש) בצד הלקוח."""
    days = int(days or 30)
    target_days = int(target_days or _DEFAULT_TARGET_DAYS)
    today = date.today()
    since = (today - timedelta(days=days)).isoformat()
    agg = db.sales_aggregate(since, branch_id=branch_id)   # {pid: {qty,last_date,branches}}
    catalog = _build_catalog()
    summary = db.sales_summary()

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
    for pid, s in agg.items():
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
                 "sales_summary": summary},
    }
