"""GreenDay · תזכורות פנימיות: יצירה, הופעה בלוח גם בארכיון, דחייה, טופל, התראה פעם אחת לבעלים בלבד."""
import sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ.update(TRANSFERS_DB_PATH=_tmp, DATABASE_URL="", ADMIN_PASSWORD="legacy-secret", MONDAY_API_TOKEN="x")
from datetime import datetime, timedelta
from fastapi.testclient import TestClient
import main, db, wa_push
db.init_db(); main.cfg.ADMIN_PASSWORD = "legacy-secret"
db.setting_set("greenday_mode", "primary", "test")
c = TestClient(main.app)
fails = []
def check(label, cond, extra=""):
    print(f"{'✅' if cond else '❌'} {label}" + (f"  [{extra}]" if not cond else "")); cond or fails.append(label)
ADM = {"X-Admin-Key": "legacy-secret"}
db.device_register("dev-r-0000000000000001", "קופה", "ua", "1.2.3.4", "1", "approved")
for un, role, pr in [("asi", "owner", []), ("udi", "owner", []), ("dana", "user", ["orders"])]:
    c.post("/api/admin/users", headers=ADM, json={"username": un, "name": un, "password": "pass123", "role": role, "perms": pr})
def login(un):
    t = c.post("/api/auth/login", json={"username": un, "password": "pass123"}, headers={"X-Device-Token": "dev-r-0000000000000001"}).json()["token"]
    return {"X-User-Token": t, "X-Device-Token": "dev-r-0000000000000001"}
A, D = login("asi"), login("dana")
owners = {u["id"] for u in db.users_list() if u["role"] == "owner"}

mid = db.gd_create_local("REDMI Note 17 – באנר מתנה", "group_mm3y9b6z", {"status": "הושלם", "wc_id": "51841"})
today = main._gd_today()
past = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
future = (datetime.strptime(today, "%Y-%m-%d") + timedelta(days=40)).strftime("%Y-%m-%d")

check("⛔ עובדת לא יוצרת תזכורת", c.post(f"/api/greenday/products/{mid}/reminders", headers=D, json={"due": future, "text": "x"}).status_code == 403)
check("⛔ תאריך לא תקין נדחה", c.post(f"/api/greenday/products/{mid}/reminders", headers=A, json={"due": "30/11", "text": "x"}).status_code == 400)
r = c.post(f"/api/greenday/products/{mid}/reminders", headers=A, json={"due": future, "text": "המבצע מסתיים — להאריך?"}).json()
rid = r["id"]
p = c.get(f"/api/greenday/products/{mid}", headers=A).json()["product"]
check("התזכורת במוצר + עדכון 'נקבעה תזכורת'", len(p["reminders"]) == 1 and any("נקבעה תזכורת" in u["body"] for u in p["updates"]))

c.post(f"/api/greenday/products/{mid}", headers=A, json={"field": "grp", "value": "group_mkzqzksn"})
b = c.get("/api/greenday/board", headers=A).json()
check("מוצר בארכיון — התזכורת עדיין בלוח", [x["id"] for x in b["reminders"]] == [rid] and b["reminders"][0]["grp"] == "group_mkzqzksn" and b["today"] == today)

sent = []
wa_push.send_to_all = lambda **kw: sent.append(kw) or 1
main._greenday_reminders_job()
check("תזכורת עתידית — אין התראה", not sent)

c.post(f"/api/greenday/reminders/{rid}", headers=A, json={"action": "edit", "due": past})
main._greenday_reminders_job()
hour = datetime.now(__import__("zoneinfo").ZoneInfo("Asia/Jerusalem")).hour
if hour >= 9:
    check("הגיע היום → push לבעלים בלבד, עם קישור למוצר", len(sent) == 1 and set(sent[0]["user_ids"]) == owners and sent[0]["url"] == f"/?gd={mid}" and sent[0]["kind"] == "greenday", sent)
    main._greenday_reminders_job()
    check("לא נשלחת פעמיים", len(sent) == 1)
    ups = c.get(f"/api/greenday/products/{mid}", headers=A).json()["product"]["updates"]
    check("נרשם עדכון 'תזכורת להיום' במוצר", any(u["author"] == "תזכורת" for u in ups))
    c.post(f"/api/greenday/reminders/{rid}", headers=A, json={"action": "edit", "due": past, "text": "נדחה"})
    check("דחייה מאפסת את סימון ההתראה", db.gd_reminder_get(rid)["notified_at"] is None)
else:
    print("ℹ️  לפני 09:00 בישראל — בדיקות ההתראה דולגו (ההתראה לא יוצאת בלילה)")
    check("לפני 09:00 — אין התראה", not sent)

c.post(f"/api/greenday/reminders/{rid}", headers=A, json={"action": "done"})
b = c.get("/api/greenday/board", headers=A).json()
check("טופל → יוצא מהלוח", b["reminders"] == [] and db.gd_reminder_get(rid)["done_by"] == "asi")
c.post(f"/api/greenday/reminders/{rid}", headers=A, json={"action": "undo"})
check("פתח מחדש → חוזר", len(c.get("/api/greenday/board", headers=A).json()["reminders"]) == 1)
c.post(f"/api/greenday/reminders/{rid}", headers=A, json={"action": "delete"})
check("מחיקה", db.gd_reminder_get(rid) is None)

print("\n" + ("✅ כל בדיקות התזכורות עברו" if not fails else f"❌ נכשלו {len(fails)}: {fails}"))
sys.exit(1 if fails else 0)
