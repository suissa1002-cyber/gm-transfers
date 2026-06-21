#!/usr/bin/env python3
"""בדיקת רגרסיה — מודעות-וריאציה למלאי לפי צבע (variant-awareness).

באג (שיחה 491633171111): מוצר OPPO Find X9 Ultra Hasselblad עם שתי וריאציות באותו צבע
TUNDRA UMBER — אחת אזלה (נפח אחד) ואחת במלאי (נפח אחר). הבוט הכריז 'אזל בצבע TUNDRA
UMBER' למרות שיש יחידה זמינה באותו צבע, ואז ה-watcher ירה 'חזר למלאי' → פליפ-פלופ.

מאמת:
  • _oos_variations: צבע נחשב 'אזל' רק אם **כל** הוריאציות שלו אזלו.
  • _asked_color: כששואלים על צבע שקיים בכמה נפחים — מחזיר וריאציה **זמינה**, לא שאזלה.

רץ בלי קופה/WC/DB. הרצה: python3 tests/test_variant_stock.py  (קוד יציאה 0=עבר)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("META_WA_TOKEN", "T")
os.environ.setdefault("META_WA_PHONE_ID", "T")

import wa_bot  # noqa: E402

# התרחיש האמיתי: שני TUNDRA UMBER (אחד אזל, אחד במלאי) + CANYON ORANGE במלאי
VS = [
    {"id": 46481, "color": "CANYON ORANGE", "stock": "instock", "sku": "520396"},
    {"id": 46480, "color": "TUNDRA UMBER", "stock": "instock", "sku": "520395"},
    {"id": 46479, "color": "TUNDRA UMBER", "stock": "outofstock", "sku": "520368"},
]

# _oos_variations: TUNDRA UMBER לא אמור להופיע (יש לו וריאציה זמינה)
oos_colors = [v.get("color") for v in wa_bot._oos_variations(VS)]
assert oos_colors == [], f"color with an in-stock variation must not be 'oos': {oos_colors}"

# _asked_color: שאלה על TUNDRA UMBER → הוריאציה הזמינה (522395), לא שאזלה
asked = wa_bot._asked_color("יש לכם את זה ב TUNDRA UMBER?", VS)
assert asked and asked.get("stock") == "instock", f"expected in-stock variation, got {asked}"
assert asked.get("sku") == "520395"

# בקרה: צבע שכל הוריאציות שלו אזלו → כן 'אזל'
VS2 = [
    {"id": 1, "color": "BLUE", "stock": "instock", "sku": "A"},
    {"id": 2, "color": "RED", "stock": "outofstock", "sku": "B"},
    {"id": 3, "color": "RED", "stock": "outofstock", "sku": "C"},
]
assert [v.get("color") for v in wa_bot._oos_variations(VS2)] == ["RED", "RED"]
# שאלה על RED (הכל אזל) → מחזיר וריאציה (אזלה, אין זמינה)
assert (wa_bot._asked_color("RED בבקשה", VS2) or {}).get("stock") == "outofstock"

print("✅ variant-stock (color availability) regression test PASSED")
