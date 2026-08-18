"""בדיקות לסיגנלים שהחליפו את IPQS בטריאז' האימות.

⚠️ הרקע (אסי, 18/08/2026): הקרדיט ב-IPQualityScore נגמר ולא מתחדש —
"insufficient credits" אומת חי. איתו נפלו זיהוי מייל חד-פעמי, גיל מייל,
ו-VoIP בטלפון. שכבת הרשת (ip-api), גרף הכרטיסים והלקוח החוזר נשארו חיים.

⛔ הבדיקות בוחנות **החלטות** — מה מסומן ומה לא. לא את קוד המקור.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402


def run():
    passed, failed = 0, []

    def check(name, cond):
        nonlocal passed
        if cond:
            passed += 1
        else:
            failed.append(name)

    kind = main._il_phone_kind

    # 1. ⛔ הליבה: 07X = וירטואלי. זה מה שהחליף את דגל ה-VoIP של IPQS.
    for ph in ("0721234567", "0731234567", "0741234567",
               "0761234567", "0771234567", "0781234567"):
        check(f"07X וירטואלי: {ph}", kind(ph) == "virtual")

    # 2. סלולרי אמיתי — 98% מהלקוחות שלנו. ⛔ סימון שגוי כאן חוסם לקוח אמיתי.
    for ph in ("0501234567", "0521234567", "0531234567", "0541234567",
               "0551234567", "0581234567", "+972541112223", "972501234567"):
        check(f"⛔ סלולרי לא מסומן: {ph}", kind(ph) == "mobile")

    # 3. קווי מזוהה בנפרד — לא הונאה, רק לא שכיח בקוד דיגיטלי.
    for ph in ("031234567", "0412345678", "086543210", "091234567"):
        check(f"קווי: {ph}", kind(ph) == "landline")

    # 4. קלט חסר/שבור לא מפיל ולא מסמן.
    for ph in ("", None, "123", "abc", "05"):
        check(f"קלט לא תקין → unknown: {ph!r}", kind(ph) == "unknown")

    # 5. מייל חד-פעמי — הרשימה המקומית לבדה, בלי רשת.
    net = main._disposable_email
    real_get = None
    try:
        import requests
        real_get = requests.get
        requests.get = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no net"))
        check("⛔ מקומי עובד בלי רשת", net("x@mailinator.com") is True)
        check("⛔ נכשל-פתוח: תקלת רשת ≠ חד-פעמי", net("x@some-unknown-domain-xyz.com") is False)
        check("דומיין תקין לא מסומן", net("x@gmail.com") is False)
        check("קלט ריק", net("") is False)
        check("בלי @ ", net("notanemail") is False)
    finally:
        if real_get:
            requests.get = real_get

    # 6. הרשימה המקומית התרחבה משמעותית — 24 היו מעט מדי.
    check("⛔ רשימה מורחבת", len(main._DISPOSABLE_DOMAINS) >= 50)
    check("כוללת את הוותיקים", "mailinator.com" in main._DISPOSABLE_DOMAINS)

    # 7. לגיטימייזר הקופה נכשל-פתוח — לעולם לא מעלה סיכון.
    import poller
    orig = poller.client
    poller.client = lambda: (_ for _ in ()).throw(RuntimeError("neworder down"))
    try:
        check("⛔ קופה נפולה → False (לא מוריד ולא מעלה)",
              main._pos_known_customer("0501234567") is False)
    finally:
        poller.client = orig

    print(f"עברו {passed}/{passed + len(failed)}")
    for f in failed:
        print("  ⛔", f)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
