"""הזמנה ↔ העברה: אחרי שהסניף מבצע את ההעברה, ההזמנה יודעת "הועבר מ-X — ממתין לקליטה".

⚠️ 05/10/2026 (אסי, הזמנה 52490): תג "בקשת העברה שודרה" נעלם ברגע ההעברה בקופה,
כי שורת הבקשה נמחקת — ואיתה הקשר להזמנה.
"""
import sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["TRANSFERS_DB_PATH"] = _tmp
os.environ["DATABASE_URL"] = ""
import db
db.init_db()

fails = []
def check(label, cond):
    print(f"{'✅' if cond else '❌'} {label}"); cond or fails.append(label)

GAN, STAR, SITE = 1, 2, 5
PIX = {"product_id": "520212", "name": "Google Pixel 10a 128GB שחור"}

# בקשה רגילה (כמותית) להזמנה 52490 מגן העיר לאתר
db.plan_add([{**PIX, "from_branch": GAN, "to_branch": SITE, "qty": 1}], created_by="הזמנת אתר #52490")
check("לפני ההעברה — אין קישור", db.plan_fulfilled_for_order("52490") == [])

op = {"id": 17001, "branchId": GAN, "receivingBranchId": SITE, "operationType": 5,
      "employee": "שלמה", "createDate": "2026-10-05T13:00:00",
      "stockItems": [{"id": "520212", "name": PIX["name"], "quantity": 1, "serials": []}]}
db.upsert_transfer(op)
n = db.plan_match_transfer(GAN, SITE, [{"product_id": "520212", "serials": [], "qty": 1}], op_id="17001")
f = db.plan_fulfilled_for_order("52490")
check("הבקשה נוקתה", n == 1 and not [p for p in db.plan_list() if p.get("product_id") == "520212"])
check("ההזמנה מקושרת להעברה 17001", len(f) == 1 and f[0]["op_id"] == "17001")
check("מקור = גן העיר, מצב = ממתין לקליטה", f[0]["from_branch"] == GAN and f[0]["status"] == "in_transit")

t = db.get_transfer("17001")
db.receive_item_manual(t["items"][0]["id"], "17001", "אסי")
check("אחרי קליטה — המצב 'received'", db.plan_fulfilled_for_order("52490")[0]["status"] == "received")

# בקשה סריאלית
db.plan_add([{**PIX, "from_branch": STAR, "to_branch": SITE, "qty": 1, "serial": "SN-777"}],
            created_by="הזמנת אתר #52500")
db.plan_match_transfer(STAR, SITE, [{"product_id": "520212", "serials": ["SN-777"], "qty": 1}], op_id="17002")
f = db.plan_fulfilled_for_order("52500")
check("סריאלי — מקושר עם מקור סטאר", len(f) == 1 and f[0]["op_id"] == "17002" and f[0]["from_branch"] == STAR)

# בקשה שלא מהאתר (ידנית) — לא יוצרת קישור להזמנה
db.plan_add([{**PIX, "from_branch": STAR, "to_branch": GAN, "qty": 1}], created_by="אסי")
db.plan_match_transfer(STAR, GAN, [{"product_id": "520212", "serials": [], "qty": 1}], op_id="17003")
check("בקשה ידנית — בלי רשומת הזמנה", all(r["op_id"] != "17003" for o in ("52490", "52500")
                                          for r in db.plan_fulfilled_for_order(o)))

# 🐛 צריכה כפולה: שורת-כמות של 3 + שורה סריאלית, יחידה אחת עם סריאל אחר
db.plan_add([{**PIX, "from_branch": STAR, "to_branch": GAN, "qty": 3}], created_by="הזמנת אתר #52600")
db.plan_add([{**PIX, "from_branch": STAR, "to_branch": GAN, "qty": 1, "serial": "SN-OTHER"}],
            created_by="הזמנת אתר #52601")
db.plan_match_transfer(STAR, GAN, [{"product_id": "520212", "serials": ["SN-NOMATCH"], "qty": 1}], op_id="17004")
left = [p for p in db.plan_list() if p.get("from_branch") == STAR and p.get("to_branch") == GAN]
qty_row = [p for p in left if not p.get("serial")]
check("יחידה אחת סיפקה בקשה אחת בלבד (כמות 3→2, הסריאלית נשארה)",
      qty_row and int(qty_row[0]["qty"]) == 2 and any(p.get("serial") == "SN-OTHER" for p in left))
check("והקישור נרשם רק להזמנה 52600",
      len(db.plan_fulfilled_for_order("52600")) == 1 and db.plan_fulfilled_for_order("52601") == [])

os.unlink(_tmp)
assert not fails, fails
print("\n✅ הכל עבר")
