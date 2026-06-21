#!/usr/bin/env python3
"""בדיקת רגרסיה — זיהוי שאלת מחיר-תיקון בטקסט חופשי (_is_repair_quote_intent).

באג (שיחה 972546619676): 'כמה עולה להחליף סוללה לאייפון 14 פרו' נפלה לאורי (שלא מכיר
את מחירון המעבדה) ונשלחה לנציג, במקום לתת מחיר מהמחירון הדטרמיניסטי.

מאמת: שאלות תיקון → True; שאלות מוצר (כולל 'מגן מסך') → False.
הרצה: python3 tests/test_repair_intent.py  (קוד יציאה 0=עבר)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ.setdefault("META_WA_TOKEN", "T")
os.environ.setdefault("META_WA_PHONE_ID", "T")

import wa_bot  # noqa: E402

REPAIR = [
    "כמה עולה להחליף סוללה לאייפון 14 פרו",
    "המסך נשבר באייפון 13",
    "תיקון מסך גלקסי S23",
    "החלפת סוללה לאייפון 12",
    "השקע טעינה דפוק",
]
NOT_REPAIR = [
    "יש לכם מגן מסך לאייפון 15?",      # מוצר, לא תיקון
    "כמה עולה מגן מסך",
    "אני רוצה אוזניות JBL",
    "כמה עולה אייפון 16 פרו מקס",       # שאלת מחיר מוצר, לא תיקון
    "יש לכם כיסוי סיליקון",
]

for q in REPAIR:
    assert wa_bot._is_repair_quote_intent(q), f"repair question not detected: {q!r}"
for q in NOT_REPAIR:
    assert not wa_bot._is_repair_quote_intent(q), f"product query wrongly flagged as repair: {q!r}"

print("✅ repair-quote intent regression test PASSED")
