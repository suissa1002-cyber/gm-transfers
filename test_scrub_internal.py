"""סינון נימוק פנימי מתשובות אלה לפני שליחה ללקוח (01/11/2026)."""
import main

LEAK = ("ה-verdict הוא `site_only_handoff` — האתר מציג \"במלאי\" אך הקופה לא מחוברת. "
        "לפי הנחיות: לומר שזמין לפי מה שנראה + להעביר לנציג שיבדוק.\n\n"
        "כן, יש לנו PS5 Slim Blu-ray במלאי לפי מה שנראה לי, במחיר 3,349₪.\n"
        "אעביר אותך לנציג שיאשר זמינות עדכנית.\n\n"
        "https://greenmobile.co.il/product/playstation-5-slim-1tb-blu-ray/")

clean, dropped = main._scrub_internal(LEAK)
assert len(dropped) == 1 and "verdict" in dropped[0]
assert clean.startswith("כן, יש לנו PS5") and clean.endswith("blu-ray/")

# תשובה רגילה — לא נוגעים, כולל slug עם מילה כמו api בתוך URL
ok = "זמין במלאי 🙂\n\nhttps://greenmobile.co.il/product/api-cable-sku/"
assert main._scrub_internal(ok) == (ok, [])

# תשובה פנימית כולה → ריקה
c, d = main._scrub_internal("בדקתי curl ל-uri-bridge/stock, in_stock=true")
assert c == "" and len(d) == 1
print("ok")
