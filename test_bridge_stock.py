"""מלאי חי לאלה: סיטי בשמו (לא "מחסן\\מרלוג"), בלי כמויות — רק 'יחידה אחרונה' כשנשארה אחת."""
import sys, os, tempfile, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ.update(TRANSFERS_DB_PATH=_tmp, DATABASE_URL="")
from fastapi.testclient import TestClient
import main, db
db.init_db(); main.URI_BRIDGE_KEY = "bk"
c = TestClient(main.app)
H = {"X-Bridge-Key": "bk"}


class _NO:
    def __init__(self, st): self.st = st
    def get_product_stock(self, sku): return self.st


def run(st):
    main.poller.client = lambda: _NO(st)
    return c.get("/api/uri-bridge/stock/519547", headers=H).json()


r = run({1: 0.0, 2: 3.0, 3: 2.0, 4: 1.0, 5: 0.0})
names = r["branches"]
assert r["in_stock"] and names == ["סטאר", "סיטי", "עד הלום"], r
assert "מרלוג" not in json.dumps(r, ensure_ascii=False) and "qty" not in json.dumps(r) and "total" not in r, r
assert not r["last_unit"], r
r = run({2: 1.0, 3: 1.0})
assert not r["last_unit"] and r["branches"] == ["סטאר", "סיטי"], r   # 1+1 ≠ אחרונה
r = run({3: 1.0})
assert r["in_stock"] and r["last_unit"] and r["branches"] == ["סיטי"], r
main._pos_product_unknown = lambda sku: False
r = run({})
assert not r["in_stock"] and r["branches"] == [] and r["unknown_product"] is False, r
assert c.get("/api/uri-bridge/stock/1").status_code == 401
os.unlink(_tmp)
print("✅ מלאי לאלה: סיטי בשמו, בלי כמויות, יחידה אחרונה מסומנת")
