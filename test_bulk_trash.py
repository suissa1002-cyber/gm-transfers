"""העברה מרובה לפח: רק בוטל/נכשל/הוחזר, בלי force, ולעולם לא orders/batch.

⚠️ 06/10/2026: ל-WooCommerce יש orders/batch {"delete":[...]} — אבל הוא מריץ כל
מחיקה עם force=true, כלומר מחיקה סופית בלי פח. הבדיקה כאן מוודאת שלא נוגעים בו.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("WC_STORE_URL", "https://example.invalid")
os.environ.setdefault("WC_CONSUMER_KEY", "k"); os.environ.setdefault("WC_CONSUMER_SECRET", "s")
from fastapi.testclient import TestClient
import requests, main

KEY = os.getenv("ADMIN_PASSWORD") or ""
STATUS = {101: "cancelled", 102: "failed", 103: "refunded", 104: "processing",
          105: "completed", 106: "trash"}
deleted, urls = [], []

class R:
    def __init__(self, j, code=200): self._j, self.status_code, self.ok = j, code, code < 400
    def json(self): return self._j
def fget(url, **kw):
    urls.append(url); oid = int(url.rstrip("/").split("/")[-1])
    return R({"id": oid, "status": STATUS[oid]}) if oid in STATUS else R({}, 404)
def fdel(url, **kw):
    urls.append(url); deleted.append((int(url.rstrip("/").split("/")[-1]), kw.get("params"), "force" in url))
    return R({"id": 0})
def fpost(url, **kw):
    urls.append(url); return R({})
requests.get, requests.delete, requests.post = fget, fdel, fpost

fails = []
def check(label, cond):
    print(f"{'✅' if cond else '❌'} {label}"); cond or fails.append(label)

c = TestClient(main.app, raise_server_exceptions=False)
r = c.post("/api/admin/orders/bulk-trash", json={"ids": [101, 102, 103, 104, 105, 106, 999]},
           headers={"X-Admin-Key": KEY}).json()
check("בוטל/נכשל/הוחזר הועברו (3) + שכבר בפח נספר (1)", r["trashed"] == 4 and set(r["ids"]) == {101, 102, 103, 106})
check("בטיפול/הושלם דולגו עם סיבה בעברית", {x["id"] for x in r["skipped"]} == {104, 105}
      and all("לפח אפשר" in x["error"] for x in r["skipped"]))
check("הזמנה שלא קיימת → נכשל, לא דולג", [x["id"] for x in r["failed"]] == [999])
check("DELETE נשלח רק על 3 ההזמנות המתאימות", sorted(d[0] for d in deleted) == [101, 102, 103])
check("⛔ אף DELETE עם force", not any(d[1] or d[2] for d in deleted))
check("⛔ אף קריאה ל-orders/batch", not any("batch" in u for u in urls))

deleted.clear()
r1 = c.delete("/api/admin/orders/104", headers={"X-Admin-Key": KEY})
check("הבודד עדיין חוסם 'בטיפול' (400)", r1.status_code == 400 and not deleted)
r2 = c.delete("/api/admin/orders/101", headers={"X-Admin-Key": KEY})
check("הבודד עדיין מעביר 'בוטל' לפח", r2.status_code == 200 and [d[0] for d in deleted] == [101])

assert not fails, fails
print("\n✅ הכל עבר")
