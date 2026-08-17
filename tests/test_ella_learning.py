"""בדיקות ללולאת הלמידה של אלה.

⚠️ הרקע (אסי, 16/08/2026): 62 לקחים נערמו בתור אישור בקצב ~9 ליום. אלה לא
למדה כלום עד שאסי לחץ על כל אחד, והפלייבוק — 228 לקחים, 53,765 תווים —
נכנס לכל הודעה של כל לקוח וגדל בלי הגבלה.

השינוי: עובדות נכנסות לבד, שיקול דעת ממתין לאסי, וכפילויות מתאחדות.

⛔ הבדיקות בוחנות **החלטות** — מה נכנס לבד ומה ממתין. לא את קוד המקור.
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

    needs = main._ella_needs_asi

    # 1. עובדות נטו — נכנסות לבד. זו כל מטרת השינוי.
    for txt in ("מגבלת כרטיס דביט היא 3000 לפעימה אחת מול הבנק",
                "אחריות יבואן מקביל נמדדת ממועד המסירה ולא ממועד ההזמנה",
                "סניף סטאר סגור בימי שישי אחרי 14:00",
                "הודעת חוסר במלאי נשלחת אוטומטית בכל ביטול, גם ידני"):
        check(f"עובדה נכנסת לבד: {txt[:26]}", needs("FACT", txt) is False)

    # 2. ⛔ הקריטי: כסף והתחייבות תמיד עוברים דרך אסי, גם אם אלה תייגה FACT.
    for txt in ("אפשר להציע הנחה קטנה כשהלקוח מצטט מחיר מתחרה",
                "במקרה כזה נותנים פיצוי של 50 שקל",
                "אם הלקוח מתעקש אפשר לתת מגן מסך בחינם",
                "כדאי להציע את המבצע של נפח כפול",
                "מתחייבים ללקוח שהמשלוח יגיע מחר"):
        check(f"⛔ שיקול דעת ממתין: {txt[:26]}", needs("FACT", txt) is True)

    # 3. תיוג חסר או לא מוכר → ממתין. ברירת המחדל שמרנית.
    check("בלי תיוג → ממתין", needs("", "מגבלת דביט היא 3000") is True)
    check("CALL → ממתין", needs("CALL", "מגבלת דביט היא 3000") is True)
    check("תיוג זר → ממתין", needs("MAYBE", "מגבלת דביט היא 3000") is True)
    check("⛔ טקסט ריק → ממתין", needs("FACT", "") is True)
    check("⛔ לקח קצר מדי → ממתין", needs("FACT", "סניף סגור") is True)
    # ⛔ נטיות בעברית: "מתחייבים" חייב להיתפס גם כשהשורש נכתב "התחייב"
    for form in ("מתחייבים ללקוח שהמשלוח יגיע מחר בבוקר לכתובת",
                 "אין להתחייב מול הלקוח על מועד הגעה של משלוח",
                 "כשמוותרים ללקוח על דמי משלוח בגלל עיכוב ארוך"):
        check(f"נטייה נתפסת: {form[:22]}", needs("FACT", form) is True)

    # 4. איחוד: שני ניסוחים של אותו לקח מזוהים ככפילות, שונים — לא.
    w = main._ella_words
    same_a = w("מגבלת כרטיס דביט היא 3000 שקל לפעימה אחת מול הבנק")
    same_b = w("מגבלת כרטיס דביט היא 3000 שקל לפעימה אחת מול הבנק בלבד")
    diff = w("אחריות יבואן מקביל נמדדת ממועד המסירה ללקוח")
    ratio = lambda a, b: len(a & b) / max(len(a | b), 1)  # noqa: E731
    check("ניסוח כמעט זהה → כפילות", ratio(same_a, same_b) >= main._ELLA_DUP_RATIO)
    check("⛔ לקח אחר → לא כפילות", ratio(same_a, diff) < main._ELLA_DUP_RATIO)

    # 5. גדרות הבטיחות של האיחוד. (הסף עצמו נבדק בסעיף 7)
    check("⛔ תקרת מחיקות לריצה", main._ELLA_DUP_MAX <= 5)

    # 6. איחוד לא נוגע בפלייבוק קטן — אין מה לאחד ב-19 לקחים.
    calls = []
    orig_list, orig_del, orig_tg = db_list_swap(calls)
    try:
        main._ella_consolidate_job()
        check("⛔ פלייבוק קטן — בלי מחיקות", calls == [])
    finally:
        main.db.kb_list, main.db.kb_delete, main._tg_admin = orig_list, orig_del, orig_tg

    # 7. הסף שאוחד: 0.85 לא תפס כפילות אמיתית של 0.82 בריצה יבשה על 288 לקחים.
    check("סף איחוד תופס 82%", main._ELLA_DUP_RATIO <= 0.82)
    check("⛔ סף לא מתירני מדי", main._ELLA_DUP_RATIO >= 0.7)
    check("סף הדיווח נמוך מסף המחיקה", main._ELLA_DUP_SUGGEST < main._ELLA_DUP_RATIO)

    print(f"עברו {passed}/{passed + len(failed)}")
    for f in failed:
        print("  ⛔", f)
    return 0 if not failed else 1


def db_list_swap(calls):
    """מחליף את המסד בזיכרון — 19 לקחים, מתחת לסף שבו האיחוד בכלל פועל."""
    ol, od, ot = main.db.kb_list, main.db.kb_delete, main._tg_admin
    main.db.kb_list = lambda: [{"id": i, "lesson": f"לקח מספר {i} על נושא נפרד לגמרי"}
                               for i in range(19)]
    main.db.kb_delete = lambda i: calls.append(i)
    main._tg_admin = lambda m: None
    return ol, od, ot


if __name__ == "__main__":
    sys.exit(run())
