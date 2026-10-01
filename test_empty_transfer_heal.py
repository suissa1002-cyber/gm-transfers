"""העברה שנקלטה ריקה מתמלאת כשהפריטים מופיעים בקופה, ואפשר לסגור העברה ריקה.

⚠️ 01/10/2026 (op 16372, סטאר→אתר): NewOrder כתבו כותרת לפני פריטים; הפולר
קלט 0 פריטים והעברה נתקעה 0/0, למרות Xiaomi Smart Band 9 Active בקופה.
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

HDR = {"id": 16372, "branchId": 2, "receivingBranchId": 5, "operationType": 5,
       "employee": "יעקב.ע", "createDate": "2026-10-01T14:16:08"}
ITEM = {"id": "520009", "name": "Xiaomi Smart Band 9 Active - White", "quantity": 1.0, "serials": []}

db.upsert_transfer({**HDR, "stockItems": []})
t = db.get_transfer("16372")
check("נקלטה ריקה (0 פריטים) — משחזר את הבאג", t["total_units"] == 0 and not t["items"])
check("מזוהה כריקה-טרייה", db.transfer_is_empty_recent("16372"))

n = db.fill_empty_transfer({**HDR, "stockItems": [ITEM]})
t = db.get_transfer("16372")
check("ריפוי הוסיף יחידה אחת", n == 1 and t["total_units"] == 1 and len(t["items"]) == 1)
check("הפריט הנכון", t["items"][0]["product_id"] == "520009")
check("כבר לא ריקה", not db.transfer_is_empty_recent("16372"))
check("ריפוי חוזר לא מכפיל", db.fill_empty_transfer({**HDR, "stockItems": [ITEM]}) == 0
      and len(db.get_transfer("16372")["items"]) == 1)

db.upsert_transfer({**HDR, "id": 16400, "stockItems": [ITEM]})
check("⛔ לא נוגעים בהעברה שיש בה פריטים",
      db.fill_empty_transfer({**HDR, "id": 16400, "stockItems": [ITEM, ITEM]}) == 0
      and db.get_transfer("16400")["total_units"] == 1)

db.upsert_transfer({**HDR, "id": 16401, "stockItems": []})
db.close_transfer("16401", "העברה ריקה — אין פריטים", "test")
check("העברה ריקה נסגרת ויוצאת מהלוח",
      db.get_transfer("16401")["status"] == "closed"
      and all(x["op_id"] != "16401" for x in db.list_in_transit(5)))
check("⛔ לא מרפאים העברה שנסגרה",
      db.fill_empty_transfer({**HDR, "id": 16401, "stockItems": [ITEM]}) == 0)

# מטמון הלקוח: [] לא נשמר, רשימה מלאה כן
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "shared"))
from neworder_client import NewOrderClient
cl = NewOrderClient("x")
answers = [[], [ITEM]]
cl._get = lambda path, params=None: answers.pop(0) if answers else [ITEM]
first, second = cl.get_stock_items(16372), cl.get_stock_items(16372)
check("[] לא נשמר במטמון — הקריאה השנייה מקבלת את הפריט", first == [] and second == [ITEM])
cl._get = lambda path, params=None: []
check("רשימה מלאה כן נשמרת במטמון", cl.get_stock_items(16372) == [ITEM])

os.unlink(_tmp)
assert not fails, fails
print("\n✅ הכל עבר")
