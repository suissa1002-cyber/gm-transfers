"""🔒 24/11/26: IP אמין (CF-Connecting-IP, לא XFF) + חסימה אוטומטית על גל דחיות, נשמרת ב-DB."""
import sys, os, tempfile, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["TRANSFERS_DB_PATH"] = _tmp
os.environ["DATABASE_URL"] = ""
os.environ["ADMIN_PASSWORD"] = "sec-secret"
from fastapi.testclient import TestClient
import main, db
db.init_db()
main.cfg.ADMIN_PASSWORD = "sec-secret"
sent = []
main._tg_admin = lambda msg, *a, **k: sent.append(msg)
c = TestClient(main.app)
fails = []
def check(label, cond, extra=""):
    print(f"{'✅' if cond else '❌'} {label}" + (f"  [{extra}]" if not cond else "")); cond or fails.append(label)
CF = lambda ip: {"CF-Connecting-IP": ip}
ADM = {"X-Admin-Key": "sec-secret"}

# 1) זיוף XFF לא משפיע
r = c.get("/health", headers={"X-Forwarded-For": "67.159.48.148", **CF("198.51.100.10")})
check("XFF מזויף של IP חסום — לא נחסם (IP אמיתי מ-CF)", r.status_code == 200, r.status_code)
r = c.get("/health", headers=CF("67.159.48.148"))
check("IP חסום אמיתי (CF) — 403", r.status_code == 403, r.status_code)

# 2) גל דחיות → חסימה + התראה + נשמר ב-DB
atk = CF("203.0.113.50")
for i in range(main._REJ_MAX):
    c.get("/api/admin/stellr/codes", headers=atk)
r = c.get("/health", headers=atk)
check(f"{main._REJ_MAX} דחיות ב-10 דק' → נחסם (403 גם על /health)", r.status_code == 403, r.status_code)
check("נשלחה התראת טלגרם על החסימה", any("נחסם אוטומטית" in m and "203.0.113.50" in m for m in sent), sent[-1:] )
saved = json.loads(db.setting_get("ip_bans") or "{}")
check("החסימה נשמרה ב-DB (שורדת פריסה)", "203.0.113.50" in saved, saved)
check("חסימה ראשונה = שעה", 3500 < saved["203.0.113.50"]["until"] - saved["203.0.113.50"]["at"] <= 3600)

# 3) טעינה מחדש מה-DB (סימולציית פריסה)
main._IP_BANS.clear(); main._SEC_STATE["loaded"] = False
r = c.get("/health", headers=atk)
check("אחרי 'פריסה' — עדיין חסום (נטען מ-DB)", r.status_code == 403, r.status_code)

# 4) IP מוכר (מכשיר מאושר) לא נחסם — רק התראה
db.device_register("dev-branch-city-000001", "קופה סיטי", "ua", "192.0.2.20", "3", "approved")
main._SEC_STATE["loaded"] = False; main._GOOD_IPS.clear()
good = CF("192.0.2.20")
for i in range(main._REJ_MAX + 5):
    c.get("/api/admin/pos/agent-config", headers=good)
r = c.get("/health", headers=good)
check("IP של סניף עם מפתח ישן — לא נחסם", r.status_code == 200, r.status_code)
check("…אבל נשלחה התראה 'IP מוכר'", any("IP מוכר" in m for m in sent))

# 5) IP שהוכיח זהות (מפתח ניהול נכון) הופך מוכר
me = CF("198.51.100.99")
c.get("/api/admin/ip-bans", headers={**ADM, **me})
check("כניסה מאומתת מסמנת IP מוכר", "198.51.100.99" in main._GOOD_IPS)

# 6) קריאה פנימית (בלי CF) לא נספרת ולא נחסמת
for i in range(main._REJ_MAX + 5):
    c.get("/api/admin/stellr/codes")
r = c.get("/health")
check("קריאה פנימית (בלי CF) — לא נחסמת", r.status_code == 200, r.status_code)

# 7) ניהול: רשימה + שחרור
lst = c.get("/api/admin/ip-bans", headers=ADM).json()
check("רשימת חסימות מציגה את התוקף", any(b["ip"] == "203.0.113.50" for b in lst["active"]), lst)
r = c.post("/api/admin/ip-bans/unban", headers=ADM, json={"ip": "203.0.113.50"})
check("שחרור חסימה", r.status_code == 200 and c.get("/health", headers=atk).status_code == 200, r.text[:100])

# 8) חסימה חוזרת מחמירה ל-6 שעות
for i in range(main._REJ_MAX):
    c.get("/api/admin/stellr/codes", headers=atk)
b = main._IP_BANS["203.0.113.50"]
check("חסימה שנייה = 6 שעות", b["strikes"] == 2 and 6 * 3600 - 100 < b["until"] - b["at"] <= 6 * 3600, b)

# 9) 401 של _device_gate (מכשיר לא מאושר) נספר
main.DEVICE_ENFORCE = True
x = CF("203.0.113.77")
for i in range(main._REJ_MAX):
    c.get("/api/transfers?branch_id=2", headers={**x, "X-Device-Token": "nope-nope-nope-nope"})
check("401 'מכשיר לא מאושר' נספר וחוסם", c.get("/health", headers=x).status_code == 403)

# 10) כפתור שחרור בטלגרם (קישור חתום)
r = c.get("/ip-action", params={"ip": "203.0.113.77", "sig": "bad"})
check("⛔ קישור שחרור עם חתימה שגויה — 403", r.status_code == 403)
r = c.get("/ip-action", params={"ip": "203.0.113.77", "sig": main._sig("203.0.113.77", "ip:unban")})
check("קישור שחרור חתום — משחרר", r.status_code == 200 and c.get("/health", headers=x).status_code == 200, r.status_code)

print("\n" + ("✅ הכל עבר" if not fails else f"❌ {len(fails)} נכשלו"))
sys.exit(1 if fails else 0)
