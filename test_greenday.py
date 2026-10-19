"""GreenDay · לוח מוצרים: בעלים בלבד, כתיבה ל-Monday בפורמט הנכון, יצירה+עדכון+העברת קבוצה (Monday מדומה)."""
import sys, os, tempfile, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ.update(TRANSFERS_DB_PATH=_tmp, DATABASE_URL="", ADMIN_PASSWORD="legacy-secret", MONDAY_API_TOKEN="x")
from fastapi.testclient import TestClient
import main, db, greenday
db.init_db(); main.cfg.ADMIN_PASSWORD = "legacy-secret"
calls = []
ITEM = {"id": "111", "name": "Galaxy Watch 9", "updated_at": "2026-10-15T10:00:00Z", "created_at": "2026-10-15T09:00:00Z",
        "group": {"id": "topics"}, "subitems": [],
        "column_values": [{"id": "status", "text": "ממתין", "value": None}, {"id": "numeric_mks5nhn6", "text": "1249", "value": None},
                          {"id": "dropdown_mks5ymd", "text": "128GB, 256GB", "value": None}]}
def fake_gql(q, v=None, timeout=90):
    calls.append((q.split("(")[0].split("{")[0].strip() + " " + q[q.find("{")+1:q.find("(", q.find("{")+1)].strip(), v))
    if "create_item" in q: return {"create_item": {"id": "222"}}
    if "create_update" in q: return {"create_update": {"id": "u1", "created_at": "x"}}
    if "items(ids" in q and "updates" in q: return {"items": [{"id": i, "updates": [{"id": "u1", "text_body": "asi · GreenDay\nהוראות\n\n\n\nגיימינג", "created_at": "2026-10-15T10:00:00Z", "creator": {"name": "Asi"}, "assets": [], "replies": []}]} for i in v["ids"]]}
    if "items(ids" in q: return {"items": [dict(ITEM, id=v["ids"][0])]}
    if "groups(ids" in q: return {"boards": [{"groups": [{"items_page": {"cursor": None, "items": [ITEM]}}]}]}
    return {"x": {"id": "1"}}
greenday.gql = fake_gql
c = TestClient(main.app)
fails = []
def check(label, cond, extra=""):
    print(f"{'✅' if cond else '❌'} {label}" + (f"  [{extra}]" if not cond else "")); cond or fails.append(label)
ADM = {"X-Admin-Key": "legacy-secret"}
db.device_register("dev-b-0000000000000001", "קופה", "ua", "1.2.3.4", "1", "approved")
for un, role, pr in [("asi", "owner", []), ("udi", "owner", []), ("dana", "user", ["orders", "transfers"])]:
    c.post("/api/admin/users", headers=ADM, json={"username": un, "name": un, "password": "pass123", "role": role, "perms": pr})
def login(un):
    t = c.post("/api/auth/login", json={"username": un, "password": "pass123"}, headers={"X-Device-Token": "dev-b-0000000000000001"}).json()["token"]
    return {"X-User-Token": t, "X-Device-Token": "dev-b-0000000000000001"}
A, D = login("asi"), login("dana")

print(greenday.sync())
check("סנכרון קבוצות פעילות → מוצר ב-DB", db.gd_product_get("111")["price"] == 1249 and db.gd_product_get("111")["storages"] == ["128GB", "256GB"])
check("⛔ עובדת (גם עם הרשאות) לא רואה את הלוח", c.get("/api/greenday/board", headers=D).status_code == 403)
check("⛔ סיסמה משותפת לא נכנסת ללוח", c.get("/api/greenday/board", headers=ADM).status_code == 403)
b = c.get("/api/greenday/board", headers=A).json()
check("בעלים רואה לוח עם כל הקבוצות והסטטוסים", len(b["groups"]) == len(greenday.GROUPS) and len(b["statuses"]) == len(greenday.STATUSES) and len(b["products"]) == 1)

calls.clear()
r = c.post("/api/greenday/products/111", headers=A, json={"field": "status", "value": "בעבודה"})
mut = [x for x in calls if "change_column_value" in x[0] or "change_column_value" in json.dumps(x)]
check("סטטוס → change_column_value עם {label}", r.status_code == 200 and any(json.loads(x[1]["v"]) == {"label": "בעבודה"} for x in calls if x[1] and "v" in x[1]), calls[:2])
calls.clear()
c.post("/api/greenday/products/111", headers=A, json={"field": "storages", "value": ["256GB", "bogus"]})
check("נפחים → {labels} מסונן לערכים חוקיים", any(x[1] and "v" in x[1] and json.loads(x[1]["v"]) == {"labels": ["256GB"]} for x in calls), calls[:2])
calls.clear()
c.post("/api/greenday/products/111", headers=A, json={"field": "price", "value": "1299"})
check("מחיר → change_simple_column_value '1299'", any(x[1] and x[1].get("v") == "1299" for x in calls), calls[:2])
check("⛔ סטטוס לא מוכר נדחה", c.post("/api/greenday/products/111", headers=A, json={"field": "status", "value": "בלגן"}).status_code == 400)
calls.clear()
c.post("/api/greenday/products/111", headers=A, json={"field": "grp", "value": "group_mkzqzksn"})
check("העברה לארכיון → move_item_to_group", any(x[1] and x[1].get("g") == "group_mkzqzksn" for x in calls))
check("⛔ קבוצה לא מוכרת נדחית", c.post("/api/greenday/products/111", headers=A, json={"field": "grp", "value": "zzz"}).status_code == 400)
calls.clear()
r = c.post("/api/greenday/products", headers=A, json={"name": "Pixel 12", "grp": "topics", "values": {"price": "3999", "storages": ["256GB"]}, "update": "קטגוריות: סמארטפונים"})
cv = next((json.loads(x[1]["v"]) for x in calls if x[1] and "n" in x[1]), {})
check("יצירת מוצר: create_item בקבוצת חדשים, סטטוס ממתין כברירת מחדל", r.json().get("monday_id") == "222" and cv.get("status") == {"label": "ממתין"} and cv.get("numeric_mks5nhn6") == "3999", cv)
body = next((x[1]["b"] for x in calls if x[1] and "b" in x[1] and "i" in x[1]), "")
check("עדכון ראשון נכתב בשם הכותב (asi · GreenDay)", "asi · GreenDay" in body and "קטגוריות" in body, body[:80])
calls.clear()
r = c.post("/api/greenday/products/111/update", headers=A, json={"text": "<script>x</script> שורה\nשנייה"})
body = next((x[1]["b"] for x in calls if x[1] and "b" in x[1]), "")
check("עדכון: HTML מנוטרל + שורות", "&lt;script&gt;" in body and "<br>" in body, body)
# ── פעולות קבוצתיות ──
calls.clear()
r = c.post("/api/greenday/bulk", headers=A, json={"ids": ["111", "112", "113"], "action": "grp", "value": "group_mkzqzksn"}).json()
moves = [x for x in calls if x[1] and x[1].get("g") == "group_mkzqzksn"]
check("העברה קבוצתית לארכיון → move לכל אחד", r["done"] == 3 and len(moves) == 3, r)
calls.clear()
r = c.post("/api/greenday/bulk", headers=A, json={"ids": ["111", "112"], "action": "status", "value": "הושלם"}).json()
check("שינוי סטטוס קבוצתי", r["done"] == 2 and sum(1 for x in calls if x[1] and "v" in x[1] and json.loads(x[1]["v"]) == {"label": "הושלם"}) == 2, r)
check("⛔ קבוצתי: סטטוס לא מוכר נדחה", c.post("/api/greenday/bulk", headers=A, json={"ids": ["111"], "action": "status", "value": "x"}).status_code == 400)
check("⛔ קבוצתי: עובדת נחסמת", c.post("/api/greenday/bulk", headers=D, json={"ids": ["111"], "action": "grp", "value": "topics"}).status_code == 403)

# ── כותב אמיתי + עריכת עדכון ──
pr = c.get("/api/greenday/products/111", headers=A).json()["product"]
u0 = pr["updates"][0]
check("עדכון מ-GreenDay מוצג בשם הכותב (לא בעל הטוקן) ובלי שורת החתימה", u0["author"] == "asi" and u0["gd"] and u0["body"] == "הוראות\n\nגיימינג", u0)
U = login("udi")
check("⛔ אודי לא יכול לערוך עדכון של אסי", c.post("/api/greenday/products/111/update/u1", headers=U, json={"text": "x"}).status_code == 403)
calls.clear()
r = c.post("/api/greenday/products/111/update/u1", headers=A, json={"text": "תיקון: גיימינג › עכברים"})
eb = next((x[1]["b"] for x in calls if x[1] and "b" in x[1] and "i" in x[1]), "")
check("אסי עורך את העדכון שלו → edit_update עם החתימה", r.status_code == 200 and "asi · GreenDay" in eb and "עכברים" in eb, eb)

# ── מצב מקור (primary): סוכנים מול GreenDay, בלי Monday ──
from urllib.parse import quote
GALI = {"X-Admin-Key": "legacy-secret", "X-Agent": quote("גלי")}
check("⛔ מפתח ניהול בלי X-Agent לא נכנס ללוח", c.get("/api/greenday/agent/items", headers=ADM).status_code == 403)
check("⛔ החלפת מצב ע\"י עובדת", c.post("/api/greenday/mode", headers=D, json={"mode": "primary"}).status_code == 403)
check("בעלים מעביר למצב מקור", c.post("/api/greenday/mode", headers=A, json={"mode": "primary"}).json()["mode"] == "primary")
calls.clear()
check("סנכרון מ-Monday מושבת במצב מקור", greenday.sync().get("skipped") is True)
r = c.post("/api/greenday/products", headers=A, json={"name": "Galaxy Buds 4", "grp": "topics", "values": {"price": "699"}, "update": "קטגוריות: אוזניות"})
nid = r.json()["monday_id"]
check("מוצר חדש נוצר מקומית (לא ב-Monday)", nid.startswith("gd") and not calls, (nid, calls[:1]))
it = c.get("/api/greenday/agent/items?groups=topics", headers=GALI).json()["items"]
mine = next(x for x in it if x["monday_id"] == nid)
check("גלי רואה את המוצר עם המחיר וההוראות של אסי", mine["price"] == 699 and mine["updates"][0]["author"] == "asi" and "אוזניות" in mine["updates"][0]["body"], mine)
c.post(f"/api/greenday/products/{nid}", headers=GALI, json={"field": "status", "value": "בעבודה"})
c.post(f"/api/greenday/products/{nid}", headers=GALI, json={"field": "wc_id", "value": "53001"})
c.post(f"/api/greenday/products/{nid}/update", headers=GALI, json={"text": "הועלה כטיוטה"})
pr = c.get(f"/api/greenday/products/{nid}", headers=A).json()
check("שינויי גלי נשמרו מקומית + עדכון בשמה", pr["product"]["status"] == "בעבודה" and pr["product"]["wc_id"] == "53001"
      and pr["product"]["updates"][0]["author"] == "גלי" and not calls and pr["monday_url"] == "", (pr["product"]["updates"][:1], calls[:1]))
upd = next(u for u in pr["product"]["updates"] if u["author"] == "asi")
check("אסי עורך עדכון מקומי שלו", c.post(f"/api/greenday/products/{nid}/update/{upd['monday_id']}", headers=A, json={"text": "קטגוריות: אוזניות › TWS"}).status_code == 200)
check("⛔ סטטוס לא מוכר נדחה גם במצב מקור", c.post(f"/api/greenday/products/{nid}", headers=GALI, json={"field": "status", "value": "בלגן"}).status_code == 400)
c.post("/api/greenday/bulk", headers=A, json={"ids": [nid], "action": "grp", "value": "group_mkzqzksn"})
check("העברה לארכיון מקומית", db.gd_product_get(nid)["grp"] == "group_mkzqzksn" and not calls)
check("חזרה למצב מראה (מתג חזרה)", c.post("/api/greenday/mode", headers=A, json={"mode": "mirror"}).json()["mode"] == "mirror")

# ── הרצת סוכנים ידנית + דופק מהמק ──
MAC = {"X-Admin-Key": "legacy-secret", "X-Agent": quote("שולח משימות")}
st = c.get("/api/greenday/agents", headers=A).json()
check("לפני דופק — המק לא ידוע", st["mac_seen_ago"] is None)
r = c.post("/api/greenday/run", headers=A).json()
check("לחיצה על 'הרץ סוכנים' שומרת בקשה בשם אסי", r["request"]["by"] == "asi" and r["request"]["ts"] > r["ack_ts"])
check("⛔ עובדת לא יכולה להריץ סוכנים", c.post("/api/greenday/run", headers=D).status_code == 403)
hb = c.post("/api/greenday/agents/heartbeat", headers=MAC, json={"running": {"yuval": True}, "queue": {"yuval": 5}, "manual_until": 0, "handled_request_ts": 0}).json()
check("הדופק מחזיר למק את הבקשה הפתוחה", hb["request"]["ts"] == r["request"]["ts"])
c.post("/api/greenday/agents/heartbeat", headers=MAC, json={"running": {"yuval": True}, "queue": {"yuval": 5}, "manual_until": 9999999999, "handled_request_ts": hb["request"]["ts"]})
st = c.get("/api/greenday/agents", headers=A).json()
check("אחרי שהמק קלט: ack = הבקשה, יובל רץ, 5 בתור", st["ack_ts"] == r["request"]["ts"] and st["heartbeat"]["running"]["yuval"] and st["heartbeat"]["queue"]["yuval"] == 5 and st["mac_seen_ago"] < 5, st)

os.unlink(_tmp)
assert not fails, fails
print("\n✅ כל בדיקות GreenDay עברו")
