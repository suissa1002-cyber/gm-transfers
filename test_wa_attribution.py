"""ייחוס וואטסאפ: כל הודעה יוצאת נשמרת עם "מי ענה" — בוט / צוות / שם משתמש / מתוזמן."""
import sys, os, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["TRANSFERS_DB_PATH"] = _tmp
os.environ["DATABASE_URL"] = ""
os.environ["ADMIN_PASSWORD"] = "legacy-secret"
from fastapi.testclient import TestClient
import main, db, wa
db.init_db()
main.cfg.ADMIN_PASSWORD = "legacy-secret"

# בלי רשת: Meta "שולח" ומחזיר wamid ייחודי; החלון פתוח
_n = [0]
def _fake_send(phone, text):
    _n[0] += 1
    return f"wamid.test{_n[0]}"
wa._meta_send_text = _fake_send
wa.meta_direct_ready = lambda: True
wa._window_for = lambda phone: {"in_window": True}
wa._auto_human = lambda phone: None
main._bot_handoff_on = lambda phone: None

c = TestClient(main.app, raise_server_exceptions=True)
fails = []
def check(label, cond, extra=""):
    print(f"{'✅' if cond else '❌'} {label}" + (f"  [{extra}]" if not cond else "")); cond or fails.append(label)
P = "972501234567"
def last():
    m = db.wa_msg_thread(P, 1)[-1]
    return m["sent_by"], m["sent_kind"]

wa.send_text(P, "שלום מהבוט")
check("תשובת בוט (webhook, בלי מבצע) → בוט", last() == ("בוט", "bot"), last())

r = c.post("/api/admin/wa/send", headers={"X-Admin-Key": "legacy-secret"}, json={"phone": P, "text": "היי, כאן נציג"})
check("סיסמה משותפת → צוות", r.status_code == 200 and last() == ("צוות", "console"), (r.status_code, last()))

c.post("/api/admin/users", headers={"X-Admin-Key": "legacy-secret"},
       json={"username": "udi", "name": "אודי", "password": "owner456", "role": "owner"})
tok = c.post("/api/auth/login", json={"username": "udi", "password": "owner456"}).json()["token"]
r = c.post("/api/admin/wa/send", headers={"X-User-Token": tok}, json={"phone": P, "text": "אודי עונה"})
check("משתמש מחובר → השם שלו", r.status_code == 200 and last() == ("אודי", "user"), (r.status_code, last()))

check("אחרי הבקשה המבצע מתאפס (לא דולף לבוט)", (wa.send_text(P, "בוט שוב") and last()) == ("בוט", "bot"), last())

r = c.post("/api/admin/wa/schedule", headers={"X-User-Token": tok}, json={"phone": P, "text": "תזכורת", "at": "23:59"})
sid = r.json().get("id")
with db._conn() as cn:
    cn.cursor().execute(db._q("UPDATE scheduled_sends SET send_at=? WHERE id=?"), (int(time.time()) - 5, sid))
main._scheduled_sends_job()
check("מתוזמן → 'מתוזמן · אודי'", last() == ("מתוזמן · אודי", "scheduled"), last())

msgs = wa.get_thread_native(P)["messages"]
check("השיחה מחזירה by/by_kind לממשק", msgs[-1].get("by") == "מתוזמן · אודי" and msgs[0].get("by_kind") == "bot",
      [(m.get("by"), m.get("by_kind")) for m in msgs])

os.unlink(_tmp)
assert not fails, fails
print(f"\n✅ כל הבדיקות עברו ({len(msgs)} הודעות)")
