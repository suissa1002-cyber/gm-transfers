#!/usr/bin/env python3
"""בדיקת רגרסיה — החלפת מוצר בהזמנה (auto_transfer._handle_order עם pins / only_skus).

מאמת:
  • סידורי שנבחר ידנית: הסניף שלו הופך למקור (גם אם אינו הראשון בעדיפות) והוא היחידה שנשלחת.
  • only_skus: רק המק"ט החדש משודר — שורות אחרות בהזמנה לא משודרות שוב,
    ודגלי OOS של ההזמנה לא נוגעים (הקורא מחליט).
  • בלי pins — ההתנהגות הרגילה לא השתנתה (סטאר קודם, הסידורי הוותיק ביותר).
הרצה: python3 tests/test_swap_pin.py    (קוד יציאה 0=עבר)
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["TRANSFERS_DB_PATH"] = _tmp.name
os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("META_WA_TOKEN", "T")
os.environ.setdefault("META_WA_PHONE_ID", "T")

import db                # noqa: E402
import auto_transfer as AT  # noqa: E402
import alerts             # noqa: E402

db.init_db()

NEW, OTHER = "888001", "888002"
CATALOG = {NEW: {"name": "שעון זהב", "is_stock": True, "category": "", "kind": "serial"},
           OTHER: {"name": "רצועה", "is_stock": True, "category": "", "kind": "barcode"}}
SERIALS = [{"serial": "S-STAR-OLD", "branchId": 2, "status": 1, "insertDate": "2026-01-01"},
           {"serial": "S-CITY-1", "branchId": 3, "status": 1, "insertDate": "2026-02-01"},
           {"serial": "S-CITY-2", "branchId": 3, "status": 1, "insertDate": "2026-03-01"}]


class _FakeNO:
    def get_product_stock(self, sku):
        return {2: 1, 3: 2} if sku == NEW else {2: 5}

    def get_product_serials(self, sku):
        return [dict(u) for u in SERIALS] if sku == NEW else []


AT.poller.client = lambda: _FakeNO()
alerts._send = lambda *a, **k: None
fails = 0


def check(name, cond):
    global fails
    print(("✓ " if cond else "✗ ") + name)
    if not cond:
        fails += 1


def lines():
    return [(int(l["from_branch"]), str(l.get("product_id")), str(l.get("serial") or ""),
             str(l.get("created_by") or "")) for l in db.plan_list()
            if not str(l.get("created_by") or "").startswith("השלמה")]


order = {"number": "T1", "id": 1, "line_items": [{"sku": NEW, "quantity": 1, "name": "x"},
                                                {"sku": OTHER, "quantity": 1, "name": "y"}]}

# 1) ללא pins — סטאר, הסידורי הוותיק
db.plan_clear()
AT._handle_order(order, CATALOG)
ls = [l for l in lines() if l[1] == NEW]
check("default: from Star with oldest serial", ls == [(2, NEW, "S-STAR-OLD", ls[0][3] if ls else "")])

# 2) pin לסידורי בסיטי + only_skus — רק המק"ט החדש, מסיטי, היחידה שנבחרה
db.plan_clear()
db.sales_state_set("order_oos_list", '[{"number": "T1", "item": "רצועה", "partial": true}]')
created = AT._handle_order(order, CATALOG, pins={NEW: "S-CITY-2"}, only_skus={NEW})
ls = lines()
check("pin: only the new sku was broadcast", {l[1] for l in ls} == {NEW})
check("pin: source is the pinned serial's branch (City)", [l[0] for l in ls] == [3])
check("pin: the pinned serial is the unit sent", [l[2] for l in ls] == ["S-CITY-2"])
check("pin: created reports City", [c["src"] for c in created if not c.get("balance")] == [3])
check("only_skus: order OOS flag untouched", "T1" in (db.sales_state_get("order_oos_list") or ""))
check("only_skus: broadcast flag set", bool(db.sales_state_get("auto_tr_bcast:T1")))

# 3) pin לסידורי שלא קיים — נופל להתנהגות הרגילה
db.plan_clear()
AT._handle_order(order, CATALOG, pins={NEW: "NOPE"}, only_skus={NEW})
check("unknown pin: falls back to default (Star)", [l[0] for l in lines()] == [2])

# 4) שורה שהוחלפה לווריאציה לא-מחוברת: SKU ריק באתר, מק"ט הקופה ב-_gm_pos_sku
db.plan_clear()
o4 = {"number": "T4", "id": 4, "line_items": [{"sku": "", "quantity": 1, "name": "z",
      "meta_data": [{"key": "_gm_pos_sku", "value": NEW}]}]}
AT._handle_order(o4, CATALOG, pins={NEW: "S-CITY-1"}, only_skus={NEW})
check("_gm_pos_sku: line without site SKU is broadcast by its POS sku",
      [(l[0], l[1], l[2]) for l in lines()] == [(3, NEW, "S-CITY-1")])
check("li_pos_sku falls back to the site SKU", AT.li_pos_sku({"sku": " 123 "}) == "123")

print("PASS" if not fails else f"FAIL ({fails})")
sys.exit(1 if fails else 0)
