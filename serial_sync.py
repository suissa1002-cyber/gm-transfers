"""
סנכרון אינדקס סריאל→מוצר.
מיפוי סריאל→מוצר הוא קבוע (לא משתנה לעולם), אז מספיק סבב baseline תקופתי + עדכון חי
מה-poller. הסניף עצמו נבדק חי בזמן הסריקה (misroute.py), לא נשמר כאן.

baseline: עוברים על כל הדגמים הסריאליים שיש להם מלאי (~555), ולכל אחד מושכים את הסריאלים
שלו (עם branchId) ומעדכנים את האינדקס. ~555 קריאות, מוגבל-קצב → ~5-6 דקות.
"""

import logging
import time

import db
import poller  # reuse the shared NewOrder client + path setup

logger = logging.getLogger("transfers.serial_sync")

_running = False


def full_sync(max_products: int = 5000, include_zero_stock: bool = False) -> dict:
    """סבב מלא: מאנדקס את הסריאלים של הדגמים הסידוריים.

    ⚠️ include_zero_stock: הפילטר "מלאי>0" נלקח מהקטלוג **שלנו**, שמתרענן כל 6
    שעות. יחידה שיושבת בסניף בזמן שהאגרגט אצלנו אפס (או מתעדכן באיחור) נשארת
    מחוץ לאינדקס, והסריקה אמרה "לא נמצא בקופה" (אסי, 10/08/2026). הסבב הלילי
    רץ עם include_zero_stock=True כדי שהבסיס יהיה שלם."""
    global _running
    if _running:
        return {"skipped": "already running"}
    _running = True
    try:
        no = poller.client()
        # ⚠️ מקור הדגמים = הקטלוג ב-DB (מרוענן ע"י catalog_refresh כל 6ש), לא
        # get_all_products — שהוא ~28 קריאות ל-NewOrder בכל ריצה על מידע שכבר יש לנו.
        # (חלק מקיצוץ המכסה 26/07 אחרי תלונת NewOrder.)
        catalog = db.catalog_load() or {}
        targets = [{"id": pid, "name": c.get("name") or ""}
                   for pid, c in catalog.items()
                   if c.get("kind") == "serial"
                   and (include_zero_stock or (c.get("stock") or 0) > 0)][:max_products]
        if not targets:      # קטלוג ריק (טרם רוענן) — נפילה למקור הישן, פעם אחת
            logger.warning("serial full_sync: catalog empty — falling back to get_all_products")
            targets = [{"id": p.get("id"), "name": p.get("name") or ""}
                       for p in (no.get_all_products() or [])
                       if p.get("isSerial") and (p.get("currentStock") or 0) > 0][:max_products]
        logger.info("serial full_sync: %d serial products with stock", len(targets))
        total_serials = 0
        batch = []
        for i, p in enumerate(targets):
            pid = p.get("id")
            name = p.get("name") or ""
            # האטה מכוונת (~70 קריאות/דקה במקום מקסימום 99) — משאיר מקום בקצב המשותף
            # לקריאות אינטראקטיביות (טאב מלאי חי / אורי) בזמן שהסנכרון רץ ברקע.
            time.sleep(0.5)
            try:
                serials = no.get_product_serials(pid) or []
            except Exception as e:  # noqa: BLE001
                logger.warning("serials fetch failed for %s: %s", pid, e)
                continue
            for s in serials:
                sn = s.get("serial")
                if sn:
                    batch.append((sn, pid, name))
            if len(batch) >= 500:
                db.serial_index_upsert_many(batch)
                total_serials += len(batch)
                batch = []
            if (i + 1) % 100 == 0:
                logger.info("serial full_sync progress: %d/%d products", i + 1, len(targets))
        if batch:
            db.serial_index_upsert_many(batch)
            total_serials += len(batch)
        logger.info("serial full_sync done: %d serials indexed (from %d products)",
                    total_serials, len(targets))
        return {"products": len(targets), "serials": total_serials,
                "index_size": db.serial_index_count()}
    finally:
        _running = False

def index_from_operations(days: int = 60, branch_id=None) -> dict:
    """אינדוקס סריאלים מתוך **תנועות המלאי** של הקופה.

    ⚠️ למה זה קיים: הסבב המלא עובר על מוצרים סידוריים ש**יש להם מלאי בקטלוג
    שלנו**, ורץ פעם ביום. מכשיר שהגיע לסניף אחרי הסבב האחרון אינו באינדקס,
    והסריקה אמרה "מכשיר לא מזוהה — לא נמצא בקופה" (אסי, 10/08/2026).
    כל מכשיר סידורי נכנס לסניף דרך תנועת מלאי, ותנועה מחזירה את הסריאלים
    שלה — ולכן זו הדרך הזולה לתפוס בדיוק את המקרים שהסבב מפספס:
    קריאה אחת עד שתיים, במקום סריקה של אלפי מוצרים.
    """
    from datetime import date, timedelta
    no = poller.client()
    start = (date.today() - timedelta(days=max(1, days))).strftime("%d/%m/%Y")
    end = date.today().strftime("%d/%m/%Y")
    rows, ops_seen = [], 0
    for pn in range(1, 12):
        try:
            batch = no.get_stock_operations(branch_id=branch_id, from_date=start,
                                            to_date=end, page_size=200, page_num=pn,
                                            items_for=lambda op: True)
        except Exception as e:  # noqa: BLE001
            logger.warning("serial index_from_operations page %s failed: %s", pn, e)
            break
        if not batch:
            break
        ops_seen += len(batch)
        for o in batch:
            for it in (o.get("stockItems") or []):
                pid = it.get("id")
                name = it.get("name") or ""
                for sn in (it.get("serials") or []):
                    if sn:
                        rows.append((str(sn), pid, name))
        if len(batch) < 200:
            break
    n = db.serial_index_upsert_many(rows) if rows else 0
    logger.info("serial index_from_operations: branch=%s days=%s ops=%d serials=%d",
                branch_id, days, ops_seen, n)
    return {"ops": ops_seen, "serials": n}
