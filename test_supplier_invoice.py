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

# ── צילום בטלפון (QR) + חשבונית של כמה עמודים ──
import io
from PIL import Image
def jpg():
    b = io.BytesIO(); Image.new("RGB", (600, 800), "white").save(b, "JPEG"); return b.getvalue()
h = c.post("/api/chat/handoff", headers=A).json()
check("QR נוצר עם קישור ו-SVG", h["url"].endswith("/snap/" + h["token"]) and "<svg" in h["qr_svg"])
check("דף הטלפון נפתח בלי כניסה", c.get(f"/snap/{h['token']}").status_code == 200)
check("⛔ טוקן מזויף", c.get("/snap/" + "0" * 32).status_code == 410 and c.post("/snap/" + "0" * 32 + "/upload", files={"file": ("a.jpg", jpg(), "image/jpeg")}).status_code == 410)
check("⛔ עובד אחר לא רואה את ה-QR שלי", c.get(f"/api/chat/handoff/{h['token']}", headers=Y).json()["state"] == "expired")
r1 = c.post(f"/snap/{h['token']}/upload", files={"file": ("p1.jpg", jpg(), "image/jpeg")}).json()
r2 = c.post(f"/snap/{h['token']}/upload", files={"file": ("p2.jpg", jpg(), "application/octet-stream")}).json()
st = c.get(f"/api/chat/handoff/{h['token']}", headers=A).json()
check("שני עמודים מהטלפון הגיעו למחשב (גם בלי סוג מוצהר)", r2.get("pages") == 2 and st["state"] == "done" and len(st["files"]) == 2
      and all(f["attach_mime"] == "image/jpeg" for f in st["files"]), st)
keys = [f["attach_key"] for f in st["files"]]
r = c.post("/api/chat/topics", headers=A, json={"kind": "invoice", "attach_keys": keys})
t2 = r.json()["topic"]["id"]
m = [x for x in c.get(f"/api/chat/messages?topic={t2}", headers=A).json()["messages"] if x.get("attach_url")]
check("כמה עמודים → PDF אחד בנושא", r.status_code == 200 and m and m[0]["attach_mime"] == "application/pdf" and "2 עמודים" in m[0]["attach_name"], m)

# ── מודול חשבוניות ספק ──
check("⛔ עובד לא רואה את מודול החשבוניות", c.get("/api/chat/invoices", headers=Y).status_code == 403)
L = c.get("/api/chat/invoices", headers=A).json()
check("כל החשבוניות ברשימה + רשימת ספקים", L["total"] >= 2 and "המילטון" in L["suppliers"] and L["invoices"][0]["url"], L["total"])
check("חיפוש מוצר", c.get("/api/chat/invoices?q=note 17", headers=A).json()["total"] >= 1
      and c.get("/api/chat/invoices?q=iphone", headers=A).json()["total"] == 0)
check("סינון ספק + תאריך + סטטוס", c.get("/api/chat/invoices?supplier=המילטון&frm=2026-10-01&to=2026-10-31&status=mismatch", headers=A).json()["total"] >= 1 and c.get("/api/chat/invoices?status=ok", headers=A).json()["total"] == 0
      and c.get("/api/chat/invoices?frm=2026-11-01", headers=A).json()["total"] == 0)

# ── הוספת עמודים אחרי העלאה + מחיקת נושא ──
k3 = c.post("/api/chat/upload?hq=1", headers=A, files={"file": ("p3.jpg", jpg(), "image/jpeg")}).json()["attach_key"]
check("⛔ עובד לא מוסיף עמודים לחשבונית של אחר", c.post(f"/api/chat/topics/{t2}/invoice/pages", headers=Y, json={"attach_keys": [k3]}).status_code in (403, 404))
r = c.post(f"/api/chat/topics/{t2}/invoice/pages", headers=A, json={"attach_keys": [k3]})
time.sleep(0.6)
si2 = db.si_for_topic(t2)
import fitz
got = db.wa_media_blob_get(si2["attach_key"])
check("עמוד נוסף → PDF של 3 עמודים + הודעה בנושא + קריאה מחדש", r.status_code == 200 and fitz.open(stream=got[1], filetype="pdf").page_count == 3
      and any("הוסיף/ה 1 עמודים" in (m["text"] or "") for m in c.get(f"/api/chat/messages?topic={t2}", headers=A).json()["messages"]), r.text)
check("⛔ עובד לא מוחק נושא שלא פתח", c.delete(f"/api/chat/topics/{t2}", headers=Y).status_code in (403, 404))
old_key = si2["attach_key"]
check("בעלים מוחק נושא חשבונית — הכל נמחק", c.delete(f"/api/chat/topics/{t2}", headers=A).status_code == 200
      and db.topic_get(t2) is None and db.si_for_topic(t2) is None and db.wa_media_blob_get(old_key) is None)
check("העובד מוחק נושא שהוא פתח", c.delete(f"/api/chat/topics/{tid}", headers=Y).status_code == 200)

# ── שידור למוצרים חדשים ב-GreenDay ──
import greenday
db.setting_set("greenday_mode", "primary", "test")
up3 = c.post("/api/chat/upload?hq=1", headers=A, files={"file": ("p.jpg", jpg(), "image/jpeg")}).json()
t3 = c.post("/api/chat/topics", headers=A, json={"kind": "invoice", **{k: up3[k] for k in ("attach_key", "attach_name", "attach_mime", "attach_size")}}).json()["topic"]["id"]
time.sleep(0.6)
SI.group_products = lambda lines: [{"name": "Xiaomi Redmi 10", "brand": "Xiaomi", "variants": [
    {"ref": "SA", "color": "שחור", "ram": "4GB", "storage": "128GB", "sku": "520900", "pos_name": "Redmi 10 4+128 Black", "qty": 2, "desc": "x"},
    {"ref": "SB", "color": "כחול", "ram": "6GB", "storage": "128GB", "sku": "520903", "pos_name": "Redmi 10 6+128 Blue", "qty": 1, "desc": "y"}]}]
check("⛔ עובד לא משדר", c.post(f"/api/chat/topics/{t3}/invoice/products", headers=Y).status_code in (403, 404))
g = c.post(f"/api/chat/topics/{t3}/invoice/products", headers=A).json()
check("קיבוץ: מוצר אחד עם 2 וריאציות", len(g["groups"]) == 1 and len(g["groups"][0]["variants"]) == 2, g)
pushes.clear()
import wa_push
wpush = []
wa_push.send_to_all = lambda **kw: wpush.append(kw) or 1
r = c.post(f"/api/chat/topics/{t3}/invoice/products/send", headers=A, json={"index": 0, "name": "Xiaomi Redmi 10"})
mid = r.json().get("monday_id")
gp = db.gd_product_get(mid) if mid else None
check("נוצר ב'מוצרים חדשים' בסטטוס המתנה עם מק״טים, צבעים, נפח וליסט וריאציות",
      gp and gp["grp"] == "topics" and gp["status"] == greenday.STATUS_HOLD and gp["pos_sku"] == "520900, 520903"
      and gp["colors"] == "שחור, כחול" and gp["storages"] == ["128GB"] and gp["list_type"] == "ליסט וריאציות", gp)
check("עדכון עם הווריאציות ומק״טים", gp and any("520903" in (x["body"] or "") for x in gp["updates"]))
udi = [x["id"] for x in db.users_list() if x["name"] == "udi"]
time.sleep(0.3)
check("אודי מתויג בנושא ומקבל התראה עם קישור למוצר", any(set(udi) <= set(m.get("mentions") or []) for m in db.chat_list(topic_id=t3, limit=50))
      and wpush and wpush[-1]["user_ids"] == udi and wpush[-1]["url"] == f"/?gd={mid}", wpush)
check("⛔ שידור כפול נחסם", c.post(f"/api/chat/topics/{t3}/invoice/products/send", headers=A, json={"index": 0}).status_code == 409)
g2 = c.post(f"/api/chat/topics/{t3}/invoice/products", headers=A).json()
check("בפתיחה הבאה: מסומן כשודר + 'כבר ב-GreenDay' לכל וריאציה", g2["sent"].get("0", {}).get("monday_id") == mid
      and all(v["exists"] and v["exists"]["monday_id"] == mid for v in g2["groups"][0]["variants"]))
import sys as _s; _s.path.insert(0, "../shared")
import greenday_dispatch as _gdd
check("הסוכנים לא נוגעים בסטטוס ההמתנה", _gdd.plan([{"name": "Xiaomi Redmi 10", "status": greenday.STATUS_HOLD, "grp": "topics", "image_link": "", "list_id": "", "created": "2026-01-01T00:00:00Z", "updates": []}]) == {"yuval": [], "gali": [], "qa": []})

os.unlink(_tmp)
print("\n" + ("✅ כל בדיקות חשבונית הספק עברו" if not fails else f"❌ נכשלו {len(fails)}: {fails}"))
sys.exit(1 if fails else 0)
