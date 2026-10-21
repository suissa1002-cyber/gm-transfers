"""חשבונית ספק ב-GreenDay: חובה לצרף מסמך, עובד מעלה ולא רואה את הבדיקה, בעלים רואים דוח;
התאמת כמויות דטרמיניסטית; ממתין לכניסה בקופה → בדיקה חוזרת; התראה לבעלים רק כשלא תואם."""
import sys, os, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ.update(TRANSFERS_DB_PATH=_tmp, DATABASE_URL="", ADMIN_PASSWORD="legacy-secret")
from fastapi.testclient import TestClient
import main, db, supplier_invoice as SI
db.init_db(); main.cfg.ADMIN_PASSWORD = "legacy-secret"
c = TestClient(main.app)
fails = []
def check(label, cond, extra=""):
    print(f"{'✅' if cond else '❌'} {label}" + (f"  [{extra}]" if not cond else "")); cond or fails.append(label)

# ── לוגיקה טהורה ──
check("מספר מסמך: זהה / עם אפסים / חלקי ≥5 ספרות", SI.doc_numbers_match("264025611", "0264025611")
      and SI.doc_numbers_match("INV-264025611", "25611") and not SI.doc_numbers_match("1234", "91234"))
items = [{"description": "Note 17 Black", "code": "", "qty": 3}, {"description": "Buds", "code": "", "qty": 6},
         {"description": "Charger", "code": "", "qty": 2}, {"description": "Pro?", "code": "", "qty": 1}]
stock = [{"id": "A", "name": "Note 17 Black", "qty": 2}, {"id": "B", "name": "Buds 6 Play", "qty": 6},
         {"id": "C", "name": "Note 17 Pro", "qty": 1}, {"id": "X", "name": "Case", "qty": 4}]
mp = {0: {"ids": ["A"], "confidence": "high"}, 1: {"ids": ["B"], "confidence": "high"},
      2: {"ids": [], "confidence": "low"}, 3: {"ids": ["C"], "confidence": "low"}}
r = SI.compare(items, stock, mp)
check("חלקי / נכנס / לא נכנס / לבדיקה", [l["status"] for l in r["lines"]] == ["partial", "ok", "missing", "unsure"], r["lines"])
check("נכנס לקופה ולא בחשבונית", r["extras"] == [{"id": "X", "name": "Case", "qty": 4}] and r["overall"] == "mismatch")
r2 = SI.compare(items[:2], stock[:2] + [{"id": "B2", "name": "Buds", "qty": 0}], {0: {"ids": ["A"], "confidence": "high"}, 1: {"ids": ["B"], "confidence": "high"}})
check("שורה אחת → כמה פריטים: הכמות מתחלקת", r2["lines"][0]["got"] == 2 and r2["lines"][1]["got"] == 6)

# ── זרימה מלאה ──
ADM = {"X-Admin-Key": "legacy-secret"}
db.device_register("dev-i-0000000000000001", "קופה", "ua", "1.2.3.4", "1", "approved")
for un, role, pr in [("asi", "owner", []), ("udi", "owner", []), ("yakov", "user", ["orders"])]:
    c.post("/api/admin/users", headers=ADM, json={"username": un, "name": un, "password": "pass123", "role": role, "perms": pr})
def login(un):
    t = c.post("/api/auth/login", json={"username": un, "password": "pass123"}, headers={"X-Device-Token": "dev-i-0000000000000001"}).json()["token"]
    return {"X-User-Token": t, "X-Device-Token": "dev-i-0000000000000001"}
A, Y = login("asi"), login("yakov")
pushes = []
main._chat_push = lambda ids, title, body, tid=0: pushes.append((sorted(ids), title))
state = {"ops": []}
SI.extract = lambda data, mime: {"readable": True, "doc_type": "תעודת משלוח", "supplier": "המילטון", "doc_number": "264025611",
                                 "doc_date": "2026-10-21", "items": [{"description": "Note 17 Black", "code": "", "qty": 3}], "notes": ""}
SI.find_ops = lambda ex, created, client: (state["ops"], "doc" if state["ops"] else "")
SI.map_lines = lambda items, stock: {0: {"ids": ["A"], "confidence": "high"}}
main.poller.client = lambda: None

check("⛔ חשבונית בלי קובץ נדחית", c.post("/api/chat/topics", headers=Y, json={"kind": "invoice", "text": "x"}).status_code == 400)
up = c.post("/api/chat/upload?hq=1", headers=Y, files={"file": ("inv.png", b"\x89PNG fake", "image/png")}).json()
r = c.post("/api/chat/topics", headers=Y, json={"kind": "invoice", "text": "חסר מכשיר?", **{k: up[k] for k in ("attach_key", "attach_name", "attach_mime", "attach_size")}})
tid = r.json()["topic"]["id"]
check("עובד פותח חשבונית ספק — כותרת אוטומטית", r.status_code == 200 and r.json()["topic"]["title"].startswith("חשבונית ספק"))
time.sleep(0.6)
check("⛔ העובד לא רואה את הבדיקה", c.get(f"/api/chat/topics/{tid}/invoice", headers=Y).status_code == 403)
inv = c.get(f"/api/chat/topics/{tid}/invoice", headers=A).json()["invoice"]
check("אין עדיין כניסה בקופה → ממתין, בלי התראה", inv["status"] == "waiting_stock" and not [p for p in pushes if p[1].startswith("חשבונית ספק")], inv)
msgs = c.get(f"/api/chat/messages?topic={tid}", headers=Y).json()["messages"]
check("ההודעה הראשונה מכילה את הקובץ והטקסט", any(m.get("attach_url") and m["text"] == "חסר מכשיר?" for m in msgs))

state["ops"] = [{"id": "16849", "createDate": "2026-10-21T12:47:41", "documentNumber": "264025611", "employee": "אודי",
                 "supplier": {"name": "המילטון"}, "totalQuantity": 2, "operationType": 1,
                 "stockItems": [{"id": "A", "name": "Note 17 Black", "quantity": 2}]}]
main._invoice_recheck_job()
inv = c.get(f"/api/chat/topics/{tid}/invoice", headers=A).json()["invoice"]
check("הכניסה הופיעה → בדיקה חוזרת → לא תואם (2 מתוך 3)", inv["status"] == "mismatch" and inv["result"]["lines"][0]["got"] == 2, inv)
owners = sorted(u["id"] for u in db.users_list() if u["role"] == "owner")
check("התראה לבעלים בלבד", [p for p in pushes if "לא תואמת" in p[1]] == [(owners, "חשבונית ספק לא תואמת לכניסה למלאי")], pushes)
main._invoice_recheck_job()
check("סטטוס סופי — לא נבדק ולא מתריע שוב", len([p for p in pushes if "לא תואמת" in p[1]]) == 1)
check("⛔ עובד לא מפעיל בדיקה חוזרת", c.post(f"/api/chat/topics/{tid}/invoice/recheck", headers=Y).status_code == 403)
check("⛔ צירוף קובץ לנושא רגיל נדחה", c.post("/api/chat/topics", headers=Y, json={"title": "x", "kind": "question", "attach_key": up["attach_key"]}).status_code == 400)

os.unlink(_tmp)
print("\n" + ("✅ כל בדיקות חשבונית הספק עברו" if not fails else f"❌ נכשלו {len(fails)}: {fails}"))
sys.exit(1 if fails else 0)
