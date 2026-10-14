#!/usr/bin/env python3
"""זריעת קמפיין ההרשמה המוקדמת ל-iPhone Duo (אסי, 14/10/2026).

idempotent — אפשר להריץ שוב אחרי שינוי טקסט. הרצה: python3 seed_preorder_iphone_duo.py
עריכה שוטפת נעשית ב-/preorder, לא כאן.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

INTRO = ("רשמתי אותך לרשימה המוקדמת ל-iPhone Duo ✅\n\n"
         "המחיר עדיין לא נקבע — ברגע שייסגר נחזור אליך אישית עם כל הפרטים. "
         "מי שברשימה המוקדמת מקבל קדימות בהקצאה, והמלאי הראשון מוגבל.\n\n"
         "שתי שאלות קצרות כדי לשמור לך את הדגם הנכון 👇")

OUTRO = ("סגרנו 🙌 אתה ברשימה המוקדמת ל-iPhone Duo, ותהיה מהראשונים שנעדכן.\n"
         "אם תרצה לשנות משהו — פשוט כתוב לי כאן.")

STEPS = [
    {"id": "color", "q_short": "צבע", "q": "איזה צבע מעניין אותך?",
     "opts": [["white", "Star White"], ["night", "Night Sky"]]},
    {"id": "capacity", "q_short": "נפח", "q": "ואיזה נפח אחסון?",
     "button": "בחירת נפח", "title": "נפח אחסון",
     "opts": [["256", "256GB"], ["512", "512GB"], ["1tb", "1TB"], ["2tb", "2TB"]]},
]

if __name__ == "__main__":
    db.init_db()
    cid = db.preorder_campaign_save(
        "iphone-duo", title="iPhone Duo", active=True,
        keywords="iphone duo,איפון דואו,אייפון דואו",
        intro=INTRO, steps=STEPS, outro=OUTRO,
        note="באנר 'בקרוב במלאי' בדף הבית → wa.me עם הודעה מוכנה. "
             "השקה רשמית 23/10/2026. ⛔ בלי מחיר עד החלטת אסי.")
    print("campaign saved id=", cid)
    print(db.preorder_campaign_get("iphone-duo"))
