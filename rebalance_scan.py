"""
סריקת איזון מלאי — פעם ביום (וגם ידנית).
עוברת על כל הסניפים פרט ל"אתר" (5), בונה מטריצת מלאי לכל מוצר סריאלי/ברקוד, ומסמנת
מוצרים שבהם בסניף אחד יש 0 ובאחר יש ≥2 (עודף שאפשר להעביר). מחליפה את רשימת האיזון.

יעיל: ~4 קריאות מקובצות (get_all_products לכל סניף, מעומד) במקום אלפי קריאות פר-מוצר.
"""

import logging

import db
import poller

logger = logging.getLogger("transfers.rebalance")

_BRANCHES = [1, 2, 3, 4]   # ללא 5 (אתר — לא מחזיק מלאי)
_running = False


def scan() -> dict:
    global _running
    if _running:
        return {"skipped": "already running"}
    _running = True
    try:
        no = poller.client()
        matrix = {}   # pid -> {name, isSerial, barcode, stock:{branch:qty}}
        for b in _BRANCHES:
            prods = no.get_all_products(branch_id=b) or []
            for p in prods:
                pid = str(p.get("id"))
                m = matrix.setdefault(pid, {"name": p.get("name") or "",
                                            "isSerial": bool(p.get("isSerial")),
                                            "barcode": (p.get("barcode") or "").strip(),
                                            "stock": {}})
                # ודא שהמטא נשמר (סניף ראשון שבו המוצר מופיע)
                if not m["name"]:
                    m["name"] = p.get("name") or ""
                m["isSerial"] = m["isSerial"] or bool(p.get("isSerial"))
                if not m["barcode"]:
                    m["barcode"] = (p.get("barcode") or "").strip()
                m["stock"][b] = p.get("currentStock") or 0
            logger.info("rebalance: branch %s → %d products", b, len(prods))

        candidates = []
        for pid, m in matrix.items():
            if not m["isSerial"] and not m["barcode"]:
                continue   # רק פריטים עם סריאל/ברקוד
            stock = {b: (m["stock"].get(b, 0) or 0) for b in _BRANCHES}
            needs = [b for b in _BRANCHES if stock[b] == 0]
            surplus = [b for b in _BRANCHES if stock[b] >= 2]
            if needs and surplus:
                candidates.append({
                    "product_id": pid, "name": m["name"],
                    "kind": "serial" if m["isSerial"] else "barcode",
                    "stock": {str(b): stock[b] for b in _BRANCHES},
                    "needs": needs, "surplus": surplus,
                })

        db.rebalance_replace(candidates, db.now_iso())
        logger.info("rebalance scan done: %d products to balance (of %d scanned)",
                    len(candidates), len(matrix))
        return {"products_scanned": len(matrix), "to_balance": len(candidates)}
    finally:
        _running = False
