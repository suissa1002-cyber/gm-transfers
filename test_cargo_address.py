"""Cargo: הזמנה בלי רחוב נעצרת לפני Cargo עם הסבר, ולא עם "ודא שתוסף הגשר מותקן".

⚠️ 29/09/2026 (52261): הלקוח מילא רק "קיבוץ נאות מרדכי" בשדה העיר, רחוב ריק.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("WC_STORE_URL", "https://example.invalid")
os.environ.setdefault("WC_CONSUMER_KEY", "k"); os.environ.setdefault("WC_CONSUMER_SECRET", "s")
from fastapi.testclient import TestClient
import requests, main

KEY = os.getenv("ADMIN_PASSWORD") or ""
posted = []

class R:
    def __init__(self, j, code=200): self._j, self.status_code, self.ok = j, code, code < 400
    def json(self): return self._j

def run(shipping, billing, pickup=False):
    posted.clear()
    requests.get = lambda url, **kw: (R({"shipping": shipping, "billing": billing})
                                      if "/wc/v3/orders/" in url else R({"ok": False}))
    requests.post = lambda url, **kw: (posted.append(url), R({"ok": True, "shipments": {}}))[1]
    main._wp_app_auth = lambda: ("u", "p")
    main._advance_to_shipping = lambda oid: "shipping-stage"
    c = TestClient(main.app, raise_server_exceptions=False)
    return c.post("/api/admin/orders/52261/cargo", json={"pickup": pickup},
                  headers={"X-Admin-Key": KEY})

fails = []
def check(label, cond):
    print(f"{'✅' if cond else '❌'} {label}"); cond or fails.append(label)

r = run({"address_1": "", "city": "קיבוץ נאות מרדכי"}, {"address_1": ""})
check("רחוב ריק → 400", r.status_code == 400)
check("ההודעה אומרת מה חסר", "חסרה כתובת רחוב" in r.text)
check("Cargo לא נקרא בכלל", not posted)

r = run({"address_1": "קיבוץ נאות מרדכי"}, {"address_1": ""})
check("רחוב מלא → עובר ל-Cargo", r.status_code == 200 and posted)

r = run({"address_1": ""}, {"address_1": "הרצל 5"})
check("רחוב רק בחיוב → עובר (נפילה לחיוב)", r.status_code == 200 and posted)

r = run({"address_1": ""}, {"address_1": ""}, pickup=True)
check("נקודת איסוף בלי רחוב → עובר", r.status_code == 200 and posted)

# ── 04/10/2026 (52350): תשובה שאינה JSON → אזהרה לבדוק ב-Cargo לפני ניסיון חוזר ──
class HTMLResp:
    status_code, ok = 502, False
    headers = {"content-type": "text/html"}
    text = "<html><body><h1>502 Bad Gateway</h1>LiteSpeed</body></html>"
    def json(self): raise ValueError("not json")
saved = {}
main.db.sales_state_set = lambda k, v: saved.__setitem__(k, v)
requests.get = lambda url, **kw: R({"shipping": {"address_1": "הארבעה 30"}, "billing": {}})
requests.post = lambda url, **kw: HTMLResp()
c = TestClient(main.app, raise_server_exceptions=False)
r = c.post("/api/admin/orders/52350/cargo", json={"pickup": False}, headers={"X-Admin-Key": KEY})
check("502 שאינו JSON → 502 עם הסבר", r.status_code == 502)
check("מזהיר לבדוק בפורטל Cargo לפני ניסיון חוזר", "בדוק בפורטל Cargo" in r.text)
check("גוף התשובה מוצג בלי תגיות HTML", "502 Bad Gateway" in r.text and "<h1>" not in r.text)
check("הגוף המלא נשמר לאבחון", "cargo_fail:52350" in saved)

assert not fails, fails
print("\n✅ הכל עבר")
