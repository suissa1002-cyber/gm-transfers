#!/usr/bin/env python3
"""מחבר מק"טי קופה לוריאציות WooCommerce לפי צבע+נפח.

⚠️ למה זה קיים (אסי, 19/08/2026): לקוח ביקש להירשם לרשימת המתנה ל-Pixel 11
Pro XL וזה נכשל. הסיבה: כל 38 הווריאציות של סדרת Pixel 11 באתר **בלי מק"ט**,
והקופה לא מכירה את הסדרה בכלל. בלי מק"ט אין מה לבדוק, ולכן אי אפשר לרשום
ללא הבסיס הזה — ה-API של NewOrder הוא קריאה בלבד.

הכלי הזה לא ממציא מק"טים. הוא מחפש בקופה לפי שם, מתאים לוריאציה לפי
צבע+נפח, וכותב את המק"ט ל-WooCommerce. ⛔ ברירת המחדל היא הרצה יבשה.

    python3 link_skus.py "Pixel 11"          # מציג מה היה נכתב
    python3 link_skus.py "Pixel 11" --apply  # כותב בפועל
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "shared"))

# צבע בעברית → הטוקן שמופיע בשם בקופה (באנגלית)
COLOR_HE = {
    "שחור אובסידיאן": "obsidian", "אובסידיאן": "obsidian",
    "ירוק זית": "olive", "זית": "olive",
    "ירוק ערפילי": "fog", "ערפילי": "fog",
    "קורל קניון": "canyon", "קניון": "canyon",
    "ירוק פיסטוק": "pistachio", "לבנדר פרוסט": "frost", "ורוד היביסקוס": "hibiscus",
    "פורצלן": "porcelain", "לבן": "white", "שחור": "black", "אפור": "gray",
}


def _env(path=".env"):
    for p in (path, os.path.join("..", "..", ".env")):
        if os.path.exists(p):
            for line in open(p, encoding="utf-8"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip().strip('"\''))
            return


def _wc(path, method="GET", body=None):
    u = os.environ["WP_USERNAME"]
    p = os.environ["WP_APP_PASSWORD"]
    import base64
    auth = base64.b64encode(f"{u}:{p}".encode()).decode()
    req = urllib.request.Request(
        "https://greenmobile.co.il/wp-json/wc/v3/" + path,
        data=json.dumps(body).encode() if body else None, method=method,
        headers={"Authorization": "Basic " + auth, "Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode() or "{}")


def _storage(txt):
    m = re.search(r"(\d+)\s*(TB|GB)", str(txt), re.I)
    if not m:
        return ""
    n, unit = int(m.group(1)), m.group(2).upper()
    return f"{n*1024}GB" if unit == "TB" else f"{n}GB"


def main():
    _env()
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    query = sys.argv[1]
    apply = "--apply" in sys.argv

    from neworder_client import NewOrderClient
    nc = NewOrderClient(os.environ["NEWORDER_API_TOKEN"])
    pos = [p for p in (nc.get_products(search=query, page_size=200) or [])
           if query.lower().replace(" ", "") in str(p.get("name", "")).lower().replace(" ", "")]
    print(f"בקופה נמצאו {len(pos)} מוצרים ל-'{query}'")
    if not pos:
        print("⛔ הקופה לא מכירה את הסדרה — אין מה לחבר. צריך ליצור אותה בקופה קודם.")
        return 2
    for p in pos[:40]:
        print(f"   מק\"ט {p.get('id')}  {str(p.get('name'))[:60]}")

    prods = _wc("products?search=" + urllib.parse.quote(query) + "&per_page=30&status=any"
                "&_fields=id,name,type")
    prods = [x for x in prods if query.lower() in str(x.get("name", "")).lower()
             or query.replace("Pixel", "פיקסל") in str(x.get("name", ""))]
    linked = skipped = 0
    for prod in prods:
        vs = _wc(f"products/{prod['id']}/variations?per_page=60&_fields=id,sku,attributes")
        for v in vs:
            if (v.get("sku") or "").strip():
                continue
            opts = [a.get("option", "") for a in (v.get("attributes") or [])]
            color_he = next((o for o in opts if any(c in o for c in COLOR_HE)), "")
            color_en = next((en for he, en in COLOR_HE.items() if he in color_he), "")
            size = _storage(" ".join(opts))
            hit = None
            for p in pos:
                nm = str(p.get("name", "")).lower()
                if color_en and color_en not in nm:
                    continue
                if size and _storage(nm) != size:
                    continue
                hit = p
                break
            if not hit:
                skipped += 1
                print(f"   ⚠️ לא נמצאה התאמה: {' / '.join(opts)}")
                continue
            sku = str(hit.get("id"))
            print(f"   {'כותב' if apply else 'היה נכתב'}: {' / '.join(opts):32} → מק\"ט {sku}")
            if apply:
                _wc(f"products/{prod['id']}/variations/{v['id']}", "PUT", {"sku": sku})
            linked += 1
    print(f"\n══ {'חוברו' if apply else 'יחוברו'}: {linked} · ללא התאמה: {skipped} ══")
    if not apply:
        print("(הרצה יבשה — להוספת --apply כדי לכתוב בפועל)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
