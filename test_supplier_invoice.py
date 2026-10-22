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
check("קיבוץ: מוצר אחד עם 2 וריאציות + רשימת נוסחי אחריות", len(g["groups"]) == 1 and len(g["groups"][0]["variants"]) == 2 and "שנה אחריות גרין מובייל" in g["warranty_options"], g)
pushes.clear()
import wa_push
wpush = []
wa_push.send_to_all = lambda **kw: wpush.append(kw) or 1
import product_notes as PN
PN.examples_for = lambda name, brand, rows, k=6: []
PN.ref_product = lambda *a, **kw: None
PN.write_note = lambda group, sup, ex, ref: {"note": "יש להזין את המוצר תחת הקטגוריות הקיימות הבאות : מכשירים", "confidence": "medium", "doubts": "גודל מסך"}
main._inv_enrich = lambda gs: [g.update(site="new", wc_parent=None) or [v.update(price=699.0 if v["sku"] == "520900" else 799.0, wc=None) for v in g["variants"]] for g in gs] and gs
check("⛔ בלי אחריות — לא משודר", c.post(f"/api/chat/topics/{t3}/invoice/products/send", headers=A, json={"index": 0}).status_code == 400)
r = c.post(f"/api/chat/topics/{t3}/invoice/products/send", headers=A, json={"index": 0, "name": "Xiaomi Redmi 10", "warranty": "שנה אחריות גרין מובייל"})
mid = r.json().get("monday_id")
time.sleep(0.8)
gp = db.gd_product_get(mid) if mid else None
check("נוצר ב'מוצרים חדשים' עם מק״טים, צבעים, נפח, ליסט וריאציות ומחיר מהקופה",
      gp and gp["grp"] == "topics" and gp["pos_sku"] == "520900, 520903" and gp["colors"] == "שחור, כחול"
      and gp["storages"] == ["128GB"] and gp["list_type"] == "ליסט וריאציות" and gp["price"] == 699.0, gp)
check("פאבל כתב הוראות העלאה והעביר ל'ממתין' (בלי אודי)", gp and gp["status"] == "ממתין"
      and any(x["author"] == "פאבל (QA)" and "קטגוריות" in x["body"] for x in gp["updates"]) and not wpush, (gp or {}).get("status"))
check("עדכון עם הווריאציות, מק״טים, מחירים והאחריות שנבחרה", gp and any("520903" in (x["body"] or "") and "₪799" in x["body"] and "שנה אחריות גרין מובייל" in x["body"] for x in gp["updates"]))
check("⛔ שידור כפול נחסם", c.post(f"/api/chat/topics/{t3}/invoice/products/send", headers=A, json={"index": 0, "warranty": "שנה אחריות גרין מובייל"}).status_code == 409)
g2 = c.post(f"/api/chat/topics/{t3}/invoice/products", headers=A).json()
check("בפתיחה הבאה: מסומן כשודר + 'כבר ב-GreenDay' לכל וריאציה", g2["sent"].get("0", {}).get("monday_id") == mid
      and all(v["exists"] and v["exists"]["monday_id"] == mid for v in g2["groups"][0]["variants"]))
# ביטחון נמוך → נשאר בהמתנה, אודי מתויג ומקבל התראה
PN.write_note = lambda group, sup, ex, ref: {"note": "?", "confidence": "low", "doubts": "אין מוצר דומה"}
db.si_set(db.si_for_topic(t3)["id"], result={**db.si_for_topic(t3)["result"], "sent": {}})
r = c.post(f"/api/chat/topics/{t3}/invoice/products/send", headers=A, json={"index": 0, "warranty": "שנה אחריות גרין מובייל"})
mid2 = r.json().get("monday_id"); time.sleep(0.8)
udi = [x["id"] for x in db.users_list() if x["name"] == "udi"]
check("ביטחון נמוך → נשאר 'טיוטה – ממתין להערות' + אודי מתויג ומקבל התראה",
      db.gd_product_get(mid2)["status"] == greenday.STATUS_HOLD and wpush and wpush[-1]["user_ids"] == udi
      and wpush[-1]["url"] == f"/?gd={mid2}", wpush)
# קיים חלקית באתר → מוצרים קיימים, רק הווריאציות החסרות, wc_id של המוצר
main._inv_enrich = lambda gs: [g.update(site="partial", wc_parent=51841) or [v.update(price=1.0, wc=({"id": 9, "parent": 51841} if v["sku"] == "520900" else None)) for v in g["variants"]] for g in gs] and gs
PN.write_note = lambda group, sup, ex, ref: {"note": "להוסיף וריאציה", "confidence": "high", "doubts": ""}
db.si_set(db.si_for_topic(t3)["id"], result={**db.si_for_topic(t3)["result"], "sent": {}})
r = c.post(f"/api/chat/topics/{t3}/invoice/products/send", headers=A, json={"index": 0, "warranty": "שנה אחריות גרין מובייל"})
gp3 = db.gd_product_get(r.json()["monday_id"]); time.sleep(0.5)
check("חלקית באתר → 'מוצרים קיימים' עם WC של המוצר ורק הווריאציה החסרה", gp3["grp"] == "group_mm3y9b6z" and gp3["wc_id"] == "51841"
      and gp3["pos_sku"] == "520903" and "הוספת וריאציות" in gp3["name"], gp3)
main._inv_enrich = lambda gs: [g.update(site="all") for g in gs] and gs
db.si_set(db.si_for_topic(t3)["id"], result={**db.si_for_topic(t3)["result"], "sent": {}})
check("הכל קיים באתר → לא משודר", c.post(f"/api/chat/topics/{t3}/invoice/products/send", headers=A, json={"index": 0, "warranty": "שנה אחריות גרין מובייל"}).status_code == 409)
# מחיקה מהלוח → נעלם, והחשבונית מאפשרת שידור מחדש
first = db.si_for_topic(t3)["result"]["sent"]
check("⛔ עובד לא מוחק מהלוח", c.delete(f"/api/greenday/products/{mid}", headers=Y).status_code == 403)
check("מחיקת מוצר מהלוח", c.delete(f"/api/greenday/products/{mid}", headers=A).status_code == 200
      and all(p["monday_id"] != mid for p in c.get("/api/greenday/board", headers=A).json()["products"]))
r = c.post("/api/greenday/bulk", headers=A, json={"ids": [mid2], "action": "delete", "value": ""}).json()
check("מחיקה קבוצתית", r.get("done") == 1 and db.gd_product_get(mid2) is None)
# ── מוצר מהקופה לפי מק״ט (בלי חשבונית) ──
class _FakePOS:
    P = {"520944": "Xiaomi Redmi 10 4RAM + 128GB - Gray", "520945": "Xiaomi Redmi 10 4RAM + 128GB - White",
         "520943": "Xiaomi Redmi 10 6RAM + 128GB - Gray", "520950": "Xiaomi Redmi 10 2022 4RAM + 128GB - Gray",
         "520951": "Xiaomi Redmi 10C 64GB - Blue"}
    def get_product(self, sku):
        sku = str(sku)
        return {"id": sku, "name": self.P[sku], "price": 399.0, "currentStock": 3, "isActive": True,
                "supplier": {"name": "המילטון"}} if sku in self.P else {}
    def get_products(self, search=None, **kw):
        return [self.get_product(k) for k, v in self.P.items() if (search or "").lower() in v.lower()]
main.poller.client = lambda: _FakePOS()
check("⛔ עובד לא משתמש במוצר מהקופה", c.post("/api/greenday/pos/lookup", headers=Y, json={"skus": ["520944"]}).status_code == 403)
L = c.post("/api/greenday/pos/lookup", headers=A, json={"skus": ["520944", "999"]}).json()
skus = sorted(x["sku"] for x in L["candidates"])
check("מק״ט אחד → כל הווריאציות של אותו דגם (בלי 2022 / 10C)", skus == ["520943", "520944", "520945"]
      and [x["sku"] for x in L["candidates"] if x["entered"]] == ["520944"]
      and any(e["sku"] == "999" and not e["found"] for e in L["entered"]), L)
SI.group_products = lambda lines: [{"name": "Xiaomi Redmi 10", "brand": "Xiaomi", "variants": [
    {"ref": "S" + l["matched"][0]["id"], "color": "", "ram": "", "storage": "128GB", "sku": l["matched"][0]["id"],
     "pos_name": l["description"], "qty": l["qty"], "desc": l["description"]} for l in lines]}]
main._inv_enrich = lambda gs: [g.update(site="new", wc_parent=None) or [v.update(price=399.0, wc=None) for v in g["variants"]] for g in gs] and gs
PN.write_note = lambda group, sup, ex, ref: {"note": "יש להזין את המוצר תחת הקטגוריות", "confidence": "high", "doubts": ""}
G = c.post("/api/greenday/pos/group", headers=A, json={"skus": ["520944", "520945"]}).json()
check("קיבוץ לפי מק״טים שנבחרו + טוקן לשידור", G["token"] and len(G["groups"][0]["variants"]) == 2 and G["warranty_options"], G)
check("⛔ שידור בלי אחריות", c.post("/api/greenday/pos/send", headers=A, json={"token": G["token"], "index": 0}).status_code == 400)
r = c.post("/api/greenday/pos/send", headers=A, json={"token": G["token"], "index": 0, "warranty": "שנה אחריות גרין מובייל"}).json()
time.sleep(0.8)
gp4 = db.gd_product_get(r["monday_id"])
check("נוצר בלוח עם המק״טים ופאבל העביר ל'ממתין'", gp4 and gp4["pos_sku"] == "520944, 520945" and gp4["status"] == "ממתין"
      and any("שנה אחריות גרין מובייל" in (x["body"] or "") for x in gp4["updates"]), gp4)
check("⛔ שידור כפול", c.post("/api/greenday/pos/send", headers=A, json={"token": G["token"], "index": 0, "warranty": "x"}).status_code == 409)
import sys as _s; _s.path.insert(0, "../shared")
import greenday_dispatch as _gdd
check("הסוכנים לא נוגעים בסטטוס ההמתנה", _gdd.plan([{"name": "Xiaomi Redmi 10", "status": greenday.STATUS_HOLD, "grp": "topics", "image_link": "", "list_id": "", "created": "2026-01-01T00:00:00Z", "updates": []}]) == {"yuval": [], "gali": [], "qa": []})

# ── 🌐 מלאי באתר אחרי כניסה מאומתת (22/10) ──
db.si_set(db.si_for_topic(t3)["id"], result={**db.si_for_topic(t3)["result"],
    "lines": [{"description": "a", "qty": 10, "got": 10, "status": "ok", "matched": [{"id": "518895", "name": "Quest 3", "qty": 10}]},
              {"description": "b", "qty": 2, "got": 2, "status": "ok", "matched": [{"id": "500001", "name": "Case", "qty": 2}]},
              {"description": "c", "qty": 3, "got": 3, "status": "ok", "matched": [{"id": "500002", "name": "Phone", "qty": 3}]}],
    "extras": [{"id": "500003", "name": "New thing", "qty": 1}]})
WC = {"518895": {"id": 11, "parent_id": 0, "name": "Meta Quest 3", "status": "publish", "stock_status": "instock", "manage_stock": True, "stock_quantity": 1},
      "500001": {"id": 22, "parent_id": 7, "name": "Case - Black", "status": "publish", "stock_status": "outofstock", "manage_stock": False, "stock_quantity": None},
      "500002": {"id": 33, "parent_id": 0, "name": "Phone", "status": "publish", "stock_status": "instock", "manage_stock": False, "stock_quantity": None}}
main._wc_get = lambda path, params=None: [WC[params["sku"]]] if params and params.get("sku") in WC else []
puts = []
main._wc_put = lambda path, body: puts.append((path, body)) or {}
class _StockPOS:
    def get_product_stock(self, sku): return {1: 4.0, 2: 8.0, 5: 3.0}   # 5='אתר' משוריין — לא נספר
main.poller.client = lambda: _StockPOS()
check("⛔ עובד לא בודק מלאי באתר", c.post(f"/api/chat/topics/{t3}/invoice/site-stock", headers=Y).status_code == 403)
rows = {r["sku"]: r for r in c.post(f"/api/chat/topics/{t3}/invoice/site-stock", headers=A).json()["rows"]}
check("כמות באתר (1) קטנה ממה שנכנס (10) → מציע לעדכן לפי הקופה בלי 'אתר' (12)", rows["518895"]["need"] and rows["518895"]["suggest"] == 12 and rows["518895"]["pos_stock"] == 12, rows["518895"])
check("אזל באתר (לא מנוהל) → מציע 'במלאי'", rows["500001"]["need"] and rows["500001"]["suggest"] is None)
check("במלאי באתר בלי ניהול כמות → אין מה לעדכן", not rows["500002"]["need"])
check("לא קיים באתר → מסומן", rows["500003"]["wc"] is None and not rows["500003"]["need"])
r = c.post(f"/api/chat/topics/{t3}/invoice/site-stock/apply", headers=A, json={"items": [{"sku": "518895", "qty": 12}, {"sku": "500001"}, {"sku": "999"}]}).json()
check("עדכון: כמות למנוהל, 'במלאי' לווריאציה, מק״ט שלא באתר נכשל לבד",
      puts == [("/products/11", {"stock_quantity": 12, "stock_status": "instock"}), ("/products/7/variations/22", {"stock_status": "instock"})]
      and len(r["done"]) == 2 and r["errors"] and not r["ok"], (puts, r))
check("נרשם ביומן הנושא", any("עדכן/ה מלאי באתר" in m["text"] for m in c.get(f"/api/chat/messages?topic={t3}", headers=A).json()["messages"]))

os.unlink(_tmp)
print("\n" + ("✅ כל בדיקות חשבונית הספק עברו" if not fails else f"❌ נכשלו {len(fails)}: {fails}"))
sys.exit(1 if fails else 0)
