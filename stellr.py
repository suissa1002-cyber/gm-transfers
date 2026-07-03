"""stellr.py — לקוח Stellr Fusion API להנפקת קודים דיגיטליים (PlayStation/Xbox).

סביבה נשלטת כולה במשתני סביבה — מעבר UAT→Production בלי שינוי קוד:
  STELLR_BASE_URL   ברירת מחדל UAT: https://api-uat.stellr-net.com/fusion-v1
  STELLR_API_KEY    (header: x-api-key)
  STELLR_STORE_REF  ברירת מחדל 00001
  STELLR_CURRENCY   ברירת מחדל ILS
  STELLR_ENABLED    1 = מודול פעיל (מתג כיבוי ראשי)
  STELLR_AUTO       off | dry | on — אוטומציה לירוק מוחלט (dry = רושם בלי להנפיק)
  STELLR_MAX_VALUE  תקרת ערך לקוד בודד (ברירת מחדל 500)
  STELLR_MAX_QTY    תקרת קודים להזמנה (ברירת מחדל 3)

⚠️ כללי ברזל: לעולם לא לרשום PAN/PIN ללוג. amount נשלח כאובייקט {value, currency}.
"""
import logging
import os
import time

import requests

logger = logging.getLogger("stellr")

BASE_URL = os.environ.get("STELLR_BASE_URL",
                          "https://api-uat.stellr-net.com/fusion-v1").rstrip("/")
API_KEY = os.environ.get("STELLR_API_KEY", "")
STORE_REF = os.environ.get("STELLR_STORE_REF", "00001")
CURRENCY = os.environ.get("STELLR_CURRENCY", "ILS")
MAX_VALUE = float(os.environ.get("STELLR_MAX_VALUE", "650"))   # הקוד הגדול ביותר: Sony ₪635
MAX_QTY = int(os.environ.get("STELLR_MAX_QTY", "3"))


def enabled() -> bool:
    return bool(API_KEY) and os.environ.get("STELLR_ENABLED", "0") == "1"


def auto_mode() -> str:
    m = (os.environ.get("STELLR_AUTO", "off") or "off").lower()
    return m if m in ("off", "dry", "on") else "off"


def is_uat() -> bool:
    return "uat" in BASE_URL


def _headers() -> dict:
    return {"x-api-key": API_KEY, "Content-Type": "application/json"}


_cat_cache = {"at": 0.0, "data": None}


def catalog(fresh: bool = False) -> list:
    """קטלוג המוצרים הזמין לנו (cache 10 דק')."""
    if _cat_cache["data"] and not fresh and time.time() - _cat_cache["at"] < 600:
        return _cat_cache["data"]
    r = requests.get(f"{BASE_URL}/product", headers=_headers(), timeout=30)
    r.raise_for_status()
    data = r.json()
    _cat_cache["data"] = data
    _cat_cache["at"] = time.time()
    return data


def get_transaction(tx_id: str) -> dict:
    r = requests.get(f"{BASE_URL}/transaction/{tx_id}", headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def activate(product_ref: str, value: float, ref: str, currency: str = "") -> dict:
    """הפעלת קוד (transaction). ref ייחודי לכל שורה = אידמפוטנטיות בצד שלנו.
    מחזיר dict בלי לזרוק — הקורא מחליט מה לעשות עם שגיאות (504/507 וכו')."""
    payload = {"amount": {"value": float(value), "currency": currency or CURRENCY},
               "productRef": str(product_ref), "storeRef": STORE_REF, "ref": str(ref)}
    try:
        r = requests.post(f"{BASE_URL}/transaction", headers=_headers(),
                          json=payload, timeout=40)
    except Exception as e:  # noqa: BLE001
        logger.warning("stellr activate %s: network error %s", ref, e)
        return {"ok": False, "http": 0, "error": f"network: {e}"}
    if r.status_code in (200, 201):
        j = r.json()
        # בכוונה בלי PAN/PIN בלוג
        logger.info("stellr activate %s: OK tx=%s status=%s", ref, j.get("id"), j.get("status"))
        return {"ok": True, "http": r.status_code, "tx_id": str(j.get("id") or ""),
                "pan": str(j.get("pan") or ""), "pin": str(j.get("pin") or ""),
                "status": str(j.get("status") or "")}
    err = (r.text or "")[:300]
    logger.warning("stellr activate %s: HTTP %s %s", ref, r.status_code, err)
    return {"ok": False, "http": r.status_code, "error": err}


def status() -> dict:
    return {"enabled": enabled(), "auto": auto_mode(), "base_url": BASE_URL,
            "is_uat": is_uat(), "store_ref": STORE_REF, "currency": CURRENCY,
            "max_value": MAX_VALUE, "max_qty": MAX_QTY, "key_set": bool(API_KEY)}
