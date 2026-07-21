"""
Poller — מושך העברות בין סניפים (operationType=5) מ-NewOrder ומעדכן את ה-DB.
רץ ברקע (APScheduler) כל POLL_INTERVAL_SEC שניות.

ה-API קריאה-בלבד; אנחנו רק קוראים. סטטוס הקליטה מנוהל אצלנו ב-DB, לא בקופה.
"""

import os
import sys
import logging
from datetime import datetime, timedelta

import config as cfg
import db

logger = logging.getLogger("transfers.poller")

# ה-client המשותף ל-NewOrder. בפיתוח: agents/shared. ב-Render: vendored ל-./shared.
_here = os.path.dirname(__file__)
for _p in (os.path.join(_here, "shared"), os.path.join(_here, "..", "shared")):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

from neworder_client import NewOrderClient, NewOrderError  # noqa: E402

_client = None
_barcode_cache = {}  # product_id -> barcode (כדי לא לקרוא שוב ושוב לאותו מוצר)


def client() -> NewOrderClient:
    global _client
    if _client is None:
        _client = NewOrderClient.from_env()
    return _client


def _barcode_for(product_id: str):
    """ברקוד המוצר מהקופה (cached). למוצרים לא-סידוריים — בסיס להתאמת סריקה."""
    pid = str(product_id)
    if pid in _barcode_cache:
        return _barcode_cache[pid]
    bc = None
    try:
        p = client().get_product(pid)
        if p:
            bc = (p.get("barcode") or "").strip() or None
    except NewOrderError as e:
        logger.warning("barcode lookup failed for %s: %s", pid, e)
    _barcode_cache[pid] = bc
    return bc


def _enrich_barcodes(op: dict):
    """משלים ברקוד לפריטים לא-סידוריים (ללא serials) של פעולה חדשה."""
    for it in op.get("stockItems", []) or []:
        if not [s for s in (it.get("serials") or []) if s]:
            it["barcode"] = _barcode_for(it.get("id"))


def _needs_items(op) -> bool:
    """האם למשוך את פריטי הפעולה מה-endpoint הנפרד של NewOrder.

    ⚠️ מכסה: NewOrder התלוננו (21/07) שאנחנו קוראים אצלם יותר מכל הלקוחות יחד.
    השורש: מאז 15/07 השלמנו פריטים לכל העברה בחלון (3 ימים) בכל סבב — כלומר גם
    לפעולות שכבר מזמן ב-DB, ובכל restart של Render (=כל deploy) המטמון התאפס
    והכול נמשך מחדש. אבל `upsert_transfer` מתעלם לגמרי מפעולה קיימת
    ("כבר קיים — לא נוגעים"), ולכן הפריטים שלה מיותרים.
    ⇒ מושכים פריטים **רק לפעולת העברה שעוד לא ב-DB**. במצב יציב: אפס קריאות.
    (אותו דפוס בדיוק כמו `_enrich_barcodes` שכבר מגודר ב-transfer_exists.)
    """
    if op.get("operationType") != cfg.TRANSFER_OP_TYPE:
        return False
    try:
        return not db.transfer_exists(op.get("id"))
    except Exception:  # noqa: BLE001
        return True     # ספק → מושכים (עדיף קריאה מיותרת מפעולה בלי פריטים)


def poll_once() -> dict:
    """
    סבב יחיד: מושך תנועות מ-N הימים האחרונים, מסנן העברות (type 5), ומכניס חדשות ל-DB.
    מחזיר סיכום {scanned, transfers, new}.
    """
    cutoff = datetime.now() - timedelta(days=cfg.POLL_LOOKBACK_DAYS)
    from_date = cutoff.strftime("%d/%m/%Y")
    new_ids = []
    transfers_seen = 0
    scanned = 0
    try:
        for page_num in range(1, 21):  # עד 20 עמודים × 200 = 4000 תנועות
            # items_for: פריטים רק להעברות-בין-סניפים — כל פעולה אחרת מדולגת ממילא
            # בלולאה למטה, וקריאת פריטים לכולן שורפת את מכסת ה-100/דקה (15/07)
            ops = client().get_stock_operations(
                from_date=from_date, page_size=200, page_num=page_num,
                items_for=_needs_items)
            if not ops:
                break
            scanned += len(ops)
            for o in ops:
                if o.get("operationType") != cfg.TRANSFER_OP_TYPE:
                    continue
                transfers_seen += 1
                # רק עבור פעולות חדשות: משלימים ברקודים (חוסך קריאות API לפעולות מוכרות)
                if not db.transfer_exists(o.get("id")):
                    _enrich_barcodes(o)
                if db.upsert_transfer(o):
                    new_ids.append(str(o.get("id")))
                    # עדכון חי של אינדקס סריאל→מוצר מפריטי ההעברה
                    idx = [(s, it.get("id"), it.get("name"))
                           for it in (o.get("stockItems") or [])
                           for s in (it.get("serials") or []) if s]
                    if idx:
                        db.serial_index_upsert_many(idx)
                    # ניקוי אוטומטי של בקשות העברה: העברה אמיתית בקופה שתואמת
                    # בקשה (אותו מקור→יעד, אותו סריאל/מוצר) מורידה אותה מהתוכנית ומהטייל
                    try:
                        items = [{"product_id": it.get("id"),
                                  "serials": it.get("serials") or [],
                                  "qty": it.get("quantity") or 0}
                                 for it in (o.get("stockItems") or [])]
                        n = db.plan_match_transfer(o.get("branchId"),
                                                   o.get("receivingBranchId"), items)
                        if n:
                            logger.info("plan auto-clean: %d request line(s) matched by op %s",
                                        n, o.get("id"))
                    except Exception as e:  # noqa: BLE001
                        logger.warning("plan_match_transfer failed for op %s: %s", o.get("id"), e)
            if len(ops) < 200:
                break
    except NewOrderError as e:
        logger.error("NewOrder API error during poll: %s", e)
        return {"error": str(e), "scanned": scanned,
                "transfers": transfers_seen, "new": new_ids}

    if new_ids:
        logger.info("Poll: %d new transfer(s): %s", len(new_ids), new_ids)
    return {"scanned": scanned, "transfers": transfers_seen, "new": new_ids}
