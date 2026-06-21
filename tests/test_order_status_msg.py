#!/usr/bin/env python3
"""בדיקת רגרסיה — טקסט סטטוס הזמנה ללקוח (_status_msg).

באג (שיחה 972539644424, הזמנה 47323): סטטוס WC 'pending' (ממתין לתשלום) הוצג ללקוח
כ'נקלטה ונמצאת בטיפול — אנו מכינים אותה', בזמן שההזמנה כלל לא שולמה.

מאמת מיפוי סטטוס→טקסט: pending/checkout-draft→ממתין לתשלום; processing→בטיפול;
completed→נמסרה; cancelled→בוטלה.
הרצה: python3 tests/test_order_status_msg.py  (קוד יציאה 0=עבר)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ.setdefault("META_WA_TOKEN", "T")
os.environ.setdefault("META_WA_PHONE_ID", "T")

import wa_bot  # noqa: E402


def msg(status, meta=None):
    o = {"status": status, "shipping_lines": [], "billing": {"first_name": "אורי"}}
    return wa_bot._status_msg(o, meta or {}, "אורי", "47323")


# pending/checkout-draft = ממתין לתשלום — לא 'בטיפול'
for st in ("pending", "checkout-draft"):
    m = msg(st)
    assert "ממתינה להשלמת התשלום" in m, f"{st} must say awaiting payment: {m!r}"
    assert "בטיפול" not in m and "מכינים אותה" not in m, f"{st} must NOT say being handled: {m!r}"

# pending עם קישור תשלום → הקישור מצורף
assert "https://pay.x/1" in msg("pending", {"greenos_payplus_link": "https://pay.x/1"})

# processing = שולם ובטיפול — הטקסט הישן נכון כאן
assert "בטיפול" in msg("processing")
# completed/cancelled
assert "נמסרה" in msg("completed")
assert "בוטלה" in msg("cancelled")

print("✅ order-status message regression test PASSED")
