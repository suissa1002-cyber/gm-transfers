"""הרשמה מוקדמת להשקה: הודעה מהבאנר → כפתורים → רשימה → נרשם + ייצוא.

הנקודות שהבדיקה מגנה עליהן:
 (א) ההודעה מהבאנר **לא** נופלת לחיפוש מוצר (היא מכילה את שם הדגם);
 (ב) הנרשם נשמר **לפני** השאלות — נטישה באמצע לא מאבדת ליד;
 (ג) שאלה תמימה על הדגם ("כמה עולה") לא רושמת אף אחד;
 (ד) הצירוף הסופי זמין לדוח ההקצאה וב-CSV.
"""
import sys, os, tempfile, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["TRANSFERS_DB_PATH"] = _tmp
os.environ["DATABASE_URL"] = ""
os.environ["ADMIN_PASSWORD"] = "k"
from fastapi.testclient import TestClient
import main, db, wa, wa_bot
import seed_preorder_iphone_duo as seed
db.init_db()
main.cfg.ADMIN_PASSWORD = "k"

SENT = []
wa.send_text = lambda phone, text, **k: (SENT.append(("text", text)), "wamid.t")[1]
wa.send_buttons = lambda phone, body, buttons, **k: (SENT.append(("buttons", body, buttons)), "wamid.b")[1]
wa.send_list = lambda phone, body, rows, **k: (SENT.append(("list", body, rows)), "wamid.l")[1]
wa_bot._menu_tail = lambda phone: None
wa_bot._to_agent = lambda phone, note="": SENT.append(("agent", note))

db.preorder_campaign_save("iphone-duo", title="iPhone Duo", active=True,
                          keywords=seed.__dict__.get("KW", "iphone duo,אייפון דואו"),
                          intro=seed.INTRO, steps=seed.STEPS, outro=seed.OUTRO)
PHONE = "972500000001"
fails = []

def ck(name, cond, extra=""):
    print(("  ✓ " if cond else "  ✗ ") + name + ("" if cond else f"  → {extra}"))
    if not cond:
        fails.append(name)

# ── (ג) שאלה תמימה על הדגם — בלי כוונת הרשמה → אין רישום ──
ck("שאלת מחיר תמימה לא רושמת", wa_bot._preorder_match("כמה עולה iPhone Duo") is None)
ck("כוונת הרשמה בלי דגם לא רושמת", wa_bot._preorder_match("אשמח להירשם") is None)

# ── (א) ההודעה מהבאנר ──
camp = wa_bot._preorder_match("היי, אשמח להירשם לרשימה המוקדמת ל-iPhone Duo")
ck("הודעת הבאנר מזוהה", camp is not None and camp["slug"] == "iphone-duo")

SENT.clear()
wa_bot.handle(PHONE, "היי, אשמח להירשם לרשימה המוקדמת ל-iPhone Duo")
ck("נשלחה הודעת פתיחה", any(k == "text" for k, *_ in SENT))
btn = next((m for m in SENT if m[0] == "buttons"), None)
ck("שאלת הצבע ככפתורים (2 אפשרויות)", btn is not None and len(btn[2]) == 2,
   str(SENT))

# (ב) כבר רשום אף שעוד לא ענה
row = db.preorder_signup_get("iphone-duo", PHONE)
ck("נרשם לפני שענה (ליד לא אובד)", row is not None and row["status"] == "new")

# ── בחירת צבע → שאלת הנפח כרשימה (4 אפשרויות) ──
SENT.clear()
wa_bot.handle(PHONE, "Night Sky", reply_id=btn[2][1][0])
lst = next((m for m in SENT if m[0] == "list"), None)
ck("שאלת הנפח כרשימה (4 אפשרויות)", lst is not None and len(lst[2]) == 4, str(SENT))

# ── בחירת נפח → סיום ──
SENT.clear()
wa_bot.handle(PHONE, "512GB", reply_id=lst[2][1][0])
row = db.preorder_signup_get("iphone-duo", PHONE)
ck("שתי התשובות נשמרו", (row or {}).get("answers") == {"color": "night", "capacity": "512"},
   json.dumps(row.get("answers") if row else None, ensure_ascii=False))
ck("summary בתוויות ללקוח", (row or {}).get("summary") == "Night Sky · 512GB",
   (row or {}).get("summary"))
ck("הודעת סיום כוללת את הבחירה",
   any(k == "text" and "Night Sky · 512GB" in t for k, t in
       [(m[0], m[1]) for m in SENT if m[0] == "text"]), str(SENT))

# ── הקלדה חופשית במקום כפתור ──
P2 = "972500000002"
wa_bot.handle(P2, "אשמח להירשם לרשימה המוקדמת לאייפון דואו")
wa_bot.handle(P2, "star white")
r2 = db.preorder_signup_get("iphone-duo", P2)
ck("טקסט חופשי שתואם תווית נקלט", (r2 or {}).get("answers", {}).get("color") == "white",
   json.dumps((r2 or {}).get("answers"), ensure_ascii=False))

# ── (ד) ניהול: רשימה, פילוח, סטטוס, CSV ──
c = TestClient(main.app, raise_server_exceptions=True)
h = {"X-Admin-Key": "k"}
j = c.get("/api/admin/preorder/signups?campaign=iphone-duo", headers=h).json()
ck("API מחזיר 2 נרשמים", len(j["signups"]) == 2, str(len(j["signups"])))
ck("פילוח צבע לדוח ההקצאה", j["stats"]["by_step"]["color"] == {"Night Sky": 1, "Star White": 1},
   json.dumps(j["stats"].get("by_step"), ensure_ascii=False))

sid = j["signups"][0]["id"]
c.post(f"/api/admin/preorder/signup/{sid}", json={"status": "contacted"}, headers=h)
j2 = c.get("/api/admin/preorder/signups?campaign=iphone-duo&status=contacted", headers=h).json()
ck("סינון לפי סטטוס", len(j2["signups"]) == 1)
ck("נרשם מי יצר קשר", bool(j2["signups"][0].get("contacted_by")))

csv = c.get("/api/admin/preorder/export.csv?campaign=iphone-duo", headers=h)
ck("CSV עם BOM לאקסל עברי", csv.text.startswith("﻿"), repr(csv.text[:12]))
ck("CSV כולל כותרות בעברית", "צבע" in csv.text and "נפח" in csv.text, csv.text.split("\n")[0])
ck("CSV כולל תוויות ולא מזהים", "Night Sky" in csv.text and "512GB" in csv.text)

# ── חזרה של לקוח קיים שמשנה בחירה — לא נוצרת כפילות ──
wa_bot.handle(PHONE, "אשמח להירשם לרשימה המוקדמת ל-iPhone Duo")
all_rows = db.preorder_signups_list(campaign="iphone-duo", limit=100)
ck("אין כפילות ללקוח חוזר", len(all_rows) == 2, str(len(all_rows)))

print("\n" + ("✅ כל הבדיקות עברו" if not fails else f"❌ נכשלו: {fails}"))
sys.exit(1 if fails else 0)
