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
    """הנפח של הווריאציה, מנורמל לסלאג של זאפ: 512GB → 00512gb, 1TB → 01024gb.
    ⚠️ לקיחת ההתאמה **הראשונה** קראה מ-"12GB+256GB+Gaming Kit" את ה-12GB,
    ולכן 256GB ו-512GB של Infinix GT 30 Pro נראו כאותה תצורה והמוצר סווג
    בטעות כנפח-יחיד (אסי, 27/07/2026). האחסון תמיד ≥ הזיכרון, ואין היום
    סמארטפון מתחת ל-64GB — לוקחים את הגדול מבין הערכים הריאליים."""
    vals = []
    for a in attrs or []:
        for n, u in re.findall(r"(\d+)\s*(TB|GB)", str(a.get("option") or ""), re.I):
            vals.append(int(n) * (1024 if u.upper() == "TB" else 1))
    real = [v for v in vals if v >= 64] or vals
    return f"{max(real):05d}gb" if real else None


_TERM_CACHE = {}


_TERM_KEY = "zap_term_slugs"


def _term_slugs(base, auth) -> dict:
    """{taxonomy: {שם-אופציה-lower: slug}} — כדי לתרגם "512GB" ל-"00512gb".
    ⚠️ ה-REST מחזיר בווריאציה את **שם** האופציה, ומוצר הצל נושא ב-URL את
    ה-**slug**. בלי המפה הזו ההשוואה נכשלת וסונכרן הצל הלא נכון."""
    if _TERM_CACHE:
        return _TERM_CACHE
    # ⚠️ בניית המפה מאפס שולפת כל אטריביוט וכל המונחים שלו (עד 5 עמודות לכל
    # אחד) — כ-100 שניות. היא נשמרה בזיכרון התהליך בלבד, ולכן כל deploy או
    # מיחזור מופע ב-Render החזיר את המחיר המלא ללחיצה הראשונה (אסי,
    # 28/07/2026: "פשוט היה מאוד איטי"). המפה כמעט קבועה — נשמרת ל-24 שעות.
    try:
        raw = db.sales_state_get(_TERM_KEY) or ""
        if raw:
            d = json.loads(raw)
            if (datetime.now() - datetime.fromisoformat(d["at"])).total_seconds() < 86400:
                _TERM_CACHE.update(d["map"])
                return _TERM_CACHE
    except Exception:  # noqa: BLE001
        pass
    try:
        attrs = requests.get(f"{base}/wp-json/wc/v3/products/attributes", auth=auth,
                             timeout=45).json()
    except Exception:  # noqa: BLE001
        return {}
    for a in (attrs if isinstance(attrs, list) else []):
        # ⚠️ WooCommerce מחזיר את ה-slug כבר עם הקידומת pa_ ("pa_color"), והוספה
        # נוספת יצרה "pa_pa_color". ווקומרס לא מזהה פרמטר כזה, אף וריאציה לא
        # נבחרת בהפניה מהצל, ולכן מוצג **טווח מחירים** במקום המחיר המדויק —
        # וזאפ פוסלים הצגה כזו (אסי, 28/07/2026).
        _sl = str(a.get("slug") or "")
        tax = _sl if _sl.startswith("pa_") else "pa_" + _sl
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
    try:
        db.sales_state_set(_TERM_KEY, json.dumps(
            {"at": datetime.now().isoformat(timespec="seconds"), "map": _TERM_CACHE},
            ensure_ascii=False))
    except Exception as e:  # noqa: BLE001
        logger.warning("zap: term map persist failed: %s", e)
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
            _n = nm.strip().replace(" ", "-")
            out[_n if _n.startswith("pa_") else "pa_" + _n] = opt
    return out


def shadow_map() -> dict:
    """כל מוצרי הצל בשליפה **אחת**, ממופים לפי מזהה מוצר-האב.
    ⚠️ shadows_for עושה עד 4 עמודות של 100 מוצרים **לכל שורה**, ולכן בדיקת
    הצל לשורה בודדת לקחה עשרות שניות ונראתה כאילו לא קרה כלום (אסי,
    27/07/2026). כאן זו שליפה אחת שמשרתת את כל הטבלה."""
    base, auth = _wc()
    out: dict = {}
    page = 1
    while page <= 6:
        try:
            r = requests.get(f"{base}/wp-json/wc/v3/products", auth=auth, timeout=60,
                             params={"per_page": 100, "page": page, "type": "external",
                                     "status": "any",
                                     "_fields": "id,name,price,external_url,meta_data"})
            rows = r.json() if r.ok else []
        except Exception as e:  # noqa: BLE001
            logger.warning("zap shadow_map page %s failed: %s", page, e)
            break
        if not isinstance(rows, list) or not rows:
            break
        for p in rows:
            meta = {m["key"]: str(m["value"]) for m in (p.get("meta_data") or [])}
            par = meta.get("gm_parent_product_id")
            if not par:
                continue
            url = p.get("external_url") or ""
            out.setdefault(str(par), []).append(
                {"id": p["id"], "name": p.get("name"),
                 "price": float(p.get("price") or 0), "cap": _cap_slug(url)})
        page += 1
    return out


_SHADOW_CACHE = {"at": 0.0, "map": {}}


def _shadow_map_cached(ttl: float = 60.0) -> dict:
    """מפת הצללים עם מטמון קצר. ⚠️ shadows_for עשה 4 עמודות × 100 מוצרים
    **בכל קריאה**, ו-create_shadow קורא לו כדי לבדוק "כבר קיים" — לכן יצירת
    מוצר צל ארכה מעל שתי דקות והממשק נתקע על ספינר (אסי, 28/07/2026)."""
    import time as _t
    now = _t.time()
    if now - _SHADOW_CACHE["at"] > ttl or not _SHADOW_CACHE["map"]:
        _SHADOW_CACHE["map"] = shadow_map()
        _SHADOW_CACHE["at"] = now
    return _SHADOW_CACHE["map"]


def _shadow_cache_clear() -> None:
    _SHADOW_CACHE["at"] = 0.0


def shadows_for(parent_id: int) -> list:
    """מוצרי הצל של אותו הורה. ⚠️ הם type=external ו-hidden, ולכן לא מופיעים
    בשליפות קטלוג רגילות — מחפשים לפי המטא gm_parent_product_id.
    ⚠️ נמחקה בטעות בקומיט c369ba2 — כל קריאה ל-shadow_state החזירה 500 בשקט
    ועמודת "מחיר צל" לא עבדה מאז. שוחזרה 27/07/2026, כולל attrs להשוואה."""
    cached = _shadow_map_cached().get(str(parent_id))
    if cached is not None:
        return cached
    base, auth = _wc()
    out = []
    page = 1
    while page <= 4:
        r = requests.get(f"{base}/wp-json/wc/v3/products", auth=auth, timeout=60,
                         params={"per_page": 100, "page": page, "type": "external",
                                 "status": "any", "_fields": "id,name,price,regular_price,"
                                                             "external_url,meta_data"})
        rows = r.json() if r.ok else []
        if not rows:
            break
        for p in rows:
            meta = {m["key"]: str(m["value"]) for m in (p.get("meta_data") or [])}
            if str(meta.get("gm_parent_product_id") or "") == str(parent_id):
                url = p.get("external_url") or ""
                out.append({"id": p["id"], "name": p.get("name"), "price": p.get("price"),
                            "cap": _cap_slug(url), "attrs": _shadow_attrs(url)})
        page += 1
    return out


def find_by_pid(pid, cap: str = "") -> dict | None:
    """אותה תשובה כמו find_by_sku, אבל לפי מזהה מוצר ב-WC.
    ⚠️ נחוץ כי ל-45 מתוך 65 מוצרי הפיד אין מק"ט על ההורה (הוא יושב על
    הוריאציה), ובלי זה כפתורי הפעולה בשורות האלה פשוט לא עושים כלום."""
    base, auth = _wc()
    r = requests.get(f"{base}/wp-json/wc/v3/products/{pid}", auth=auth, timeout=45,
                     params={"_fields": "id,name,type,price,regular_price,attributes"})
    if not r.ok:
        return None
    p = r.json()
    if p.get("type") == "variable":
        v = requests.get(f"{base}/wp-json/wc/v3/products/{pid}/variations", auth=auth,
                         timeout=45, params={"per_page": 100,
                                             "_fields": "id,sku,price,regular_price,attributes"})
        vs = [x for x in (v.json() if v.ok else []) if float(x.get("price") or 0) > 1]
        # ⚠️ כשמבקשים נפח מסוים — מחזירים אותו ולא את הזולה. בלי זה לחיצה על
        # "צור מוצר צל" ל-256GB פנתה לוריאציית 128GB (הזולה), מצאה שכבר יש לה
        # צל, והחזירה "כבר קיים מוצר צל לתצורה הזו" (אסי, 28/07/2026).
        if cap:
            vs = [x for x in vs if _cap_from_attrs(x.get("attributes")) == cap] or vs
        if vs:   # בהיעדר נפח מבוקש — הזולה היא זו שמתחרה בזאפ
            x = min(vs, key=lambda z: float(z.get("price") or 0))
            return {"kind": "variation", "id": x["id"], "parent": int(pid),
                    "sku": x.get("sku"), "name": p.get("name"), "price": x.get("price"),
                    "attrs": _variation_slugs(base, auth, x),
                    "cap": _cap_from_attrs(x.get("attributes"))}
    return {"kind": "product", "id": p["id"], "parent": p["id"], "sku": p.get("sku"),
            "type": p.get("type"), "name": p.get("name"), "price": p.get("price"),
            "attrs": _variation_slugs(base, auth, p),
            "cap": _cap_from_attrs(p.get("attributes")) or _cap_from_text(p.get("name"))}


def find_target(sku: str = "", pid=None, cap: str = "") -> dict | None:
    """זהות השורה בכלי זאפ: מק"ט אם יש, אחרת מזהה המוצר (+נפח מבוקש)."""
    if (sku or "").strip():
        t = find_by_sku(sku)
        if t and (not cap or t.get("cap") == cap):
            return t
    return find_by_pid(pid, cap) if pid else None


def find_by_sku(sku: str) -> dict | None:
    """הווריאציה (או המוצר הפשוט) שנושאת את המק"ט, יחד עם ההורה והאטריביוטים.
    ⚠️ מק"ט ריק חייב להחזיר None. WooCommerce **מתעלם** מ-?sku= ריק ומחזיר את
    כל הקטלוג, החדש ראשון — והקוד לקח את הראשון ברשימה. כך "הסתר מזאפ" על
    Pixel 9a הסתיר אוזניות JBL והחזיר הצלחה (אסי, 27/07/2026)."""
    if not (sku or "").strip():
        return None
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


def set_price(sku: str, price: float, sync_shadow: bool = True, pid=None) -> dict:
    """מעדכן את מחיר האתר למק"ט, ואופציונלית מסנכרן את מוצר הצל התואם."""
    base, auth = _wc()
    tgt = find_target(sku, pid)
    if not tgt:
        return {"ok": False, "error": f"לא נמצא מוצר באתר עם מק\"ט {sku}"}
    old = tgt.get("price")
    url = (f"{base}/wp-json/wc/v3/products/{tgt['parent']}/variations/{tgt['id']}"
           if tgt["kind"] == "variation" else
           f"{base}/wp-json/wc/v3/products/{tgt['id']}")
    r = requests.put(url, auth=auth, timeout=45, json={"regular_price": f"{price:.2f}"})
    if not r.ok:
        return {"ok": False, "error": f"עדכון נכשל ({r.status_code})", "detail": r.text[:200]}

    # ⚠️ שורה בכלי הזאפ = **נפח**, לא צבע: דף ההשוואה מאחד את כל הצבעים.
    # העדכון פגע בווריאציה אחת בלבד, ו-512GB בשני צבעים נשאר במחירים שונים
    # (אסי, 27/07/2026). מיישרים את כל אחיות אותו נפח — אבל **רק** את אלה
    # שהיו באותו מחיר, כדי לא לדרוס הפרש מכוון (eSIM מול nano-SIM+eSIM).
    siblings = []
    if tgt["kind"] == "variation" and tgt.get("cap"):
        try:
            v = requests.get(f"{base}/wp-json/wc/v3/products/{tgt['parent']}/variations",
                             auth=auth, timeout=45,
                             params={"per_page": 100,
                                     "_fields": "id,sku,price,attributes"})
            for x in (v.json() if v.ok else []):
                if x["id"] == tgt["id"] or _cap_from_attrs(x.get("attributes")) != tgt["cap"]:
                    continue
                if abs(float(x.get("price") or 0) - float(old or 0)) > 0.5:
                    skip_reason = f"מחיר שונה (₪{x.get('price')})"
                    siblings.append({"id": x["id"], "sku": x.get("sku"),
                                     "updated": False, "reason": skip_reason})
                    continue
                rr = requests.put(f"{base}/wp-json/wc/v3/products/{tgt['parent']}"
                                  f"/variations/{x['id']}", auth=auth, timeout=45,
                                  json={"regular_price": f"{price:.2f}"})
                siblings.append({"id": x["id"], "sku": x.get("sku"),
                                 "color": next((a.get("option") for a in (x.get("attributes") or [])
                                                if "צבע" in str(a.get("name") or "")), ""),
                                 "updated": bool(rr.ok),
                                 **({} if rr.ok else {"reason": f"HTTP {rr.status_code}"})})
        except Exception as e:  # noqa: BLE001
            logger.warning("zap: sibling price sync failed: %s", e)

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

    # ⚠️ הקופה לא מתעדכנת מכאן — רושמים "ממתין לקופה" כדי שלא ייווצר פער שקט.
    # ⚠️ ורושמים את **כל** מק"טי הקופה שהושפעו: העדכון נוגע בכל הצבעים של
    # אותו נפח, ורישום מק"ט אחד שלח את אסי לעדכן בקופה רק אחד מהם
    # (27/07/2026). המפתח לפי מזהה המוצר — מק"ט עלול להיות ריק.
    all_skus = [x for x in ([tgt.get("sku") or sku]
                            + [y.get("sku") for y in siblings if y.get("updated")]) if x]
    key = str(tgt.get("id") or sku or tgt.get("parent"))
    db.sales_state_set(f"zap_pending:{key}", json.dumps(
        {"key": key, "sku": sku or (all_skus[0] if all_skus else ""),
         "skus": all_skus, "name": tgt.get("name"), "old": old,
         "new": round(price, 2),
         "at": datetime.now().isoformat(timespec="seconds"),
         "shadows": len(synced)}, ensure_ascii=False))
    logger.info("zap price %s: %s → %s (shadows synced: %d)", sku, old, price, len(synced))
    sib_ok = [x for x in siblings if x.get("updated")]
    return {"ok": True, "sku": sku, "old": old, "new": round(price, 2),
            "target": tgt, "shadows_synced": synced, "shadows_skipped": skipped,
            "variants_updated": 1 + len(sib_ok),
            "siblings": siblings}


def _wp_auth():
    """אימות WP (Application Password) — נדרש למסלולי ה-REST של התוספים שלנו."""
    return (os.getenv("WP_USERNAME", ""), os.getenv("WP_APP_PASSWORD", ""))


def sync_via_plugin(parent_id) -> dict | None:
    """מסנכרן את כל צללי ההורה דרך `gm_zap_sync_shadow` שבתוסף Zap Manager.
    ⚠️ למה דרך התוסף ולא כאן: הפונקציה שלו מסנכרנת regular_price **וגם**
    sale_price **וגם** stock_status, ושומרת דרך אובייקט WC כדי לנקות את
    המטמונים (בלעדיו "וי ירוק אבל המחיר הישן נשאר בפיד" — מתועד אצלו בקוד).
    השכפול בפייתון סנכרן מחיר בלבד, ולכן וריאציה שאזלה נשארה "במלאי" בצל
    (אסי, 28/07/2026). None = המסלול לא קיים ⇒ נופלים חזרה למימוש המקומי."""
    base, _ = _wc()
    try:
        r = requests.post(f"{base}/wp-json/gm-zap/v1/sync-parent/{int(parent_id)}",
                          auth=_wp_auth(), timeout=90)
    except Exception as e:  # noqa: BLE001
        logger.warning("zap: plugin sync failed: %s", e)
        return None
    if r.status_code in (404, 401, 403):
        logger.info("zap: plugin route unavailable (%s) — נופלים למימוש המקומי",
                    r.status_code)
        return None
    if not r.ok:
        return {"ok": False, "error": f"התוסף החזיר {r.status_code}",
                "detail": r.text[:200]}
    try:
        return r.json()
    except Exception:  # noqa: BLE001
        return None


def sync_shadow_only(sku: str = "", pid=None) -> dict:
    """מיישר את מוצרי הצל למחיר הנוכחי של הווריאציה, בלי לשנות מחיר."""
    base, auth = _wc()
    tgt = find_target(sku, pid)
    if not tgt:
        return {"ok": False, "error": f"לא נמצא מוצר עם מק\"ט {sku}"}
    # קודם כל — התוסף. הוא מסנכרן גם מלאי, ומנקה את מטמוני WooCommerce.
    via = sync_via_plugin(tgt["parent"])
    if via is not None:
        if not via.get("ok"):
            return via
        rows = via.get("shadows") or []
        done = [x for x in rows if x.get("result") == "synced"]
        return {"ok": True, "sku": sku, "via": "plugin",
                "name": tgt.get("name") or "",
                "synced": [{"id": x["shadow_id"], "price": x.get("price"),
                            "stock": x.get("stock")} for x in done],
                "skipped": [x for x in rows if x.get("result") != "synced"]}
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
    return {"ok": True, "sku": sku, "price": price, "synced": synced,
            "name": tgt.get("name") or ""}


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


def clear_pending(key: str) -> None:
    """הרשומה נשמרת לפי מזהה הווריאציה; תמיכה לאחור במפתח מק"ט ישן."""
    db.sales_state_set(f"zap_pending:{key}", "")


# ─────────────────────── הצגה/הסתרה בזאפ ───────────────────────
# ─────────── פעולות שממתינות לסריקה הבאה של זאפ ───────────
def act_log(pid, kind: str, detail: str = "", at: str = "") -> None:
    """רישום פעולה שהשפעתה תיראה רק אחרי שזאפ יסרוק מחדש (6-24 שעות).
    ⚠️ בלי זה אי אפשר לדעת מהטבלה על מה כבר עבדת: אסי שינה כותרת, והשורה
    נראתה בדיוק כמו קודם (27/07/2026)."""
    try:
        db.sales_state_set(f"zap_act:{pid}", json.dumps(
            {"pid": str(pid), "kind": kind, "detail": detail[:160],
             "at": at or datetime.now().isoformat(timespec="seconds")},
            ensure_ascii=False))
    except Exception as e:  # noqa: BLE001
        logger.warning("zap act_log failed: %s", e)


def acts() -> dict:
    """מזהה מוצר → הפעולה האחרונה שנעשתה עליו וממתינה לזאפ."""
    out = {}
    for k, v in db.sales_state_prefix("zap_act:"):
        if not v:
            continue
        try:
            r = json.loads(v)
            out[str(r.get("pid"))] = r
        except Exception:  # noqa: BLE001
            pass
    return out


def act_clear(pid) -> None:
    db.sales_state_set(f"zap_act:{pid}", "")


def rename(pid, name: str) -> dict:
    """שינוי שם המוצר באתר. ⚠️ הכותרת גלויה ללקוחות, ולכן הפעולה מוצגת
    לאישור עם השם הישן והחדש זה מול זה. ה-slug (הקישור) אינו משתנה —
    WooCommerce משנה רק את הכותרת, וקישורים קיימים נשארים תקינים."""
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "כותרת ריקה"}
    base, auth = _wc()
    r = requests.get(f"{base}/wp-json/wc/v3/products/{pid}", auth=auth, timeout=45,
                     params={"_fields": "id,name,slug"})
    if not r.ok:
        return {"ok": False, "error": f"לא נמצא מוצר ({r.status_code})"}
    before = r.json()
    w = requests.put(f"{base}/wp-json/wc/v3/products/{pid}", auth=auth, timeout=45,
                     json={"name": name})
    if not w.ok:
        return {"ok": False, "error": f"העדכון נכשל ({w.status_code})",
                "detail": w.text[:200]}
    after = w.json()
    act_log(pid, "title", after.get("name") or name)
    return {"ok": True, "product_id": int(pid), "old": before.get("name"),
            "name": after.get("name"), "slug": after.get("slug"),
            "note": "⚠️ זאפ סורק את הפיד מחדש תוך 6-24 שעות"}


def zap_visibility(product_id: int = 0, hidden: bool = True, sku: str = "") -> dict:
    """הדלקה/כיבוי של המוצר בפיד זאפ — אותו צ׳קבוקס שבעריכת המוצר.
    ⚠️ הערך חייב להיות 'yes' ולא '1' — התוסף מצפה בדיוק לזה (נלמד בכאב).
    הדגל יושב תמיד על מוצר-האב; לוריאציה אין הגדרת זאפ משלה."""
    if not product_id:
        tgt = find_target(sku, None)
        if not tgt:
            return {"ok": False, "error": "לא נמצא מוצר — צריך מק״ט או מזהה מוצר"}
        product_id = tgt["parent"]
    base, auth = _wc()
    r = requests.put(f"{base}/wp-json/wc/v3/products/{product_id}", auth=auth, timeout=45,
                     json={"meta_data": [{"key": "_woocommerce_zap_disable",
                                          "value": "yes" if hidden else ""}]})
    if not r.ok:
        return {"ok": False, "error": f"HTTP {r.status_code}", "detail": r.text[:200]}
    # ⚠️ מחזירים את השם **שהשרת ראה** ולא את זה שהמסך הציג: כך טעות זהות
    # נראית מיד באישור, במקום להתגלות בפיד יומיים אחרי (אסי, 27/07/2026).
    try:
        affected = (r.json() or {}).get("name") or ""
    except Exception:  # noqa: BLE001
        affected = ""
    act_log(product_id, "hidden" if hidden else "shown", affected)
    return {"ok": True, "product_id": product_id, "hidden": hidden, "name": affected}



def zap_visibility_bulk(pids: list, hidden: bool = True) -> dict:
    """הסתרה/חשיפה המונית בזאפ. ⚠️ 150 קריאות PUT נפרדות לוקחות דקות ארוכות
    ונופלות על תקרת הזמן של הפרוקסי; WooCommerce מקבל עד 100 עדכונים
    בבקשה אחת דרך products/batch. מחזירים את השם **שהשרת ראה** לכל מוצר,
    כדי שטעות זהות תתגלה מיד ולא בפיד יומיים אחרי (אסי, 27/07/2026).

    נועד לאיפוס תחום שלם: "כל האוזניות מוסתרות מלבד 10 הנמכרים" — במקום
    151 לחיצות ידניות (אסי, 28/07/2026)."""
    base, auth = _wc()
    ids = [int(p) for p in pids if str(p).strip().isdigit()]
    if not ids:
        return {"ok": False, "error": "לא התקבלו מזהי מוצר"}
    val = "yes" if hidden else ""
    done, failed = [], []
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        body = {"update": [{"id": pid,
                            "meta_data": [{"key": "_woocommerce_zap_disable", "value": val}]}
                           for pid in chunk]}
        try:
            r = requests.post(f"{base}/wp-json/wc/v3/products/batch", auth=auth,
                              timeout=180, json=body)
        except Exception as e:  # noqa: BLE001
            failed += [{"id": pid, "error": str(e)[:80]} for pid in chunk]
            continue
        if not r.ok:
            failed += [{"id": pid, "error": f"HTTP {r.status_code}"} for pid in chunk]
            continue
        seen = {}
        for u in ((r.json() or {}).get("update") or []):
            if u.get("id"):
                seen[int(u["id"])] = u.get("name") or ""
        for pid in chunk:
            if pid in seen:
                done.append({"id": pid, "name": seen[pid]})
                act_log(pid, "hidden" if hidden else "shown", seen[pid])
            else:
                failed.append({"id": pid, "error": "לא הוחזר בתשובה"})
    _shadow_cache_clear()
    return {"ok": not failed, "hidden": hidden, "changed": len(done),
            "failed": failed, "rows": done}


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
                    "code": g("PRODUCTCODE"),
                    # שדות שזאפ משתמש בהם לשיוך לדגם — ריקים ברובם אצלנו
                    "model": g("MODEL"), "manufacturer": g("MANUFACTURER"),
                    "details": g("DETAILS"), "warranty": g("WARRANTY"),
                    "size": g("SIZE"), "image": g("IMAGE")})
    return out


def shadow_state(sku: str, pid=None) -> dict:
    """מצב מוצר הצל מול הווריאציה — לעמודת "מחיר צל" ולחיווי פער.
    המטרה (אסי): לוודא שהמחיר שזאפ רואה זהה למחיר שההורה מציג."""
    tgt = find_target(sku, pid)
    if not tgt:
        return {"ok": False, "error": "לא נמצא מוצר"}
    site = float(tgt.get("price") or 0)
    if tgt.get("type") == "external":
        # השורה **היא** מוצר הצל — זה בדיוק מה שזאפ קורא, אין למה לסנכרן.
        return {"ok": True, "sku": sku, "site_price": site, "state": "is_shadow",
                "shadow_id": tgt["id"], "shadow_price": site, "drift": 0}
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


def create_shadow(sku: str = "", pid=None, name: str = "", cap: str = "") -> dict:
    """יוצר מוצר צל לזאפ לווריאציה. חמשת השלבים כפי שתועדו אצלנו:
    external+hidden, URL עם האטריביוט, קטגוריות+מותג מההורה, תמונה, והסתרת
    ההורה מזאפ. ⚠️ הערך של _woocommerce_zap_disable חייב להיות 'yes'.
    ⚠️ הכותרת חייבת להתאים לכותרת דגם ההשוואה בזאפ (שם + נפח + RAM), אחרת
    זאפ לא ישייך את המוצר לשום דף — זו הסיבה שמוצר צל קיים בכלל."""
    base, auth = _wc()
    tgt = find_target(sku, pid, cap)
    if not tgt:
        return {"ok": False, "error": f"לא נמצא מוצר עם מק\"ט {sku}"}
    if cap and tgt.get("cap") != cap:
        return {"ok": False,
                "error": f"לא נמצאה וריאציה לנפח המבוקש ({cap}) במוצר הזה"}
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
        # כותרת מפורשת מנצחת: המרשם מחזיר את כותרת דגם ההשוואה עצמה, וזו
        # הדרך היחידה להבטיח שיוב בסריקה הראשונה של זאפ (אסי, 27/07/2026).
        "name": (name or f"{par.get('name','')} {tgt.get('cap','')}").strip(),
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
    _shadow_cache_clear()          # הצל החדש חייב להיראות בקריאה הבאה
    # ⚠️ רושמים על **הצל עצמו** ולא רק על ההורה: הצל הוא השורה שמוצגת בטבלה
    # (הוא זה שבפיד), ולכן חיווי שנרשם על ההורה בלבד לא נראה בשום מקום
    # (אסי, 28/07/2026 — ביקש לראות "נוצר מוצר צל" עם תאריך, כמו בכותרות).
    act_log(new.get("id"), "shadow", new.get("name") or "")
    act_log(tgt["parent"], "shadow", new.get("name") or "")
    # מנקים את מטמון LiteSpeed לצל החדש — אחרת ההפניה הראשונה נשמרת ונתקעת
    try:
        requests.post(f"{base}/wp-json/gm-zap/v1/purge/{new.get('id')}",
                      auth=_wp_auth(), timeout=30)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "shadow_id": new.get("id"), "name": new.get("name"),
            "url": payload["external_url"], "parent_hidden": True,
            "note": "⚠️ ודא שהכותרת תואמת לכותרת דגם ההשוואה בזאפ (שם + נפח + RAM)"}
