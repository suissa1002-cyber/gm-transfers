#!/usr/bin/env python3
"""בדיקת רגרסיה — ניתוב חכם + איזון אוטומטי של הזמנות אתר (auto_transfer).

מאמת:
  • מילוי מעדיף סניף יחיד שמכסה את כל הכמות (לא מפצל מיותר).
  • סדר עדיפות מקור [סטאר, גן, סיטי, עד הלום].
  • השלמה אוטומטית לסטאר כשהוא מתרוקן, מהסניף-העודף הראשון (2+) ב-[גן,סיטי,עד הלום].

רץ בלי קופה אמיתית (mock get_product_stock) ובלי DB אמיתי (SQLite זמני) ובלי טלגרם.
הרצה: python3 tests/test_auto_balance.py    (קוד יציאה 0=עבר)
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

SKU = "999001"
CATALOG = {SKU: {"name": "מוצר בדיקה", "is_stock": True, "category": "", "kind": "barcode"}}


class _FakeNO:
    def __init__(self, stock):
        self._stock = stock

    def get_product_stock(self, sku):
        return dict(self._stock)

    def get_product_serials(self, sku):
        return []


def _run(stock, qty):
    """מריץ _handle_order עם מלאי נתון, מחזיר רשימת שורות הבקשה שנוצרו."""
    db.plan_clear()
    AT.poller.client = lambda: _FakeNO(stock)
    alerts._send = lambda *a, **k: None
    order = {"number": f"T{qty}", "line_items": [{"sku": SKU, "quantity": qty, "name": "x"}]}
    AT._handle_order(order, CATALOG)
    out = []
    for l in db.plan_list():
        out.append((int(l["from_branch"]), int(l["to_branch"]), int(l["qty"]),
                    str(l.get("created_by") or "").startswith("השלמה אוטומטית")))
    return sorted(out)


def _check(name, got, expect):
    ok = sorted(got) == sorted(expect)
    print(("✅" if ok else "❌"), name, "→", got, "" if ok else f"(ציפיתי {expect})")
    return ok


SITE, STAR, GAN, CITY, ADH = 5, 2, 1, 3, 4
passed = True

# 1) 1 יח': סטאר0 גן1 סיטי2 עדהלום2 → גן→אתר(מילוי); פיזור: סיטי→סטאר, עד הלום→גן
#    (מספיק עודף לאזן את כל 4 — כולם ל-1)
passed &= _check("1 יח' (איזון מלא)",
                 _run({STAR: 0, GAN: 1, CITY: 2, ADH: 2}, 1),
                 [(GAN, SITE, 1, False), (CITY, STAR, 1, True), (ADH, GAN, 1, True)])

# 1b) הדוגמה של אסי: סטאר0 גן0 סיטי1 עדהלום3 · 1 יח'
#     סיטי→אתר(מילוי) ; עד הלום מפזר: →סטאר ו→סיטי (גן נשאר 0, אין יותר עודף)
passed &= _check("פיזור עודף (3 סניפים מאוזנים)",
                 _run({STAR: 0, GAN: 0, CITY: 1, ADH: 3}, 1),
                 [(CITY, SITE, 1, False), (ADH, STAR, 1, True), (ADH, CITY, 1, True)])

# 2) 2 יח': סטאר0 גן1 סיטי2 עדהלום2 → סיטי מכסה לבד→אתר x2 + עד הלום→סטאר(השלמה)
passed &= _check("2 יח' (כיסוי יחיד)",
                 _run({STAR: 0, GAN: 1, CITY: 2, ADH: 2}, 2),
                 [(CITY, SITE, 2, False), (ADH, STAR, 1, True)])

# 3) לסטאר יש מלאי → סטאר ממלא, נשאר לו → אין השלמה
passed &= _check("סטאר מלא (אין השלמה)",
                 _run({STAR: 3, GAN: 0, CITY: 0, ADH: 0}, 1),
                 [(STAR, SITE, 1, False)])

# 4) סטאר יח' אחרונה, סיטי עם עודף גדול (5) → סטאר ממלא ומתרוקן; סיטי מפזר לכל הריקים
passed &= _check("עודף גדול מתפזר לכולם",
                 _run({STAR: 1, GAN: 0, CITY: 5, ADH: 0}, 1),
                 [(STAR, SITE, 1, False), (CITY, STAR, 1, True),
                  (CITY, ADH, 1, True), (CITY, GAN, 1, True)])

# 5) אין סניף-עודף 2+ → אין השלמה (לא יוצרים cascade)
passed &= _check("אין מקור 2+ → אין השלמה",
                 _run({STAR: 0, GAN: 1, CITY: 1, ADH: 0}, 1),
                 [(GAN, SITE, 1, False)])

print("\n" + ("✅ ALL PASSED" if passed else "❌ SOME FAILED"))
sys.exit(0 if passed else 1)
