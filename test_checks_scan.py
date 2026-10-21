"""סריקת צ'ק: הרשאות, פענוח (Claude מדומה), התאמת ספק/בנק, QR מהטלפון עם דף ייעודי לצ'ק."""
import sys, os, tempfile, io
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ.update(TRANSFERS_DB_PATH=_tmp, DATABASE_URL="", ADMIN_PASSWORD="legacy-secret", CHECKS_PASSWORD="chk-secret")
from fastapi.testclient import TestClient
from PIL import Image
import main, db, supplier_invoice as SI
db.init_db(); main.cfg.ADMIN_PASSWORD = "legacy-secret"
c = TestClient(main.app)
fails = []
def check(label, cond, extra=""):
    print(f"{'✅' if cond else '❌'} {label}" + (f"  [{extra}]" if not cond else "")); cond or fails.append(label)
def jpg():
    b = io.BytesIO(); Image.new("RGB", (900, 400), "white").save(b, "JPEG"); return b.getvalue()
SI._ask = lambda content, schema, effort="medium": {"readable": True, "check_number": "3004571", "payee": "המילטון חשמל", "supplier_match": "המילטון",
                                   "amount": 8500, "amount_words": "שמונת אלפים חמש מאות", "amounts_agree": True, "date": "2026-11-15",
                                   "bank": "בנק הפועלים בע\"מ", "confidence": "high", "notes": ""}
K = {"X-Checks-Key": "chk-secret"}
check("⛔ בלי הרשאה", c.post("/api/checks/scan", files={"file": ("c.jpg", jpg(), "image/jpeg")}).status_code == 401)
r = c.post("/api/checks/scan", headers=K, files={"file": ("c.jpg", jpg(), "image/jpeg")}, data={"suppliers": '["המילטון","GSM"]'}).json()
ch = r.get("check") or {}
check("פענוח: מספר, ספק מהרשימה, סכום, תאריך, בנק לרשימה של הטופס", ch.get("check_number") == "3004571" and ch.get("supplier_match") == "המילטון"
      and ch.get("amount") == 8500 and ch.get("date") == "2026-11-15" and ch.get("bank_option") == "בנק פועלים", r)
r = c.post("/api/checks/scan", headers=K, files={"file": ("c.jpg", jpg(), "image/jpeg")}, data={"suppliers": '["GSM"]'}).json()
check("ספק שלא ברשימה → לא מוצמד (payee נשאר להצגה)", r["check"]["supplier_match"] == "" and r["check"]["payee"] == "המילטון חשמל")
h = c.post("/api/checks/handoff", headers=K).json()
page = c.get(f"/snap/{h['token']}").text
check("QR → דף טלפון של צ'ק", "צילום צ'ק" in page and "צלם צ'ק" in page)
check("ממתין לפני צילום", c.get(f"/api/checks/handoff/{h['token']}", headers=K).json()["state"] == "waiting")
c.post(f"/snap/{h['token']}/upload", files={"file": ("p.jpg", jpg(), "image/jpeg")})
st = c.get(f"/api/checks/handoff/{h['token']}", headers=K).json()
check("הצילום מהטלפון הגיע", st["state"] == "done" and st["files"][0]["url"])
r = c.post("/api/checks/scan", headers={**K, "Content-Type": "application/json"}, json={"attach_key": st["files"][0]["attach_key"], "suppliers": ["המילטון"]}).json()
check("פענוח של צילום מהטלפון לפי מפתח", r.get("check", {}).get("amount") == 8500, r)
check("⛔ טוקן QR של צ'קים לא נגיש בלי הרשאה", c.get(f"/api/checks/handoff/{h['token']}").status_code == 401)
# ── שתי קריאות: אי-הסכמה → conflicts + ביטחון נמוך; כינוי שנלמד גובר ──
reads = iter([
    {"readable": True, "check_number": "3004572", "payee": "המילטון חשמל ואלקטרוניקה בע\"מ", "supplier_match": "", "amount": 1250,
     "amount_words": "", "amounts_agree": True, "date": "2026-12-03", "bank": "לאומי", "confidence": "high", "notes": ""},
    {"readable": True, "check_number": "3004572", "payee": "המילטון חשמל ואלקטרוניקה בע\"מ", "supplier_match": "", "amount": 7250,
     "amount_words": "", "amounts_agree": True, "date": "2026-12-03", "bank": "לאומי", "confidence": "high", "notes": "1 או 7?"},
    {"readable": True, "check_number": "3004572", "payee": "", "supplier_match": "", "amount": 0,   # מכריע שלא הצליח
     "amount_words": "", "amounts_agree": True, "date": "", "bank": "", "confidence": "low", "notes": ""}])
SI._ask = lambda content, schema, effort="medium": next(reads)
r = c.post("/api/checks/scan", headers=K, files={"file": ("c.jpg", jpg(), "image/jpeg")}, data={"suppliers": '["המילטון"]'}).json()["check"]
r0 = {"readable": True, "check_number": "652", "payee": "המילטון", "supplier_match": "המילטון", "amount": 0, "amount_words": "",
      "amounts_agree": True, "date": "2026-12-23", "bank": "הבינלאומי", "confidence": "medium", "notes": "x"}
check("קריאות שונות בסכום → conflicts עם שתי האפשרויות + ביטחון נמוך", r["conflicts"].get("amount") == [1250, 7250] and r["confidence"] == "low"
      and r["bank_option"] == "בנק לאומי" and r["supplier_match"] == "", r)
reads = iter([dict(r0, amount=5272), dict(r0, amount=47272), dict(r0, amount=5272, notes="")])
SI._ask = lambda content, schema, effort="medium": next(reads)
r = c.post("/api/checks/scan", headers=K, files={"file": ("c.jpg", jpg(), "image/jpeg")}, data={"suppliers": '["המילטון"]'}).json()["check"]
check("קריאה מכריעה מסכימה עם אחת → ההתנגשות נפתרת", r["amount"] == 5272 and not r["conflicts"] and r.get("resolved") == ["amount"], r)
check("בנק: 'הבינלאומי' → הבנק הבינלאומי (לא לאומי)", main._check_bank_option("הבנק הבינלאומי הראשון") == "הבנק הבינלאומי"
      and main._check_bank_option("בנק לאומי") == "בנק לאומי" and main._check_bank_option("First International Bank") == "הבנק הבינלאומי")
check("לימוד: שם רשמי → ספק במערכת", c.post("/api/checks/scan-learn", headers=K, json={"payee": "המילטון חשמל ואלקטרוניקה בע\"מ", "supplier": "המילטון"}).json()["ok"])
SI._ask = lambda content, schema, effort="medium": {"readable": True, "check_number": "1", "payee": "המילטון חשמל ואלקטרוניקה בעמ", "supplier_match": "",
    "amount": 100, "amount_words": "", "amounts_agree": True, "date": "2026-12-03", "bank": "", "confidence": "high", "notes": ""}
r = c.post("/api/checks/scan", headers=K, files={"file": ("c.jpg", jpg(), "image/jpeg")}, data={"suppliers": '["המילטון"]'}).json()["check"]
check("סריקה הבאה: הכינוי שנלמד מזהה את הספק (גם עם איות אחר של בע\"מ)", r["supplier_match"] == "המילטון" and r.get("alias_used"), r)
check("⛔ לימוד בלי הרשאה", c.post("/api/checks/scan-learn", json={"payee": "x", "supplier": "y"}).status_code == 401)
os.unlink(_tmp)
print("\n" + ("✅ כל בדיקות סריקת הצ'ק עברו" if not fails else f"❌ נכשלו {len(fails)}: {fails}"))
sys.exit(1 if fails else 0)
