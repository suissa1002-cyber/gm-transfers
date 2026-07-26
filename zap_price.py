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


def _cap_from_attrs(attrs: list) -> str | None:
    """הנפח של הווריאציה, מנורמל לסלאג של זאפ: 512GB → 00512gb, 1TB → 01024gb."""
    for a in attrs or []:
        opt = str(a.get("option") or "")
        m = re.search(r"(\d+)\s*(TB|GB)", opt, re.I)
        if m:
            gb = int(m.group(1)) * (1024 if m.group(2).upper() == "TB" else 1)
            return f"{gb:05d}gb"
    return None


def find_by_sku(sku: str) -> dict | None:
    """הווריאציה (או המוצר הפשוט) שנושאת את המק"ט, יחד עם ההורה שלה."""
    base, auth = _wc()
    r = requests.get(f"{base}/wp-json/wc/v3/products", auth=auth, timeout=45,
                     params={"sku": sku, "per_page": 10,
                             "_fields": "id,name,type,price,regular_price,parent_id"})
    for p in (r.json() if r.ok else []):
        if str(p.get("id")):
            return {"kind": "product", "id": p["id"], "parent": p.get("parent_id") or p["id"],
                    "name": p.get("name"), "price": p.get("price")}
    # מק"ט של וריאציה אינו נתפס בחיפוש המוצרים — עוברים על ההורים המשתנים
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
                            "cap": _cap_from_attrs(x.get("attributes"))}
        page += 1
    return None


def shadows_for(parent_id: int) -> list:
    """מוצרי הצל של אותו הורה. ⚠️ הם type=external ו-hidden, ולכן לא מופיעים
    בשליפות קטלוג רגילות — מחפשים לפי המטא gm_parent_product_id."""
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
                out.append({"id": p["id"], "name": p.get("name"), "price": p.get("price"),
                            "cap": _cap_slug(p.get("external_url") or "")})
        page += 1
    return out


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
            # מסנכרנים רק את הצל של אותו נפח; צל של נפח אחר אינו קשור לשינוי
            if tgt.get("cap") and sh.get("cap") and sh["cap"] != tgt["cap"]:
                skipped.append({"id": sh["id"], "name": sh["name"], "reason": "נפח אחר"})
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
        if tgt.get("cap") and sh.get("cap") and sh["cap"] != tgt["cap"]:
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
