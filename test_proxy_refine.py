"""דגל proxy של ip-api חייב להיבדק מול proxycheck לפני שהוא מדליק hard_fraud.

⚠️ המוקים כאן מחקים את החוזה של _proxycheck (proxy בוליאני), לא את ה-JSON
הגולמי של proxycheck.io ("no"/"yes") — מחרוזת "no" היא truthy ותשבור את הבדיקה.

⚠️ 18/09/2026 (51871): ספק אזורי במזרח ירושלים (3samnet) סומן proxy=true
ב-ip-api בעוד proxycheck אמר Business/סיכון 0 — ולקוח תקין נצבע אדום.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("WC_STORE_URL", "https://example.invalid")
os.environ.setdefault("WC_CONSUMER_KEY", "k"); os.environ.setdefault("WC_CONSUMER_SECRET", "s")
import main

CASES = [
    ("3samnet מזרח ירושלים",
     {"cc": "IL", "isp": "Isam Awadallah trading as 3samnet", "proxy": True, "hosting": False},
     {"proxy": False, "type": "Business", "risk": 0, "operator": ""},
     False, "ספק אזורי תקין — לא דגל"),
    ("NordVPN",
     {"cc": "IL", "isp": "NordVPN", "proxy": True, "hosting": False},
     {"proxy": True, "type": "VPN", "risk": 73, "operator": "NordVPN"},
     True, "VPN אמיתי — חייב להידלק"),
    ("TOR",
     {"cc": "IL", "isp": "tor exit", "proxy": True, "hosting": False},
     {"proxy": True, "type": "TOR", "risk": 100, "operator": ""},
     True, "TOR — חייב להידלק"),
    ("proxycheck לא זמין (fail-open)",
     {"cc": "IL", "isp": "unknown", "proxy": True, "hosting": False},
     {},
     True, "בלי חיווי מחדד — נשארים מחמירים"),
    ("סיכון גבוה למרות proxy=no",
     {"cc": "IL", "isp": "shady", "proxy": True, "hosting": False},
     {"proxy": False, "type": "Business", "risk": 66, "operator": ""},
     True, "סיכון 66 > 20 — לא מנקים"),
]

fails = []
for name, geo, pxc, want_flag, why in CASES:
    main._ip_geo = lambda ip, _g=geo: _g
    main._proxycheck = lambda ip, _p=pxc: _p
    order = {"id": 1, "number": "1", "total": "355.00", "billing": {},
             "line_items": [{"name": "קוד דיגיטלי PlayStation Store", "quantity": 1,
                             "total": "355.00", "sku": "517803"}],
             "meta_data": [], "customer_ip_address": "185.97.127.95"}
    r = main._fraud_triage(order, {}, force=True)
    assert r is not None, "הטריאז לא זיהה מוצר דיגיטלי"
    got = bool(r.get("hard_fraud"))
    ok = got == want_flag
    print(f"{'✅' if ok else '❌'} {name:32} hard_fraud={got} (ציפינו {want_flag}) — {why}")
    if not ok: fails.append(name)
assert not fails, f"נכשלו: {fails}"
