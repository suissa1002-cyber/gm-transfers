"""וואטסאפ: חיפוש מלל בהודעות (כל השיחות / שיחה אחת) + טעינת היסטוריה מנקודת זמן (קפיצה לתוצאה ישנה)."""
import sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ.update(TRANSFERS_DB_PATH=_tmp, DATABASE_URL="", ADMIN_PASSWORD="adm")
from fastapi.testclient import TestClient
import main, db
db.init_db(); main.cfg.ADMIN_PASSWORD = "adm"
main._wa_read_native = lambda: True
c = TestClient(main.app); H = {"X-Admin-Key": "adm"}
P, Q = "972501111111", "972502222222"
for i in range(100):   # 100 הודעות; המחיר המובטח הוא בהודעה 5 — מחוץ ל-60 האחרונות
    db.wa_msg_upsert(f"w{i}", P, "out" if i % 2 else "in", "text", text=("המחיר בשבילך 1,899 ש\"ח סופי" if i == 5 else f"הודעה {i}"), ts=1000 + i * 60)
db.wa_msg_upsert("q1", Q, "in", "text", text="כמה עולה? אמרו לי 1,899", ts=9000)
r = c.get("/api/admin/wa/msg-search?q=1,899", headers=H).json()["results"]
assert [x["id"] for x in r] == ["q1", "w5"] and "1,899" in r[1]["snippet"], r
r = c.get(f"/api/admin/wa/msg-search?q=1,899&phone={P}", headers=H).json()["results"]
assert [x["id"] for x in r] == ["w5"] and r[0]["direction"] == "out", r
assert c.get("/api/admin/wa/msg-search?q=א", headers=H).json()["results"] == []
assert c.get("/api/admin/wa/msg-search?q=1,899").status_code == 401
# נרמול (אסי 26/10: חיפש "5800 ש"ח"): פסיק במספר, גרשיים, מילת מטבע, סדר מילים
db.wa_msg_upsert("z1", Q, "out", "text", text="המחיר המיוחד: 5,800 ש״ח כולל משלוח", ts=9100)
r = c.get('/api/admin/wa/msg-search?q=5800 ש"ח', headers=H).json()["results"]
assert [x["id"] for x in r] == ["z1"] and "5,800" in r[0]["snippet"], r
assert [x["id"] for x in c.get("/api/admin/wa/msg-search?q=משלוח 5800", headers=H).json()["results"]] == ["z1"]
db.wa_contact_upsert(P, "דנה") if hasattr(db, "wa_contact_upsert") else None
assert db.wa_search('5800 ש"ח') == [] or all("5800" not in (x.get("phone") or "") for x in db.wa_search('5800 ש"ח'))
t = c.get(f"/api/admin/wa/thread/{P}", headers=H).json()
assert len(t["messages"]) == 60 and "w5" not in [m["id"] for m in t["messages"]]
t = c.get(f"/api/admin/wa/thread/{P}?since={1000 + 5 * 60 - 60}", headers=H).json()
assert "w5" in [m["id"] for m in t["messages"]] and t["messages"][-1]["id"] == "w99", len(t["messages"])
os.unlink(_tmp)
print("✅ חיפוש מלל בוואטסאפ + טעינת היסטוריה לתוצאה ישנה")
