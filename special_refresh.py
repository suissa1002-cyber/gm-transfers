"""רענון חי של עמודי הרשימה המלאים — רבי-המכר / מבצעים / חדש אצלנו.
רץ על Render כל כמה שעות, לצד home_refresh (אותה ארכיטקטורה: המעטפת המעוצבת
נבנית ע"י צנרת העיצוב במק, וכאן מוחלפת רק רשת המוצרים בין סמני GMLIST).

זה סוגר את הפער שאסי דיווח עליו שלוש פעמים: סקשני דף הבית התעדכנו מ-Render
24/7, אבל העמודים המלאים חיכו לרנר יומי במק (שגם דורש שהמק דולק).

לכל עמוד: מושכים מוצרים חי מ-WC, בונים כרטיסים בפורמט pcard של עמודי הקטלוג
(data-cats / data-price / data-fN לפי מפת הפא�טות שהעמוד עצמו מפרסם בסמן
GMLIST:meta), מחליפים את הגריד, מעדכנים את המונה ומרחיבים את גבולות המחיר
(PMIN/PMAX + סליידר) אם מוצר חדש חורג מהם — אחרת פילטר ברירת-המחדל היה מסתיר אותו.

⚠️ תבנית הכרטיס חייבת להתאים ל-generate_all_categories.py::pcard (מקור-אמת
העיצוב; עמודים מיוחדים = בלי באדג'י תגיות, רק באדג' הנחה). שינוי שם → לשקף כאן.
"""
import os
import re
import html
import json
import datetime
import requests

CODE_IDS = {20820, 42270}
PAGES = [
    # (slug, page_id, fetcher-key, cap)
    ("best-sellers", 48701, "best", 40),
    ("deals", 48702, "deals", 60),
    ("new-arrivals", 48706, "new", 40),
]


def _base():
    return os.getenv("WC_STORE_URL", "https://greenmobile.co.il").rstrip("/")


def _wc_auth():
    return (os.getenv("WC_CONSUMER_KEY", ""), os.getenv("WC_CONSUMER_SECRET", ""))


def _wp_auth():
    return (os.getenv("WP_USERNAME", ""), os.getenv("WP_APP_PASSWORD", ""))


def _get(path, **params):
    r = requests.get(f"{_base()}/wp-json/{path}", params=params, auth=_wc_auth(),
                     headers={"User-Agent": "gm-special-refresh"}, timeout=90)
    r.raise_for_status()
    return r.json()


def _in_catalog(p):
    return (p.get("catalog_visibility") or "visible") not in ("hidden", "search")


# ─────────────────────────── שליפות (מקביל ל-build_special) ───────────────────────────
def fetch_best(n=40, days=60):
    after = (datetime.date.today() - datetime.timedelta(days=days)).isoformat() + "T00:00:00"
    try:
        rows = _get("wc-analytics/reports/products", after=after,
                    orderby="items_sold", order="desc", per_page=60)
    except Exception:
        return []
    ids = [r["product_id"] for r in rows if r.get("product_id") not in CODE_IDS][:n]
    if not ids:
        return []
    prods = _get("wc/v3/products", include=",".join(map(str, ids)), per_page=len(ids), status="publish")
    by = {p["id"]: p for p in prods}
    return [by[i] for i in ids if i in by and by[i].get("stock_status") == "instock"
            and by[i].get("images") and _in_catalog(by[i])]


def fetch_deals(n=60):
    out, seen, page = [], set(), 1
    while page <= 5 and len(out) < n:
        rows = _get("wc/v3/products", per_page=40, page=page, status="publish",
                    stock_status="instock", on_sale="true", orderby="popularity")
        if not rows:
            break
        for p in rows:
            if p["id"] in CODE_IDS or p["id"] in seen or not p.get("images") or not _in_catalog(p):
                continue
            seen.add(p["id"]); out.append(p)
        page += 1
    return out[:n]


def fetch_new(n=40):
    out, page = [], 1
    while page <= 3 and len(out) < n:
        rows = _get("wc/v3/products", per_page=40, page=page, status="publish",
                    stock_status="instock", orderby="date", order="desc")
        if not rows:
            break
        for p in rows:
            if p["id"] in CODE_IDS or not p.get("images") or not _in_catalog(p):
                continue
            out.append(p)
        page += 1
    return out[:n]


FETCHERS = {"best": fetch_best, "deals": fetch_deals, "new": fetch_new}


# ─────────────────────────── רינדור כרטיס (pcard-תואם) ───────────────────────────
def _photon(url, size=500):
    if not url:
        return ""
    if ".wp.com/" in url:
        return url
    u = url.replace("https://", "").replace("http://", "")
    return f"https://i0.wp.com/{u}?resize={size},{size}"


def _fmt(v):
    try:
        return f"{float(v):,.0f}"
    except (TypeError, ValueError):
        return v or ""


def _pcard(p, facets):
    base = _base()
    nm = html.escape(p["name"])
    imgs = p.get("images") or []
    img = _photon(imgs[0]["src"] if imgs else "")
    price = _fmt(p.get("price"))
    reg = _fmt(p.get("regular_price")) if p.get("regular_price") else ""
    onsale = p.get("on_sale") and reg and reg != price
    bh = ""
    if onsale:
        try:
            pct = round((1 - float(p["price"]) / float(p["regular_price"])) * 100)
            if pct > 0:
                bh = f'<span class="badge deal">{pct}%- מבצע</span>'
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    if any(t.get("id") == 3709 for t in (p.get("tags") or [])):
        bh += '<span class="badge outlet">מציאון</span>'
    badge = f'<div class="badges">{bh}</div>' if bh else ""
    regline = f'<s class="reg">‏₪{reg}</s>' if onsale else ""
    link = p.get("permalink") or f'{base}/?p={p["id"]}'
    if link.startswith(base):
        link = link[len(base):]
    cats = " ".join(str(c["id"]) for c in (p.get("categories") or []))
    # מיפוי תכונות המוצר לפא�טות של העמוד (לפי label, מסמן GMLIST:meta)
    attrs = {a.get("name", ""): [o.strip() for o in (a.get("options") or []) if o]
             for a in (p.get("attributes") or [])}
    fattrs = ""
    for f in facets:
        vals = attrs.get(f["label"], [])
        fattrs += f' data-f{f["idx"]}="{html.escape("|".join(vals))}"'
    try:
        pr = int(float(p.get("price") or 0))
    except (TypeError, ValueError):
        pr = 0
    frm = '<span class="from">החל מ-</span>' if p.get("type") == "variable" else ''
    if p.get("type") == "simple":
        btn = f'<a class="card-btn" href="{base}/?add-to-cart={p["id"]}">הוספה לסל</a>'
    else:
        btn = f'<a class="card-btn opts" href="{link}">בחר אפשרויות</a>'
    return (f'<article class="card" data-cats="{cats}" data-price="{pr}"{fattrs}>'
            f'<a class="card-link" href="{link}">'
            f'<div class="card-media">{badge}<img src="{img}" alt="{nm}" loading="lazy" width="300" height="300"></div>'
            f'<h3 class="card-name">{nm}</h3></a>'
            f'<div class="card-price"><span class="price">{frm}‏₪{price}</span>{regline}</div>{btn}</article>')


# ─────────────────────────── splice ───────────────────────────
def _splice_page(content, prods):
    """מחליף את הגריד+המונה ומרחיב את גבולות המחיר. מחזיר (content, ok)."""
    meta_m = re.search(r"<!--GMLIST:meta (\{.*?\})-->", content, re.S)
    grid_pat = re.compile(r"(<!--GMLIST:grid-->).*?(<!--/GMLIST:grid-->)", re.S)
    if not meta_m or not grid_pat.search(content):
        return content, False
    facets = json.loads(meta_m.group(1)).get("facets", [])
    grid = "\n".join(_pcard(p, facets) for p in prods)
    content = grid_pat.sub(lambda m: m.group(1) + "\n" + grid + "\n" + m.group(2), content)
    content = re.sub(r"(<!--GMLIST:cnt-->).*?(<!--/GMLIST:cnt-->)",
                     lambda m: m.group(1) + str(len(prods)) + m.group(2), content, flags=re.S)
    # הרחבת טווח המחיר אם מוצר חדש חורג (אחרת פילטר ברירת-המחדל מסתיר אותו)
    prices = []
    for p in prods:
        try:
            prices.append(int(float(p.get("price") or 0)))
        except (TypeError, ValueError):
            pass
    if prices:
        lo, hi = min(prices), max(prices)
        m = re.search(r"const PMIN=(\d+), PMAX=(\d+)", content)
        if m:
            cur_lo, cur_hi = int(m.group(1)), int(m.group(2))
            new_lo, new_hi = min(cur_lo, lo), max(cur_hi, hi)
            if (new_lo, new_hi) != (cur_lo, cur_hi):
                content = content.replace(f"const PMIN={cur_lo}, PMAX={cur_hi}",
                                          f"const PMIN={new_lo}, PMAX={new_hi}")
                # סליידר + תוויות
                content = content.replace(f'id="pmin" min="{cur_lo}" max="{cur_hi}" value="{cur_lo}"',
                                          f'id="pmin" min="{new_lo}" max="{new_hi}" value="{new_lo}"')
                content = content.replace(f'id="pmax" min="{cur_lo}" max="{cur_hi}" value="{cur_hi}"',
                                          f'id="pmax" min="{new_lo}" max="{new_hi}" value="{new_hi}"')
                content = content.replace(f'<span id="pminL">{cur_lo:,}</span>', f'<span id="pminL">{new_lo:,}</span>')
                content = content.replace(f'<span id="pmaxL">{cur_hi:,}</span>', f'<span id="pmaxL">{new_hi:,}</span>')
    return content, True


def refresh_special():
    """נקודת-הכניסה: מרענן את שלושת עמודי הרשימה. מחזיר סיכום לכל עמוד."""
    wp = _wp_auth()
    if not (wp[0] and wp[1]):
        return {"ok": False, "error": "WP creds missing"}
    out = {"ok": True}
    for slug, page_id, key, cap in PAGES:
        try:
            prods = FETCHERS[key](cap)
            if len(prods) < 4:                     # שליפה חלקית — לא דורסים
                out[slug] = {"ok": False, "error": "insufficient data", "count": len(prods)}
                out["ok"] = False
                continue
            r = requests.get(f"{_base()}/wp-json/wp/v2/pages/{page_id}",
                             params={"context": "edit"}, auth=wp,
                             headers={"User-Agent": "gm-special-refresh"}, timeout=90)
            r.raise_for_status()
            content = r.json()["content"]["raw"]
            content, ok = _splice_page(content, prods)
            if not ok:
                out[slug] = {"ok": False, "error": "GMLIST markers missing"}
                out["ok"] = False
                continue
            pr = requests.post(f"{_base()}/wp-json/wp/v2/pages/{page_id}",
                               json={"content": content}, auth=wp,
                               headers={"User-Agent": "gm-special-refresh"}, timeout=90)
            pr.raise_for_status()
            # ניקוי cache ממוקד לעמוד הזה בלבד
            try:
                requests.post(f"{_base()}/wp-json/gm-catalog/v1/purge",
                              json={"url": f"{_base()}/{slug}/"}, auth=wp, timeout=60)
            except Exception:
                pass
            out[slug] = {"ok": True, "count": len(prods)}
        except Exception as e:
            out[slug] = {"ok": False, "error": str(e)[:200]}
            out["ok"] = False
    return out


if __name__ == "__main__":
    print(json.dumps(refresh_special(), ensure_ascii=False))
