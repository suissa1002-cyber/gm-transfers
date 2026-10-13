"""התראות צוות לטלפון: שיוך מכשיר למשתמש, תיוג מגיע רק אליו, נושא בלי אחראי לכולם,
ועובד בלי הרשאת וואטסאפ לא מקבל התראות על הודעות לקוחות."""
import sys, os, tempfile, types, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["TRANSFERS_DB_PATH"] = _tmp
os.environ["DATABASE_URL"] = ""
os.environ["ADMIN_PASSWORD"] = "legacy-secret"
sent = []   # (endpoint, payload)
fake = types.ModuleType("pywebpush")
class WebPushException(Exception): pass
fake.WebPushException = WebPushException
fake.webpush = lambda subscription_info, data, **kw: sent.append((subscription_info["endpoint"], json.loads(data)))
sys.modules["pywebpush"] = fake
from fastapi.testclient import TestClient
import main, db, wa_push
db.init_db()
main.cfg.ADMIN_PASSWORD = "legacy-secret"
wa_push.VAPID_PRIVATE = "test"; wa_push.VAPID_PUBLIC = "pub"
c = TestClient(main.app)
fails = []
def check(label, cond, extra=""):
    print(f"{'✅' if cond else '❌'} {label}" + (f"  [{extra}]" if not cond else "")); cond or fails.append(label)
ADM = {"X-Admin-Key": "legacy-secret"}
db.device_register("dev-b-0000000000000001", "קופה", "ua", "1.2.3.4", "1", "approved")
ids = {}
for un, nm, role, pr in [("asi", "אסי", "owner", []), ("udi", "אודי", "owner", []), ("dana", "דנה", "user", ["orders"])]:
    ids[nm] = c.post("/api/admin/users", headers=ADM, json={"username": un, "name": nm, "password": "pass123", "role": role, "perms": pr}).json()["user"]["id"]
def login(un):
    t = c.post("/api/auth/login", json={"username": un, "password": "pass123"}, headers={"X-Device-Token": "dev-b-0000000000000001"}).json()["token"]
    return {"X-User-Token": t, "X-Device-Token": "dev-b-0000000000000001"}
A, U, D = login("asi"), login("udi"), login("dana")
UA_IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)"
def sub(ep): return {"endpoint": ep, "keys": {"p256dh": "x", "auth": "y"}}

# אייפון ישן של אסי — נרשם לפני המשתמשים (לא משויך) → נקשר בכניסה (push-bind)
wa_push.subscribe(sub("https://push/asi-iphone"), UA_IPHONE)
c.post("/api/chat/push-bind", headers=A, json={"endpoint": "https://push/asi-iphone"})
r = c.post("/api/chat/push-subscribe", headers=U, json={"sub": sub("https://push/udi-iphone"), "ua": UA_IPHONE})
check("אודי מפעיל התראות → המכשיר משויך אליו", r.status_code == 200)
r = c.post("/api/chat/push-subscribe", headers=D, json={"sub": sub("https://push/dana-phone"), "ua": "Android"})
check("עובדת בלי הרשאת וואטסאפ — יכולה להפעיל התראות צוות", r.status_code == 200)
st = c.get("/api/chat/push-status", headers=A).json()["devices"]
check("סטטוס: לכל אחד המכשיר שלו", st == {str(ids["אסי"]): ["iPhone"], str(ids["אודי"]): ["iPhone"], str(ids["דנה"]): ["Android"]}, st)

def eps(): time.sleep(0.4); out = sorted(e for e, _ in sent); sent.clear(); return out
c.post("/api/chat/send", headers=D, json={"text": "@אודי שאלה דחופה", "mentions": [ids["אודי"]]})
check("תיוג → רק לאייפון של אודי", eps() == ["https://push/udi-iphone"])
c.post("/api/chat/topics", headers=A, json={"title": "מי סוגר היום?", "kind": "question", "assignees": []})
check("נושא בלי אחראי → לכל הצוות חוץ מהכותב", eps() == ["https://push/dana-phone", "https://push/udi-iphone"])
tid = c.get("/api/chat/topics", headers=A).json()["topics"][0]["id"]
c.post("/api/chat/send", headers=U, json={"text": "אני סוגר", "topic_id": tid})
check("תגובה בנושא בלי אחראי → לכל השאר", eps() == ["https://push/asi-iphone", "https://push/dana-phone"])
c.post("/api/chat/send", headers=A, json={"text": "תודה", "topic_id": tid})
check("פותח הנושא מגיב → אודי (שהגיב קודם) מקבל", "https://push/udi-iphone" in eps())
t2 = c.post("/api/chat/topics", headers=A, json={"title": "להזמין מדבקות", "kind": "task", "assignees": [ids["אודי"]]}).json()["topic"]["id"]
eps()
c.post("/api/chat/send", headers=D, json={"text": "גם בסיטי חסר", "topic_id": t2})
check("נושא עם אחראי: מי שלא מעורב כותב → פותח+אחראי מקבלים", eps() == ["https://push/asi-iphone", "https://push/udi-iphone"])
c.post("/api/chat/send", headers=U, json={"text": "הזמנתי", "topic_id": t2})
check("אחראי עונה → פותח + דנה שהשתתפה מקבלות", eps() == ["https://push/asi-iphone", "https://push/dana-phone"])
r = c.post("/api/chat/push-test", headers=A).json()
check("בדיקה עצמית → רק המכשיר שלי", r["sent"] == 1 and eps() == ["https://push/asi-iphone"])
wa_push.send_to_all("לקוח כתב", "שלום", kind="wa")
check("⛔ הודעת לקוח בוואטסאפ לא מגיעה לטלפון של דנה (בלי הרשאה)", eps() == ["https://push/asi-iphone", "https://push/udi-iphone"])
wa_push.send_to_all("הזמנה חדשה", "#1", kind="order")
check("הזמנה חדשה — כן מגיעה לדנה (יש לה הרשאת הזמנות)", "https://push/dana-phone" in eps())
os.unlink(_tmp)
assert not fails, fails
print("\n✅ כל בדיקות ההתראות עברו")
