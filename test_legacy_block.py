"""חסימת הסיסמה המשותפת בדפדפנים: דפדפן מנותק, שירותי שרת ממשיכים, רק בעלים בכניסה אישית מפעיל."""
import sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["TRANSFERS_DB_PATH"] = _tmp
os.environ["DATABASE_URL"] = ""
os.environ["ADMIN_PASSWORD"] = "legacy-secret"
from fastapi.testclient import TestClient
import main, db
db.init_db()
main.cfg.ADMIN_PASSWORD = "legacy-secret"
main._tg_admin = lambda *a, **k: None
c = TestClient(main.app)
fails = []
def check(label, cond, extra=""):
    print(f"{'✅' if cond else '❌'} {label}" + (f"  [{extra}]" if not cond else "")); cond or fails.append(label)
BROWSER = {"X-Admin-Key": "legacy-secret", "Sec-Fetch-Site": "same-origin", "X-Device-Token": "dev-x"}
SERVER = {"X-Admin-Key": "legacy-secret"}   # סוכן קופות / Stock Watcher (python requests)
c.post("/api/admin/users", headers=SERVER, json={"username": "asi", "name": "אסי", "password": "pass123", "role": "owner"})
T = {"X-User-Token": c.post("/api/auth/login", json={"username": "asi", "password": "pass123"}).json()["token"]}

check("לפני החסימה — דפדפן עם סיסמה משותפת עובד", c.get("/api/admin/users", headers=BROWSER).status_code == 200)
db.device_register("dev-branch-star-000001", "קופה סטאר", "ua", "1.2.3.4", "2", "approved")
c.get("/api/transfers?branch_id=2", headers={**BROWSER, "X-Device-Token": "dev-branch-star-000001"})
lk = c.get("/api/admin/legacy-key", headers=T).json()
row = next((d for d in lk["devices"] if d["name"] == "קופה סטאר"), None)
check("רשימת מכשירים עם סיסמה משותפת: מחשב סטאר מזוהה כסניף (לא יתבקש כניסה)", row and row["branch"] == "סטאר" and not row["admin_branch"], lk)
c.post("/api/admin/legacy-key", headers=T, json={"blocked": True})
r = c.get("/api/transfers?branch_id=2", headers={"X-Device-Token": "dev-branch-star-000001", "Sec-Fetch-Site": "same-origin"})
check("אחרי חסימה: מחשב סניף בלי הסיסמה (רק אישור מכשיר) — עובד", r.status_code == 200, r.status_code)
c.post("/api/admin/legacy-key", headers=T, json={"blocked": False})
r = c.post("/api/admin/legacy-key", headers=SERVER, json={"blocked": True})
check("⛔ אי אפשר לחסום עם הסיסמה המשותפת עצמה", r.status_code == 403)
r = c.post("/api/admin/legacy-key", headers=T, json={"blocked": True})
check("בעלים בכניסה אישית חוסם", r.status_code == 200 and r.json()["blocked"])
r = c.get("/api/admin/users", headers=BROWSER)
check("⛔ דפדפן עם סיסמה משותפת — 401 legacy_off", r.status_code == 401 and r.json().get("code") == "legacy_off", r.text[:100])
r = c.get("/api/admin/users", headers={"X-Admin-Key": "legacy-secret", "Sec-Fetch-Mode": "cors"})
check("⛔ גם דף נפרד (צ'קים/סידור) בלי מכשיר — נחסם", r.status_code == 401)
r = c.post("/api/admin/login", headers={"Sec-Fetch-Site": "same-origin"}, json={"password": "legacy-secret"})
check("⛔ מסך הכניסה הישן לא מקבל אותה", r.status_code == 403 and "בוטלה" in r.text)
check("שירות שרת (בלי Sec-Fetch) — ממשיך לעבוד", c.get("/api/admin/users", headers=SERVER).status_code == 200)
check("כניסה אישית — עובדת", c.get("/api/admin/users", headers=T).status_code == 200)
check("סטטוס המתג", c.get("/api/admin/legacy-key", headers=T).json()["blocked"] is True)
c.post("/api/admin/legacy-key", headers=T, json={"blocked": False})
check("החזרה — הדפדפן עובד שוב", c.get("/api/admin/users", headers=BROWSER).status_code == 200)
os.unlink(_tmp)
assert not fails, fails
print("\n✅ כל בדיקות החסימה עברו")
