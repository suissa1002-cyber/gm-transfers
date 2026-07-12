"""רענון חי של דף הבית (עמוד 48664) — רץ על Render כל כמה שעות.

מחליף רק את שלושת קטעי-המוצרים (רבי-מכר/מבצעים/חדש) בין סמני-גבול
(<!--GMHOME:best-->…<!--/GMHOME:best-->) — המעטפת הסטטית (באנרים, הדר,
קודים דיגיטליים, פוטר) לא נגעת. הנתונים נמשכים חי מ-WooCommerce.

⚠️ תבנית הכרטיס כאן חייבת להתאים ל-generate_mockup.py::card() (מקור-אמת
העיצוב). שינוי עיצוב כרטיס שם → לשקף כאן.
"""
import os
import re
import html
import datetime
import requests

WP_PAGE_ID = 48664
_MARKERS = ("best", "sale", "new")


def _base():
    return os.getenv("WC_STORE_URL", "https://greenmobile.co.il").rstrip("/")


def _wc_auth():
    return (os.getenv("WC_CONSUMER_KEY", ""), os.getenv("WC_CONSUMER_SECRET", ""))


def _wp_auth():
    return (os.getenv("WP_USERNAME", ""), os.getenv("WP_APP_PASSWORD", ""))


CODE_IDS = {20820, 42270}   # קודים דיגיטליים — לא בכרטיסי המוצרים


# ─────────────────────────── שליפת נתונים ───────────────────────────
def _get(path, **params):
    r = requests.get(f"{_base()}/wp-json/{path}", params=params, auth=_wc_auth(),
                     headers={"User-Agent": "gm-home-refresh"}, timeout=90)
    r.raise_for_status()
    return r.json()


def _slim(p):
    imgs = p.get("images") or []
    return {"id": p["id"], "name": p.get("name", ""),
            "price": p.get("price") or p.get("regular_price") or "",
            "regular": p.get("regular_price") or "",
            "type": p.get("type", "simple"), "link": p.get("permalink"),
            "img": imgs[0]["src"] if imgs else ""}


def fetch_best(n=8, days=60):
    """רבי-מכר של 60 הימים האחרונים (items_sold בחלון) דרך wc-analytics."""
    after = (datetime.date.today() - datetime.timedelta(days=days)).isoformat() + "T00:00:00"
    out = []
    try:
        rows = _get("wc-analytics/reports/products", after=after,
                    orderby="items_sold", order="desc", per_page=30, extended_info="true")
    except Exception:
        rows = []
    for r in rows:
        pid = r.get("product_id"); info = r.get("extended_info") or {}
        if pid in CODE_IDS or info.get("stock_status") != "instock":
            continue
        m = re.search(r'src="([^"]+)"', info.get("image") or "")
        img = m.group(1).replace("&amp;", "&") if m else ""
        if not img:
            continue
        out.append({"id": pid, "name": info.get("name", ""),
                    "price": str(info.get("price") or ""), "regular": "",
                    "type": "variable" if (info.get("variations") or []) else "simple",
                    "link": info.get("permalink"), "img": img})
        if len(out) >= n:
            break
    if len(out) < 6:                        # נפילת דוח → נסיגה ל-popularity כללי
        out = (out + _fetch_ordered("popularity", n))[:n]
    return out


def _fetch_ordered(orderby, n=8):
    out, page = [], 1
    while len(out) < n and page <= 4:
        rows = _get("wc/v3/products", per_page=30, page=page, status="publish",
                    stock_status="instock", orderby=orderby)
        if not rows:
            break
        for p in rows:
            if p["id"] in CODE_IDS or not p.get("images"):
                continue
            out.append(_slim(p))
            if len(out) >= n:
                break
        page += 1
    return out


def fetch_sale(n=8):
    disc, plain, seen, page = [], [], set(), 1
    while page <= 6 and (len(disc) + len(plain)) < 40:
        rows = _get("wc/v3/products", per_page=40, page=page, status="publish",
                    stock_status="instock", on_sale="true", orderby="popularity")
        if not rows:
            break
        for p in rows:
            if p["id"] in CODE_IDS or not p.get("images") or p["id"] in seen:
                continue
            seen.add(p["id"]); s = _slim(p)
            reg, pr = p.get("regular_price"), p.get("price")
            try:
                (disc if (reg and pr and float(reg) > float(pr)) else plain).append(s)
            except (TypeError, ValueError):
                plain.append(s)
        page += 1
    return (disc + plain)[:n]


# ─────────────────────────── רינדור כרטיס ───────────────────────────
def _fmt(v):
    try:
        return f"{float(v):,.0f}"
    except (TypeError, ValueError):
        return v or ""


def _card(p, kind):
    name = html.escape(p["name"]); img = p.get("img", "")
    price = _fmt(p.get("price")); reg = _fmt(p.get("regular")) if p.get("regular") else ""
    badge = ""
    if kind == "sale" and reg and reg != price:
        try:
            pct = round((1 - float(p["price"]) / float(p["regular"])) * 100)
            if pct > 0:
                badge = f'<span class="badge deal">{pct}%- מבצע</span>'
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    elif kind == "best":
        badge = '<span class="badge best">רב־מכר</span>'
    elif kind == "new":
        badge = '<span class="badge new">חדש</span>'
    regline = f'<s class="reg">‏₪{reg}</s>' if (kind == "sale" and reg and reg != price) else ""
    link = p.get("link") or f'{_base()}/?p={p.get("id")}'
    is_var = p.get("type") != "simple"
    frm = '<span class="from">החל מ-</span>' if p.get("type") == "variable" else ''
    btn = (f'<a class="card-btn opts" href="{link}">בחר אפשרויות</a>' if is_var
           else f'<a class="card-btn" href="{_base()}/?add-to-cart={p.get("id")}">הוספה לסל</a>')
    return (f'<article class="card">'
            f'<a class="card-link" href="{link}">'
            f'<div class="card-media">{badge}<img src="{img}" alt="{name}" loading="lazy" width="260" height="260"></div>'
            f'<h3 class="card-name">{name}</h3></a>'
            f'<div class="card-price"><span class="price">{frm}‏₪{price}</span>{regline}</div>'
            f'{btn}</article>')


def _cards(products, kind):
    return "\n".join(_card(p, kind) for p in products)


# ─────────────────────────── החלפה + דחיפה ───────────────────────────
def _splice(content, key, inner):
    """מחליף את מה שבין <!--GMHOME:key--> ל-<!--/GMHOME:key-->."""
    pat = re.compile(r"(<!--GMHOME:%s-->).*?(<!--/GMHOME:%s-->)" % (key, key), re.S)
    if not pat.search(content):
        return content, False
    return pat.sub(lambda m: m.group(1) + "\n" + inner + "\n" + m.group(2), content), True


def refresh_home():
    """נקודת-הכניסה: מושך WC, מחליף 3 קטעים בעמוד 48664, דוחף, מנקה cache."""
    wp = _wp_auth()
    if not (wp[0] and wp[1]):
        return {"ok": False, "error": "WP creds missing"}

    best, sale, new = fetch_best(8, 60), fetch_sale(8), _fetch_ordered("date", 8)
    counts = {"best": len(best), "sale": len(sale), "new": len(new)}
    if counts["best"] < 4 or counts["new"] < 4:      # שליפה חלקית — לא דורסים
        return {"ok": False, "error": "insufficient data", "counts": counts}

    r = requests.get(f"{_base()}/wp-json/wp/v2/pages/{WP_PAGE_ID}",
                     params={"context": "edit"}, auth=wp, timeout=90)
    r.raise_for_status()
    content = r.json()["content"]["raw"]

    blocks = {"best": _cards(best, "best"), "sale": _cards(sale, "sale"), "new": _cards(new, "new")}
    replaced = {}
    for key in _MARKERS:
        content, ok = _splice(content, key, blocks[key])
        replaced[key] = ok
    if not all(replaced.values()):
        return {"ok": False, "error": "markers missing", "replaced": replaced, "counts": counts}

    pr = requests.post(f"{_base()}/wp-json/wp/v2/pages/{WP_PAGE_ID}",
                       json={"content": content}, auth=wp, timeout=90)
    pr.raise_for_status()
    # ניקוי cache של LiteSpeed כדי שהעדכון יופיע מיד
    try:
        requests.post(f"{_base()}/wp-json/gm-catalog/v1/live", json={"on": True},
                      auth=wp, timeout=60)
    except Exception:
        pass
    return {"ok": True, "counts": counts, "bytes": len(content)}


if __name__ == "__main__":
    import json as _j
    print(_j.dumps(refresh_home(), ensure_ascii=False))
