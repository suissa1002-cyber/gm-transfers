"""צ'אט צוות: כתיבה מזוהה, תיוג + push ממוקד, קבצים בקישור חתום, נקרא/לא נקרא, מחיקה רכה."""
import sys, os, tempfile, io
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["TRANSFERS_DB_PATH"] = _tmp
os.environ["DATABASE_URL"] = ""
os.environ["ADMIN_PASSWORD"] = "legacy-secret"
from fastapi.testclient import TestClient
import main, db, wa_push
db.init_db()
main.cfg.ADMIN_PASSWORD = "legacy-secret"
pushed = []
wa_push.send_to_all = lambda **kw: pushed.append(kw)
c = TestClient(main.app)
fails = []
def check(label, cond, extra=""):
    print(f"{'✅' if cond else '❌'} {label}" + (f"  [{extra}]" if not cond else "")); cond or fails.append(label)
ADM = {"X-Admin-Key": "legacy-secret"}
db.device_register("dev-branch-0000000001", "קופה", "ua", "1.2.3.4", "1", "approved")
ids = {}
for un, nm, role in [("asi", "אסי", "owner"), ("udi", "אודי", "owner"), ("dana", "דנה", "user")]:
    ids[un] = c.post("/api/admin/users", headers=ADM, json={"username": un, "name": nm, "password": "pass123", "role": role}).json()["user"]["id"]
def login(un):
    t = c.post("/api/auth/login", json={"username": un, "password": "pass123"}, headers={"X-Device-Token": "dev-branch-0000000001"}).json()["token"]
    return {"X-User-Token": t, "X-Device-Token": "dev-branch-0000000001"}
A, U, D = login("asi"), login("udi"), login("dana")

r = c.post("/api/chat/send", headers=ADM, json={"text": "x"})
check("⛔ סיסמה משותפת לא כותבת בצ'אט (אין זהות)", r.status_code == 401)
check("עובדת בלי שום הרשאת פיצ'ר — כן משתמשת בצ'אט", c.get("/api/chat/messages", headers=D).status_code == 200)

r = c.post("/api/chat/send", headers=A, json={"text": "אודי, מה עם ההזמנה של כהן?", "mentions": [ids["udi"], 999], "wa_phone": "050-1234567"})
m = r.json()["message"]
import time; time.sleep(0.3)
check("הודעה נשמרת עם שם הכותב", m["user_name"] == "אסי" and m["user_id"] == ids["asi"])
check("תיוג: רק משתמש קיים נשמר (999 נזרק)", m["mentions"] == [ids["udi"]], m["mentions"])
check("קישור לשיחת לקוח נשמר נקי", m["wa_phone"] == "0501234567")
check("push רק לאודי", len(pushed) == 1 and pushed[0]["user_ids"] == [ids["udi"]] and "אסי" in pushed[0]["title"], pushed)

un = c.get("/api/chat/unread", headers=U).json()
check("אצל אודי: 1 לא נקראה, 1 תיוג", un["count"] == 1 and un["mentions"] == 1, un)
check("אצל אסי (הכותב): 0 לא נקראו", c.get("/api/chat/unread", headers=A).json()["count"] == 0)
c.post("/api/chat/read", headers=U, json={"last_id": m["id"]})
check("אחרי קריאה — 0", c.get("/api/chat/unread", headers=U).json()["count"] == 0)

png = io.BytesIO()
from PIL import Image
Image.new("RGB", (50, 50), (0, 200, 0)).save(png, "PNG")
up = c.post("/api/chat/upload", headers=D, files={"file": ("shot.png", png.getvalue(), "image/png")}).json()
r = c.post("/api/chat/send", headers=D, json={"text": "", "mentions": ["all"], **{k: up[k] for k in ("attach_key", "attach_name", "attach_mime", "attach_size")}})
m2 = r.json()["message"]
time.sleep(0.3)
check("@כולם → כל הפעילים חוץ ממני", sorted(m2["mentions"]) == sorted([ids["asi"], ids["udi"]]), m2["mentions"])
f = c.get(m2["attach_url"])
check("תמונה מוגשת בקישור החתום (בלי כותרות)", f.status_code == 200 and f.headers["content-type"] == "image/png" and f.content[:4] == b"\x89PNG")
bad = m2["attach_url"].split("?")[0] + "?t=deadbeef"
check("⛔ קישור עם חתימה שגויה — 403", c.get(bad).status_code == 403)
r = c.post("/api/chat/send", headers=D, json={"attach_key": "chat:../../etc", "text": "x"})
check("⛔ מפתח קובץ מזויף נדחה", r.status_code == 400)

check("⛔ אי אפשר למחוק הודעה של מישהו אחר", c.post(f"/api/chat/{m['id']}/delete", headers=D).status_code == 403)
c.post(f"/api/chat/{m2['id']}/delete", headers=D)
msgs = c.get("/api/chat/messages", headers=A).json()
last = msgs["messages"][-1]
check("מחיקה רכה: נשאר בתיעוד כ'נמחקה', בלי הקובץ", last.get("deleted") and not last.get("attach_url") and last["user_name"] == "דנה")
check("רשימת משתמשים לתיוג", {x["name"] for x in msgs["users"]} == {"אסי", "אודי", "דנה"})
new = c.get(f"/api/chat/messages?after={m['id']}", headers=U).json()["messages"]
check("סקר חי: after מחזיר רק חדשות", [x["id"] for x in new] == [m2["id"]])

# ── נושאים/טיקטים (מיני-Slack) ──
pushed.clear()
r = c.post("/api/chat/topics", headers=D, json={"title": "להזמין מדבקות למדפסת", "kind": "request",
                                                 "assignees": [ids["udi"]], "text": "נגמרו בסניף סיטי"})
t = r.json()["topic"]; time.sleep(0.3)
check("טיקט נפתח עם סוג, פותח ואחראי", t["kind"] == "request" and t["created_name"] == "דנה" and t["assignees"] == [ids["udi"]] and t["status"] == "open")
check("push לאחראי על טיקט חדש", any(p["user_ids"] == [ids["udi"]] and "בקשה" in p["title"] for p in pushed), pushed)
tl = c.get("/api/chat/topics", headers=U).json()
row = next(x for x in tl["topics"] if x["id"] == t["id"])
check("ברשימה אצל אודי: 2 לא נקראו + 'שלי'", row["unread"] == 2 and row["mine"] and "נגמרו" in row["last"], row)
th = c.get(f"/api/chat/messages?topic={t['id']}", headers=U).json()
check("שרשור נפרד מהכללי (הודעת מערכת + הודעה)", len(th["messages"]) == 2 and th["messages"][0]["system"] == 1 and th["topic"]["title"].startswith("להזמין"))
gen = c.get("/api/chat/messages", headers=U).json()["messages"]
check("הכללי לא מכיל את הודעות הטיקט", all(x["topic_id"] == 0 for x in gen))
pushed.clear()
c.post("/api/chat/send", headers=U, json={"text": "הזמנתי, יגיע מחר", "topic_id": t["id"]}); time.sleep(0.3)
check("תגובה בטיקט → push לפותחת", any(ids["dana"] in p["user_ids"] for p in pushed), pushed)
c.post(f"/api/chat/topics/{t['id']}", headers=U, json={"status": "done"}); time.sleep(0.3)
check("סגירה: סטטוס טופל + מי סגר", c.get("/api/chat/topics?status=done", headers=A).json()["topics"][0]["closed_name"] == "אודי")
check("סגירה נרשמת בשרשור", "טופל" in c.get(f"/api/chat/messages?topic={t['id']}", headers=A).json()["messages"][-1]["text"])
check("טיקט סגור לא ברשימת הפתוחים", all(x["id"] != t["id"] for x in c.get("/api/chat/topics", headers=A).json()["topics"]))
sysid = th["messages"][0]["id"]
check("⛔ אי אפשר למחוק הודעת מערכת", c.post(f"/api/chat/{sysid}/delete", headers=D).status_code == 403)
# ── נראות: עובדת רואה רק נושאים שהיא מעורבת בהם ──
priv = c.post("/api/chat/topics", headers=A, json={"title": "אודי - נושא קריטי", "kind": "task", "assignees": []}).json()["topic"]["id"]
c.post("/api/chat/send", headers=U, json={"text": "תגובה בדיקה", "topic_id": priv}); time.sleep(0.2)
dl = [x["id"] for x in c.get("/api/chat/topics", headers=D).json()["topics"]]
check("⛔ שיחה בין אסי לאודי לא מופיעה אצל דנה", priv not in dl, dl)
check("⛔ לא נספרת במונה שלה", str(priv) not in c.get("/api/chat/unread", headers=D).json()["by_topic"])
check("⛔ גם בכתובת ישירה — 404", c.get(f"/api/chat/messages?topic={priv}", headers=D).status_code == 404)
check("⛔ ואי אפשר לכתוב בה", c.post("/api/chat/send", headers=D, json={"text": "x", "topic_id": priv}).status_code == 404)
check("⛔ ולא לסגור אותה", c.post(f"/api/chat/topics/{priv}", headers=D, json={"status": "done"}).status_code == 404)
check("אודי (בעלים) רואה", priv in [x["id"] for x in c.get("/api/chat/topics", headers=U).json()["topics"]])
c.post("/api/chat/send", headers=A, json={"text": "@דנה תעזרי כאן", "topic_id": priv, "mentions": [ids["dana"]]})
check("אחרי שתייגו אותה — היא רואה את הנושא", priv in [x["id"] for x in c.get("/api/chat/topics", headers=D).json()["topics"]])
wt = c.post("/api/chat/topics", headers=A, json={"title": "שיחה עם 0508526256", "kind": "question", "assignees": [ids["udi"]], "wa_phone": "972508526256"}).json()["topic"]["id"]
la = c.get("/api/chat/wa-links", headers=A).json()["links"]
check("קישור לשיחת וואטסאפ: מופיע לפי 9 ספרות אחרונות", any(x["id"] == wt and x["status"] == "open" for x in la.get("508526256", [])), la)
check("⛔ עובדת לא מעורבת לא רואה את הקישור", "508526256" not in c.get("/api/chat/wa-links", headers=D).json()["links"])
own = c.post("/api/chat/topics", headers=D, json={"title": "שאלה שלי", "kind": "question", "assignees": []}).json()["topic"]["id"]
check("נושא שהיא פתחה — מופיע אצלה", own in [x["id"] for x in c.get("/api/chat/topics", headers=D).json()["topics"]])
pres = {x["name"]: x["online"] for x in c.get("/api/chat/topics", headers=A).json()["users"]}
check("נוכחות: מי שביצע בקשות — מחובר", pres["אסי"] and pres["אודי"] and pres["דנה"], pres)
main._PRESENCE[ids["udi"]] = time.time() - 600
pres = {x["name"]: x for x in c.get("/api/chat/topics", headers=A).json()["users"]}
check("נוכחות: 10 דקות בלי פעילות — לא מחובר", not pres["אודי"]["online"] and pres["אודי"]["seen_ago"] >= 600)

os.unlink(_tmp)
assert not fails, fails
print("\n✅ כל בדיקות הצ'אט עברו")
