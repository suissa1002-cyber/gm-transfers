"""משתמשים והרשאות GreenOS — כל תרחיש קריטי: נעילה, פרצה, מכשיר, פעולות רגישות."""
import sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["TRANSFERS_DB_PATH"] = _tmp
os.environ["DATABASE_URL"] = ""
os.environ["ADMIN_PASSWORD"] = "legacy-secret"
os.environ.setdefault("WC_STORE_URL", "https://example.invalid")
os.environ.setdefault("WC_CONSUMER_KEY", "k"); os.environ.setdefault("WC_CONSUMER_SECRET", "s")
os.environ["CHECKS_PASSWORD"] = "chk_test"
from fastapi.testclient import TestClient
import main, db
db.init_db()
main.cfg.ADMIN_PASSWORD = "legacy-secret"
c = TestClient(main.app, raise_server_exceptions=False)
fails = []
passed = [0]
def check(label, cond, extra=""):
    print(f"{'✅' if cond else '❌'} {label}" + (f"  [{extra}]" if not cond and extra else "")); passed[0] += bool(cond); cond or fails.append(label)

ADM = {"X-Admin-Key": "legacy-secret"}
def T(tok, dev=None):
    h = {"X-User-Token": tok}
    if dev: h["X-Device-Token"] = dev
    return h

# ── אתחול: הסיסמה המשותפת ממשיכה לעבוד, ויוצרת את הבעלים הראשון ──
r = c.get("/api/admin/users", headers=ADM)
check("סיסמה משותפת עדיין עובדת (בלי אסימון)", r.status_code == 200, r.text[:80])
r = c.post("/api/admin/users", headers=ADM, json={"username": "asi", "name": "אסי", "password": "owner123", "role": "owner"})
check("יצירת בעלים ראשון עם הסיסמה המשותפת", r.status_code == 200, r.text[:120])
c.post("/api/admin/users", headers=ADM, json={"username": "udi", "name": "אודי", "password": "owner456", "role": "owner"})

# ── בעלים: נכנס מכל מכשיר ──
r = c.post("/api/auth/login", json={"username": "asi", "password": "owner123"})
check("בעלים נכנס בלי מכשיר מאושר", r.status_code == 200, r.text[:120])
ASI = r.json().get("token")
check("me מחזיר את אסי עם כל ההרשאות", c.get("/api/auth/me", headers=T(ASI)).json()["user"]["name"] == "אסי")
r = c.get("/api/admin/users", headers=T(ASI))
check("בעלים מחובר רואה ניהול משתמשים", r.status_code == 200 and len(r.json()["users"]) == 2)

# ── עובדת: רק הזמנות ──
r = c.post("/api/admin/users", headers=T(ASI), json={"username": "dana", "name": "דנה", "password": "dana123",
                                                       "perms": ["orders", "coupons.create"]})
check("פעולה רגישה בלי הפיצ'ר שלה נזרקת", r.json()["user"]["perms"] == ["orders"], str(r.json()))
r = c.post("/api/auth/login", json={"username": "dana", "password": "dana123"})
check("⛔ עובדת בלי מכשיר — נחסמת", r.status_code == 403 and "מכשיר" in r.text)
db.device_register("dev-pending-0000000001", "בית", "ua", "1.2.3.4", None, "pending")
r = c.post("/api/auth/login", json={"username": "dana", "password": "dana123"}, headers={"X-Device-Token": "dev-pending-0000000001"})
check("⛔ עובדת ממכשיר ממתין (מהבית) — נחסמת", r.status_code == 403)
db.device_register("dev-branch-0000000001", "קופה", "ua", "1.2.3.4", "1", "approved")
r = c.post("/api/auth/login", json={"username": "dana", "password": "dana123"}, headers={"X-Device-Token": "dev-branch-0000000001"})
check("עובדת ממכשיר מאושר — נכנסת", r.status_code == 200, r.text[:120])
DANA = r.json().get("token"); DEV = "dev-branch-0000000001"

# ── אכיפת פיצ'רים בשרת ──
r = c.post("/api/checks/gql", headers=T(DANA, DEV), json={"query": "{me{id}}"})
check("⛔ דנה בלי הרשאת צ'קים — 403", r.status_code == 403 and r.json().get("need") == "checks", r.text[:120])
r = c.get("/api/admin/diag", headers=T(DANA, DEV))
check("⛔ נתיב מערכת (בעלים בלבד) — 403", r.status_code == 403)
r = c.get("/api/admin/users", headers=T(DANA, DEV))
check("⛔ ניהול משתמשים — 403", r.status_code == 403)
check("⛔ אסימון של דנה ממכשיר אחר — נחסם", c.get("/api/admin/orders/bulk-trash", headers=T(DANA)).status_code in (403, 405))
r = c.post("/api/admin/orders/bulk-trash", headers=T(DANA, "dev-pending-0000000001"), json={"ids": []})
check("⛔ אסימון גנוב ממכשיר לא מאושר — 403", r.status_code == 403 and r.json().get("code") == "user_device")
check("נתיב ציבורי לא מושפע", c.get("/health").status_code == 200)

# ── פעולות רגישות ──
r = c.post("/api/admin/orders/bulk-trash", headers=T(DANA, DEV), json={"ids": []})
check("⛔ דנה בלי 'פח' — 403 לפני כל פעולה", r.status_code == 403 and "לפח" in r.text, r.text[:120])
r = c.post("/api/admin/orders/1/status", headers=T(DANA, DEV), json={"status": "cancelled"})
check("⛔ דנה בלי 'ביטול' — 403 על שינוי לבוטל", r.status_code == 403 and "ביטול" in r.text, r.text[:120])
uid = next(u["id"] for u in c.get("/api/admin/users", headers=T(ASI)).json()["users"] if u["username"] == "dana")
c.put(f"/api/admin/users/{uid}", headers=T(ASI), json={"perms": ["orders", "orders.trash"]})
r = c.post("/api/admin/orders/bulk-trash", headers=T(DANA, DEV), json={"ids": []})
check("אחרי מתן 'פח' — עוברת את ההרשאה (400 על רשימה ריקה)", r.status_code == 400 and "לא נבחרו" in r.text, r.text[:120])

# ── ייחוס ──
tok = main._CURRENT_USER.set({"name": "דנה", "username": "dana", "role": "user", "perms": []})
check("ייחוס: _actor_name מחזיר את שם המשתמש", main._actor_name(None, None) == "דנה")
main._CURRENT_USER.reset(tok)
check("ייחוס: בלי משתמש — כמו קודם", main._actor_name("legacy-secret", None) == "קונסולת ניהול")

# ── חסימה, שחזור סיסמה, בעלים אחרון ──
c.put(f"/api/admin/users/{uid}", headers=T(ASI), json={"active": False})
check("⛔ חסימה מנתקת מיד", c.get("/api/auth/me", headers=T(DANA)).status_code == 401)
r = c.post("/api/auth/login", json={"username": "dana", "password": "dana123"}, headers={"X-Device-Token": DEV})
check("⛔ משתמש חסום לא נכנס", r.status_code == 403 and "חסום" in r.text)
c.put(f"/api/admin/users/{uid}", headers=T(ASI), json={"active": True})
c.post(f"/api/admin/users/{uid}/password", headers=T(ASI), json={"password": "newpass9"})
r = c.post("/api/auth/login", json={"username": "dana", "password": "dana123"}, headers={"X-Device-Token": DEV})
check("⛔ אחרי שחזור — הסיסמה הישנה לא עובדת", r.status_code == 401)
r = c.post("/api/auth/login", json={"username": "dana", "password": "newpass9"}, headers={"X-Device-Token": DEV})
check("אחרי שחזור — הסיסמה החדשה עובדת", r.status_code == 200)
udi = next(u["id"] for u in c.get("/api/admin/users", headers=T(ASI)).json()["users"] if u["username"] == "udi")
c.put(f"/api/admin/users/{udi}", headers=T(ASI), json={"active": False})
asi_id = next(u["id"] for u in c.get("/api/admin/users", headers=T(ASI)).json()["users"] if u["username"] == "asi")
r = c.put(f"/api/admin/users/{asi_id}", headers=T(ASI), json={"role": "user"})
check("⛔ אי אפשר להוריד את הבעלים האחרון", r.status_code == 400 and "האחרון" in r.text)

# ── ניחוש סיסמה ──
for _ in range(5):
    c.post("/api/auth/login", json={"username": "asi", "password": "x"})
r = c.post("/api/auth/login", json={"username": "asi", "password": "owner123"})
check("⛔ אחרי 5 ניסיונות שגויים — נעילה 10 דקות", r.status_code == 429)

import perms as _pm
check("שידור בקשת העברה (שלב 2 של 'העבר' ממלאי חי) = הרשאת העברות, לא 'עדכונים לסניפים'",
      _pm.feature_for_path("/api/admin/broadcast") == "transfers" and _pm.feature_for_path("/api/admin/plan") == "transfers")

os.unlink(_tmp)
assert not fails, fails
print(f"\n✅ כל {passed[0]} הבדיקות עברו")
