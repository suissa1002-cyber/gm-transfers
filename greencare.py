"""
Green Care — ניהול הגנה מורחבת פר-מוצר.

שני מסלולים (טירים) שנמכרים על-גבי מוצר האתר (WooCommerce):
  gc  — Green Care         : הרחבת אחריות שנה שנייה נטו. מחיר flat לפי מדרגת מחיר.
  gcp — Green Care Plus    : חבילת שברים 24 ח'. מחיר ~28% ממחיר המכשיר, רצפה/תקרה.

ברירת-המחדל של המחיר נגזרת מנוסחה (ראה CONCEPT.md §3). ניתן לדרוס פר-מוצר
ב-GreenOS (טבלה greencare_overrides) — הדריסה תמיד גוברת. ההגדרות חלות על
מוצר ההורה ב-WooCommerce וממילא על כל הווריאציות שלו (meta על ההורה בלבד).

הסנכרון ל-WooCommerce נכתב כ-meta_data על מוצר ההורה, באותו סגנון אימות
(consumer key/secret) של wc-connect ב-main.py — הווידג'ט בעמוד המוצר קורא אותו.
"""

import json
import os

import db


# ── נוסחת ברירת-המחדל ──
def price_gcp_default(price: float) -> int:
    """Green Care Plus: 28% ממחיר המוצר, מעוגל ל-₪10 הקרוב, ואז מוצמד ל-[₪200, ₪749].
    (מעגלים לפני ההצמדה כדי לשמור על התקרה ה"חכמה" ₪749 בדיוק בקצה העליון.)"""
    p = float(price or 0)
    rounded = round((p * 0.28) / 10.0) * 10
    return int(max(200, min(749, rounded)))


def price_gc_default(price: float) -> int:
    """Green Care (הרחבת שנה שנייה): ₪79 מתחת ל-₪1500, אחרת ₪99."""
    return 79 if float(price or 0) < 1500 else 99


# ── קריאה/כתיבה של דריסות (דרך db) ──
def get_override(wc_product_id) -> dict:
    """הדריסה השמורה למוצר, או {} אם אין."""
    return db.greencare_get(wc_product_id)


def set_override(wc_product_id, enabled, tier_gc, tier_gcp,
                 price_gc=None, price_gcp=None, by="") -> dict:
    """שומר/מעדכן דריסה ומחזיר את השורה השמורה."""
    return db.greencare_set(wc_product_id, enabled, tier_gc, tier_gcp,
                            price_gc, price_gcp, by)


def delete_override(wc_product_id) -> int:
    return db.greencare_delete(wc_product_id)


# ── חישוב המצב האפקטיבי (נוסחה + דריסה) ──
def compute_effective(product_price: float, override: dict) -> dict:
    """מאחד את נוסחת ברירת-המחדל עם הדריסה השמורה. מחזיר:
    {enabled, tiers:{gc:{on,price,source}, gcp:{on,price,source}}}
    source='override' אם המחיר נדרס ידנית, אחרת 'formula'."""
    ov = override or {}
    enabled = bool(int(ov.get("enabled", 1))) if ov else True
    on_gc = bool(int(ov.get("tier_gc", 1))) if ov else True
    on_gcp = bool(int(ov.get("tier_gcp", 1))) if ov else True

    def_gc = price_gc_default(product_price)
    def_gcp = price_gcp_default(product_price)

    o_gc = ov.get("price_gc")
    o_gcp = ov.get("price_gcp")
    gc_override = o_gc is not None and o_gc != ""
    gcp_override = o_gcp is not None and o_gcp != ""

    return {
        "enabled": enabled,
        "tiers": {
            "gc": {
                "on": on_gc,
                "price": (round(float(o_gc)) if gc_override else def_gc),
                "source": "override" if gc_override else "formula",
                "formula": def_gc,
            },
            "gcp": {
                "on": on_gcp,
                "price": (round(float(o_gcp)) if gcp_override else def_gcp),
                "source": "override" if gcp_override else "formula",
                "formula": def_gcp,
            },
        },
    }


# ── סנכרון ל-WooCommerce ──
def _wc_creds():
    u = os.getenv("WC_STORE_URL", "").rstrip("/")
    k = os.getenv("WC_CONSUMER_KEY", "")
    s = os.getenv("WC_CONSUMER_SECRET", "")
    return (u, k, s) if (u and k and s) else None


def push_to_wc(wc_product_id, effective: dict) -> dict:
    """כותב את הגדרות Green Care כ-meta_data על מוצר ההורה ב-WooCommerce.
    חלה על כל הווריאציות (meta על ההורה). מחזיר {ok, error?}."""
    creds = _wc_creds()
    if not creds:
        return {"ok": False, "error": "wc not configured"}
    base, k, s = creds
    tiers = (effective or {}).get("tiers", {})
    gc = tiers.get("gc", {})
    gcp = tiers.get("gcp", {})
    meta = [
        {"key": "_gm_greencare_enabled", "value": "1" if effective.get("enabled") else "0"},
        {"key": "_gm_greencare_tiers",
         "value": json.dumps({"gc": 1 if gc.get("on") else 0,
                              "gcp": 1 if gcp.get("on") else 0})},
        {"key": "_gm_greencare_price_gc", "value": str(gc.get("price", ""))},
        {"key": "_gm_greencare_price_gcp", "value": str(gcp.get("price", ""))},
    ]
    try:
        import requests as _rq
        url = base + f"/wp-json/wc/v3/products/{int(wc_product_id)}"
        r = _rq.put(url, json={"meta_data": meta}, auth=(k, s), timeout=20)
        if not r.ok:
            return {"ok": False, "error": f"wc {r.status_code}: {r.text[:160]}"}
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:160]}
