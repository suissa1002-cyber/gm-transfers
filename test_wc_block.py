"""חסימה מדומה: 503 מוסבר, בדיקת UA אוטומטית, ויסות, ויומן אפיזודות."""
import sys, os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import os, sys, json
os.environ.setdefault("WC_STORE_URL","https://example.invalid")
os.environ.setdefault("WC_CONSUMER_KEY","k"); os.environ.setdefault("WC_CONSUMER_SECRET","s")
from fastapi.testclient import TestClient
import requests, main
KEY=os.getenv("ADMIN_PASSWORD") or ""

class Blocked:
    status_code=200; ok=True
    headers={"content-type":"text/html","server":"LiteSpeed"}
    text="<!DOCTYPE html><title>One moment, please...</title>"
    def json(self): raise requests.exceptions.JSONDecodeError("x",self.text,0)

calls=[]
def fake_get(*a, **kw):
    calls.append((kw.get("headers") or {}).get("User-Agent"))
    return Blocked()
requests.get=fake_get

store={}
main.db.sales_state_set=lambda k,v: store.__setitem__(k,v)
main.db.sales_state_get=lambda k,default=None: store.get(k,default)

c=TestClient(main.app, raise_server_exceptions=False)
r=c.get("/api/admin/orders/51529", headers={"X-Admin-Key":KEY})
assert r.status_code==503, r.status_code
print("1) קוד תשובה           :", r.status_code, "✅")

snap=json.loads(store["wc_block_last"])["snap"]
print("2) בדיקת UA אוטומטית   :", json.dumps(snap.get("ua_test"), ensure_ascii=False))
assert len(snap.get("ua_test") or {})==4, "ציפיתי ל-4 זיהויים"

n1=len(calls)
c.get("/api/admin/orders/51530", headers={"X-Admin-Key":KEY})
c.get("/api/admin/orders/51531", headers={"X-Admin-Key":KEY})
extra=len(calls)-n1
# כל בקשה עושה קריאה אחת משלה לאתר. הצילום מוסיף 5 (בדיקה + 4 זיהויים).
# אם הוויסות עובד, שתי הבקשות הבאות מוסיפות 2 בלבד — לא 12.
print(f"3) ויסות               : ראשונה={n1} קריאות (1 שלה + 5 צילום), "
      f"שתי הבאות={extra} (1 כל אחת, בלי צילום)")
assert n1==6, f"ציפיתי 6 בראשונה, קיבלתי {n1}"
assert extra==2, f"הוויסות לא עבד — {extra} קריאות במקום 2"

hist=json.loads(store["wc_block_log"])
print("4) יומן אפיזודות       :", len(hist), "רשומה ✅")
assert len(hist)==1
print("\n✅ הכל עבר")
