#!/usr/bin/env python3
"""בדיקת רגרסיה — פתרון בומרנג בהעברות (reconcile_boomerang_transfers).

תרחיש: מכשיר יצא בהעברה X→Y (לא נקלט) ונשלח בחזרה Y→X (לא נקלט) — תקוע בשני הכיוונים.
מאמת:
  • זוג בומרנג (ענפים הפוכים, שתי השורות received=0) → שתי השורות received=4, שתי
    ההעברות 'received'.
  • העברה ללא רגל-חזרה → לא נגעת.
  • זוג הפוך אך המכשיר **נקלט** ביעד (received=1) → לא נגעת (גארד false-positive).
  • העברה עם פריט נוסף שנקלט (Samsung) → מתקדמת ל'received' כשהבומרנג נפתר.
  • idempotent — ריצה שנייה לא מוצאת כלום.

רץ בלי DB אמיתי (SQLite זמני). הרצה: python3 tests/test_boomerang.py  (קוד יציאה 0=עבר)
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["TRANSFERS_DB_PATH"] = _tmp.name
os.environ.pop("DATABASE_URL", None)

import db  # noqa: E402
from db import _conn, _q, now_iso  # noqa: E402

db.init_db()


def mk_transfer(op, frm, to, created, status="in_transit"):
    with _conn() as c:
        c.cursor().execute(_q(
            """INSERT INTO transfers (op_id,from_branch_id,to_branch_id,op_type,employee,
               created_at,first_seen,total_units,received_units,status)
               VALUES (?,?,?,?,?,?,?,?,0,?)"""),
            (op, frm, to, 5, "test", created, now_iso(), 1, status))


def mk_item(op, pid, name, serial, received=0):
    with _conn() as c:
        c.cursor().execute(_q(
            """INSERT INTO transfer_items (op_id,product_id,name,serial,barcode,line_idx,received)
               VALUES (?,?,?,?,?,?,?)"""), (op, pid, name, serial, None, 0, received))


def status(op):
    with _conn() as c:
        r = c.cursor().execute(_q("SELECT status FROM transfers WHERE op_id=?"), (op,)).fetchone()
        return r["status"]


def items(op):
    with _conn() as c:
        rows = c.cursor().execute(
            _q("SELECT serial,received FROM transfer_items WHERE op_id=? ORDER BY serial"), (op,)).fetchall()
        return [(x["serial"], x["received"]) for x in rows]


# ── 1. בומרנג קלאסי: 3→4 ואז 4→3, שתי השורות לא-נקלטו, + פריט שכן נקלט בהעברה הראשונה
mk_transfer("OUT", 3, 4, "2026-06-18T19:50:00")
mk_item("OUT", "520196", "Poco X8 Pro", "SER-BOOM", received=0)
mk_item("OUT", "520183", "Samsung A57", "SER-RECVD", received=1)   # נקלט ביעד → 'partial'
mk_transfer("BACK", 4, 3, "2026-06-18T20:04:00")
mk_item("BACK", "520196", "Poco X8 Pro", "SER-BOOM", received=0)

# ── 2. ביקורת: העברה ללא רגל-חזרה → לא נגעת
mk_transfer("SOLO", 1, 2, "2026-06-18T10:00:00")
mk_item("SOLO", "999", "Other", "SER-SOLO", received=0)

# ── 3. ביקורת: ענפים הפוכים אך המכשיר נקלט ביעד (received=1) → לא בומרנג
mk_transfer("R-OUT", 1, 2, "2026-06-18T09:00:00")
mk_item("R-OUT", "888", "Recv", "SER-OK", received=1)
mk_transfer("R-BACK", 2, 1, "2026-06-18T09:30:00")
mk_item("R-BACK", "888", "Recv", "SER-OK", received=0)

res = db.reconcile_boomerang_transfers()
assert len(res) == 1, f"expected exactly 1 boomerang, got {len(res)}: {res}"
assert res[0]["serial"] == "SER-BOOM"
assert res[0]["origin_branch_id"] == 3 and res[0]["via_branch_id"] == 4

# הבומרנג נפתר בשני הכיוונים
assert items("OUT") == [("SER-BOOM", 4), ("SER-RECVD", 1)], items("OUT")
assert items("BACK") == [("SER-BOOM", 4)], items("BACK")
# OUT: Samsung נקלט(1) + Poco בומרנג(4) → כל הפריטים פתורים → received
assert status("OUT") == "received", status("OUT")
assert status("BACK") == "received", status("BACK")

# ביקורות — לא נגעו
assert status("SOLO") == "in_transit" and items("SOLO") == [("SER-SOLO", 0)]
assert status("R-BACK") == "in_transit", "device received at dest must NOT be treated as boomerang"
assert items("R-BACK") == [("SER-OK", 0)]

# idempotent
assert db.reconcile_boomerang_transfers() == []

# הסריאל כבר לא מופיע כ-transit (received=4 לא נספר)
dyn = db.serial_dynamic_status(["SER-BOOM"])
assert "SER-BOOM" not in dyn, f"resolved serial must not show transit: {dyn}"

# ── קליטה מאוחרת: כרטיס עם בומרנג=4 + פריט שנקלט מאוחר → צריך להתקדם ל'received' ──
# (op 13987: ה-iPad בומרנג סומן 4 כשפריטים אחרים עוד לא נקלטו; הם נקלטו אחר כך,
#  והכרטיס נתקע partial. promote_fully_resolved_transfers סוגר אותו.)
mk_transfer("LATE", 3, 2, "2026-06-18T19:59:00", status="partial")
mk_item("LATE", "518807", "iPad", "SER-LATE", received=4)   # בומרנג שכבר נפתר
mk_item("LATE", "p2", "Other", "SER-OTH", received=0)        # עדיין לא נקלט
assert db.promote_fully_resolved_transfers() == 0, "must NOT promote while an item is unreceived"
assert status("LATE") == "partial"
# עכשיו הפריט השני נקלט (קליטה מאוחרת)
with _conn() as c:
    c.cursor().execute(_q("UPDATE transfer_items SET received=1 WHERE serial='SER-OTH'"))
assert db.promote_fully_resolved_transfers() == 1, "all items resolved (1+4) → promote"
assert status("LATE") == "received", status("LATE")

print("✅ boomerang reconcile regression test PASSED")
try:
    os.remove(_tmp.name)
except OSError:
    pass
