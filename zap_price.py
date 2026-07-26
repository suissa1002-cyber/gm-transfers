"""
zap_price.py — עריכת מחיר האתר מתוך דוח זאפ, כולל סנכרון מוצרי הצל.

למה זה קיים (אסי, 26/07/2026): הדוח מראה "לרדת ל-X ⇒ לעלות למקום Y", אבל
כדי לפעול צריך היה לצאת לוורדפרס, לעדכן את הווריאציה, ואז לזכור לעדכן גם
את מוצר הצל של זאפ. כאן זה פעולה אחת.

⚠️ שני דברים שחייבים להיות מובנים:

1. **מוצרי צל.** זאפ קורא מוצרי-צל (`type=external`, `catalog_visibility=hidden`,
   מטא `gm_parent_product_id`) ולא את ההורה — ההורה מוסתר בכוונה
   (`_woocommerce_zap_disable=yes`). לכן שינוי מחיר על הווריאציה **לא מגיע
   לזאפ** עד שמסנכרנים את הצל. ההתאמה בין וריאציה לצל היא לפי סלאג הנפח
   ב-`external_url` (512GB → `00512gb`, 1TB → `01024gb`).

2. **הקופה לא מתעדכנת.** המחיר בקופה מנוהל בנפרד (וברוב המכשירים הוא ₪1
   בכוונה, כדי לאלץ את המוכר לבדוק באתר). לכן כל שינוי נרשם כ"ממתין לקופה"
   עד שמסמנים אותו כמעודכן — אחרת נוצר פער שקט בין הערוצים.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime

import requests

import db

logger = logging.getLogger("transfers.zapprice")

CAP_SLUG = re.compile(r"attribute_pa[-_a-z]*storage=([0-9a-z]+)", re.I)


def _wc():
    base = os.getenv("WC_STORE_URL", "https://greenmobile.co.il").rstrip("/")
    return base, (os.getenv("WC_CONSUMER_KEY", ""), os.getenv("WC_CONSUMER_SECRET", ""))


def _cap_slug(text: str) -> str | None:
    """סלאג הנפח מתוך external_url של מוצר צל / אטריביוט של וריאציה."""
    m = CAP_SLUG.search(text or "")
    return m.group(1).lower() if m else None


def _cap_from_text(text: str) -> str | None:
    """נפח מתוך שם חופשי — גיבוי כשאין אטריביוטים על הווריאציה."""
    m = re.search(r"(\d+)\s*(TB|GB)\b", text or "", re.I)
    if not m:
        return None
    gb = int(m.group(1)) * (1024 if m.group(2).upper() == "TB" else 1)
    return f"{gb:05d}gb"


def _cap_from_attrs(attrs: list) -> str | None:
    """הנפח של הווריאציה, מנורמל לסלאג של זאפ: 512GB → 00512gb, 1TB → 01024gb."""
    for a in attrs or []:
        opt = str(a.get("option") or "")
        m = re.search(r"(\d+)\s*(TB|GB)", opt, re.I)
        if m:
            gb = int(m.group(1)) * (1024 if m.group(2).upper() == "TB" else 1)
            return f"{gb:05d}gb"
    return None


_TERM_CACHE = {}


def _term_slugs(base, auth) -> dict:
    """{taxonomy: {שם-אופציה-lower: slug}} — כדי לתרגם "512GB" ל-"00512gb".
    ⚠️ ה-REST מחזיר בווריאציה את **שם** האופציה, ומוצר הצל נושא ב-URL את
    ה-**slug**. בלי המפה הזו ההשוואה נכשלת וסונכרן הצל הלא נכון."""
    if _TERM_CACHE:
        return _TERM_CACHE
    try:
        attrs = requests.get(f"{base}/wp-json/wc/v3/products/attributes", auth=auth,
                             timeout=45).json()
    except Exception:  # noqa: BLE001
        return {}
    for a in (attrs if isinstance(attrs, list) else []):
        tax = "pa_" + str(a.get("slug") or "")
        m = {}
        page = 1
        while page <= 5:
            try:
                t = requests.get(f"{base}/wp-json/wc/v3/products/attributes/{a['id']}/terms",
                                 auth=auth, timeout=45,
                                 params={"per_page": 100, "page": page,
                                         "_fields": "name,slug"}).json()
            except Exception:  # noqa: BLE001
                break
            if not isinstance(t, list) or not t:
                break
            for x in t:
                m[str(x.get("name", "")).strip().lower()] = str(x.get("slug", ""))
            page += 1
        if m:
            _TERM_CACHE[tax] = m
    return _TERM_CACHE


def _shadow_attrs(url: str) -> dict:
    """{taxonomy: slug} מתוך ה-external_url של מוצר הצל — כמו gm_zap_sync_shadow."""
    from urllib.parse import urlparse, parse_qs
    q = parse_qs(urlparse(url or "").query)
    out = {}
    for k, v in q.items():
        if k.startswith("attribute_") and v and v[0]:
            out[k[len("attribute_"):]] = v[0].lower()
    return out


def _variation_slugs(base, auth, var: dict) -> dict:
    """{taxonomy: slug} של וריאציה — תרגום שמות האופציות לסלאגים."""
    terms = _term_slugs(base, auth)
    out = {}
    for a in var.get("attributes") or []:
        nm = str(a.get("name") or "")
        opt = str(a.get("option") or "").strip().lower()
        tax = None
        for t in terms:
            if opt in terms[t]:
                tax = t
                break
        if tax:
            out[tax] = terms[tax][opt]
        elif nm:
            out["pa_" + nm.strip().replace(" ", "-")] = opt
    return out


def find_by_sku(sku: str) -> dict | None:
    """הווריאציה (או המוצר הפשוט) שנושאת את המק"ט, יחד עם ההורה והאטריביוטים."""
    base, auth = _wc()
    r = requests.get(f"{base}/wp-json/wc/v3/products", auth=auth, timeout=45,
                     params={"sku": sku, "per_page": 10,
                             "_fields": "id,name,type,price,regular_price,parent_id,attributes"})
    for p in (r.json() if r.ok else []):
        return {"kind": "variation" if p.get("parent_id") else "product",
                "id": p["id"], "parent": p.get("parent_id") or p["id"],
                "name": p.get("name"), "price": p.get("price"),
                "attrs": _variation_slugs(base, auth, p),
                "cap": _cap_from_attrs(p.get("attributes")) or _cap_from_text(p.get("name"))}
    page = 1
    while page <= 8:
        rr = requests.get(f"{base}/wp-json/wc/v3/products", auth=auth, timeout=60,
                          params={"per_page": 100, "page": page, "type": "variable",
                                  "status": "publish", "_fields": "id,name"})
        rows = rr.json() if rr.ok else []
        if not rows:
            break
        for par in rows:
            v = requests.get(f"{base}/wp-json/wc/v3/products/{par['id']}/variations", auth=auth,
                             timeout=45, params={"per_page": 100,
                                                 "_fields": "id,sku,price,regular_price,attributes"})
            for x in (v.json() if v.ok else []):
                if str(x.get("sku")) == str(sku):
                    return {"kind": "variation", "id": x["id"], "parent": par["id"],
                            "name": par.get("name"), "price": x.get("price"),
                            "attrs": _variation_slugs(base, auth, x),
                            "cap": _cap_from_attrs(x.get("attributes"))}
        page += 1
    return None


def _shadow_matches(tgt: dict, sh: dict) -> bool:
    """האם מוצר הצל מתאר בדיוק את הווריאציה. מיישר לחוקי התוסף
    (gm_zap_sync_shadow): כל אטריביוט שמופיע ב-URL של הצל חייב להתאים.
    ⛔ אם אין במה להשוות — מחזירים False. עדיף לא לסנכרן מאשר לדרוס דגם אחר."""
    a, b = tgt.get("attrs") or {}, sh.get("attrs") or {}
    if a and b:
        common = set(a) & set(b)
        if common:
            return all(a[k] == b[k] for k in common)
    return bool(tgt.get("cap")) and tgt["cap"] == sh.get("cap")


def set_price(sku: str, price: float, sync_shadow: bool = True) -> dict:
    """מעדכן את מחיר האתר למק"ט, ואופציונלית מסנכרן את מוצר הצל התואם."""
    base, auth = _wc()
    tgt = find_by_sku(sku)
    if not tgt:
        return {"ok": False, "error": f"לא נמצא מוצר באתר עם מק\"ט {sku}"}
    old = tgt.get("price")
    url = (f"{base}/wp-json/wc/v3/products/{tgt['parent']}/variations/{tgt['id']}"
           if tgt["kind"] == "variation" else
           f"{base}/wp-json/wc/v3/products/{tgt['id']}")
    r = requests.put(url, auth=auth, timeout=45, json={"regular_price": f"{price:.2f}"})
    if not r.ok:
        return {"ok": False, "error": f"עדכון נכשל ({r.status_code})", "detail": r.text[:200]}

    synced, skipped = [], []
    if sync_shadow:
        for sh in shadows_for(tgt["parent"]):
            # ⛔ בלי התאמת נפח ודאית לא נוגעים בצל. שקט-והמשך היה מיישר את הצל
            # של 1TB למחיר של 512GB (קרה בפועל 26/07/2026) — עדיף לא לסנכרן
            # ולדווח, מאשר לדרוס מחיר של דגם אחר.
            if not _shadow_matches(tgt, sh):
                skipped.append({"id": sh["id"], "name": sh["name"], "reason": "תצורה אחרת"})
                continue
            rr = requests.put(f"{base}/wp-json/wc/v3/products/{sh['id']}", auth=auth,
                              timeout=45, json={"regular_price": f"{price:.2f}"})
            (synced if rr.ok else skipped).append(
                {"id": sh["id"], "name": sh["name"],
                 **({} if rr.ok else {"reason": f"HTTP {rr.status_code}"})})

    # ⚠️ הקופה לא מתעדכנת מכאן — רושמים "ממתין לקופה" כדי שלא ייווצר פער שקט
    db.sales_state_set(f"zap_pending:{sku}", json.dumps(
        {"sku": sku, "name": tgt.get("name"), "old": old, "new": round(price, 2),
         "at": datetime.now().isoformat(timespec="seconds"),
         "shadows": len(synced)}, ensure_ascii=False))
    logger.info("zap price %s: %s → %s (shadows synced: %d)", sku, old, price, len(synced))
    return {"ok": True, "sku": sku, "old": old, "new": round(price, 2),
            "target": tgt, "shadows_synced": synced, "shadows_skipped": skipped}


def sync_shadow_only(sku: str) -> dict:
    """מיישר את מוצרי הצל למחיר הנוכחי של הווריאציה, בלי לשנות מחיר."""
    base, auth = _wc()
    tgt = find_by_sku(sku)
    if not tgt:
        return {"ok": False, "error": f"לא נמצא מוצר עם מק\"ט {sku}"}
    price = float(tgt.get("price") or 0)
    if price <= 0:
        return {"ok": False, "error": "אין מחיר תקף לווריאציה"}
    synced = []
    for sh in shadows_for(tgt["parent"]):
        if not _shadow_matches(tgt, sh):
            continue
        if str(sh.get("price") or "") == f"{price:.2f}".rstrip("0").rstrip("."):
            continue                                  # כבר מסונכרן
        rr = requests.put(f"{base}/wp-json/wc/v3/products/{sh['id']}", auth=auth,
                          timeout=45, json={"regular_price": f"{price:.2f}"})
        if rr.ok:
            synced.append({"id": sh["id"], "name": sh["name"], "price": price})
    return {"ok": True, "sku": sku, "price": price, "synced": synced}


def pending() -> list:
    """שינויי מחיר שנעשו כאן ועדיין לא סומנו כמעודכנים בקופה."""
    out = []
    for k, v in db.sales_state_prefix("zap_pending:"):
        if not v:
            continue
        try:
            out.append(json.loads(v))
        except Exception:  # noqa: BLE001
            pass
    return sorted(out, key=lambda x: x.get("at", ""), reverse=True)


def clear_pending(sku: str) -> None:
    db.sales_state_set(f"zap_pending:{sku}", "")


# ─────────────────────── הצגה/הסתרה בזאפ ───────────────────────
def zap_visibility(product_id: int = 0, hidden: bool = True, sku: str = "") -> dict:
    """הדלקה/כיבוי של המוצר בפיד זאפ — אותו צ׳קבוקס שבעריכת המוצר.
    ⚠️ הערך חייב להיות 'yes' ולא '1' — התוסף מצפה בדיוק לזה (נלמד בכאב).
    הדגל יושב תמיד על מוצר-האב; לוריאציה אין הגדרת זאפ משלה."""
    if not product_id:
        tgt = find_by_sku(sku)
        if not tgt:
            return {"ok": False, "error": "לא נמצא מוצר למק״ט הזה"}
        product_id = tgt["parent"]
    base, auth = _wc()
    r = requests.put(f"{base}/wp-json/wc/v3/products/{product_id}", auth=auth, timeout=45,
                     json={"meta_data": [{"key": "_woocommerce_zap_disable",
                                          "value": "yes" if hidden else ""}]})
    if not r.ok:
        return {"ok": False, "error": f"HTTP {r.status_code}", "detail": r.text[:200]}
    return {"ok": True, "product_id": product_id, "hidden": hidden}


def feed(cat: int = 1934) -> list:
    """הפיד החי שנשלח לזאפ. ⚠️ Cloudflare חוסם את /zap/ לכל בקשה שאינה דפדפן;
    הכלל שנוסף (26/07/2026) מדלג רק כשמגיעה הכותרת x-gm-feed עם הסוד."""
    import xml.etree.ElementTree as ET
    base = os.getenv("WC_STORE_URL", "https://greenmobile.co.il").rstrip("/")
    key = os.getenv("ZAP_FEED_KEY", "")
    if not key:
        logger.warning("zap feed: ZAP_FEED_KEY missing")
        return []
    try:
        r = requests.get(f"{base}/zap/", params={"product_cat": cat}, timeout=90,
                         headers={"x-gm-feed": key, "User-Agent": "GreenOS/1.0"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:  # noqa: BLE001
        logger.warning("zap feed failed: %s", e)
        return []
    out = []
    for p in root.iter("PRODUCT"):
        g = lambda t: (p.findtext(t) or "").strip()   # noqa: E731
        out.append({"num": p.get("NUM"), "url": g("PRODUCT_URL"), "name": g("PRODUCT_NAME"),
                    "sku": g("CATALOG_NUMBER"), "price": g("PRICE"),
                    "code": g("PRODUCTCODE")})
    return out


def shadow_state(sku: str) -> dict:
    """מצב מוצר הצל מול הווריאציה — לעמודת "מחיר צל" ולחיווי פער.
    המטרה (אסי): לוודא שהמחיר שזאפ רואה זהה למחיר שההורה מציג."""
    tgt = find_by_sku(sku)
    if not tgt:
        return {"ok": False, "error": "לא נמצא מוצר"}
    site = float(tgt.get("price") or 0)
    mine = [sh for sh in shadows_for(tgt["parent"]) if _shadow_matches(tgt, sh)]
    if not mine:
        return {"ok": True, "sku": sku, "site_price": site, "shadow": None,
                "state": "no_shadow"}     # דרוש צל — הדגם לא ייראה בזאפ בלי
    sh = mine[0]
    sp = float(sh.get("price") or 0)
    return {"ok": True, "sku": sku, "site_price": site, "shadow_id": sh["id"],
            "shadow_name": sh.get("name"), "shadow_price": sp,
            "drift": round(sp - site, 2),
            "state": "synced" if abs(sp - site) < 0.5 else "drift"}


def create_shadow(sku: str) -> dict:
    """יוצר מוצר צל לזאפ לווריאציה. חמשת השלבים כפי שתועדו אצלנו:
    external+hidden, URL עם האטריביוט, קטגוריות+מותג מההורה, תמונה, והסתרת
    ההורה מזאפ. ⚠️ הערך של _woocommerce_zap_disable חייב להיות 'yes'.
    ⚠️ הכותרת חייבת להתאים לכותרת דגם ההשוואה בזאפ (שם + נפח + RAM), אחרת
    זאפ לא ישייך את המוצר לשום דף — זו הסיבה שמוצר צל קיים בכלל."""
    base, auth = _wc()
    tgt = find_by_sku(sku)
    if not tgt:
        return {"ok": False, "error": f"לא נמצא מוצר עם מק\"ט {sku}"}
    if tgt["kind"] != "variation":
        return {"ok": False, "error": "מוצר צל נדרש רק לווריאציה של מוצר משתנה"}
    exist = [sh for sh in shadows_for(tgt["parent"]) if _shadow_matches(tgt, sh)]
    if exist:
        return {"ok": False, "error": "כבר קיים מוצר צל לתצורה הזו",
                "shadow_id": exist[0]["id"]}
    par = requests.get(f"{base}/wp-json/wc/v3/products/{tgt['parent']}", auth=auth,
                       timeout=45).json()
    qs = "&".join(f"attribute_{k}={v}" for k, v in (tgt.get("attrs") or {}).items())
    if not qs:
        return {"ok": False, "error": "לא זוהו אטריביוטים לווריאציה — לא ניתן לבנות URL"}
    payload = {
        "name": f"{par.get('name','')} {tgt.get('cap','')}".strip(),
        "type": "external", "status": "publish", "catalog_visibility": "hidden",
        "external_url": f"{par.get('permalink','').rstrip('/')}/?{qs}",
        "regular_price": str(tgt.get("price") or ""),
        "categories": par.get("categories") or [],
        "brands": par.get("brands") or [],
        "images": (par.get("images") or [])[:1],
        "meta_data": [{"key": "gm_parent_product_id", "value": str(tgt["parent"])}],
    }
    r = requests.post(f"{base}/wp-json/wc/v3/products", auth=auth, timeout=60, json=payload)
    if not r.ok:
        return {"ok": False, "error": f"יצירה נכשלה ({r.status_code})", "detail": r.text[:200]}
    new = r.json()
    # ההורה חייב להיות מוסתר מזאפ, אחרת גם הוא וגם הצללים יופיעו
    zap_visibility(tgt["parent"], True)
    return {"ok": True, "shadow_id": new.get("id"), "name": new.get("name"),
            "url": payload["external_url"], "parent_hidden": True,
            "note": "⚠️ ודא שהכותרת תואמת לכותרת דגם ההשוואה בזאפ (שם + נפח + RAM)"}
