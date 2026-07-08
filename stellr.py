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
  STELLR_RELAY_URL  relay ב-PHP על שרת ה-WP (IP יוצא קבוע ל-allowlist של Stellr)
  STELLR_RELAY_KEY  סוד אימות ל-relay (header X-GM-Relay-Key)

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

# Relay IP-סטטי — פרודקשן של Stellr נעול ל-allowlist לפי IP, ו-Render יוצא מטווחים
# משותפים מסתובבים. STELLR_RELAY_URL מפנה קריאות Stellr דרך relay ב-PHP על שרת ה-WP
# (IP יוצא קבוע 185.60.168.165 שנותנים לסטלר). ריק = קריאה ישירה (UAT/מקומי).
STELLR_RELAY_URL = os.environ.get("STELLR_RELAY_URL", "").strip()
STELLR_RELAY_KEY = os.environ.get("STELLR_RELAY_KEY", "").strip()


class _Resp:
    """עוטף תשובת relay ({http, body}) כך שתיראה כמו requests.Response לקוראים."""
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text or ""

    def json(self):
        import json as _json
        return _json.loads(self.text) if self.text else None

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}: {self.text[:200]}")


def _request(method: str, path: str, payload=None, timeout: int = 40):
    """קריאה ל-Stellr — דרך relay (אם מוגדר) או ישירות. מחזיר אובייקט תשובה אחיד."""
    if STELLR_RELAY_URL:
        body = {"method": method, "path": path, "api_key": API_KEY}
        if payload is not None:
            body["payload"] = payload
        rr = requests.post(STELLR_RELAY_URL, json=body, timeout=timeout + 20,
                           headers={"X-GM-Relay-Key": STELLR_RELAY_KEY,
                                    "Content-Type": "application/json"})
        rr.raise_for_status()   # תקלה בשכבת ה-relay עצמה (WP/Cloudflare)
        j = rr.json() or {}
        if int(j.get("http") or 0) == 0:
            raise requests.ConnectionError(f"relay→stellr: {j.get('error')}")
        return _Resp(int(j.get("http")), j.get("body") or "")
    url = f"{BASE_URL}{path}"
    if method == "POST":
        return requests.post(url, headers=_headers(), json=payload, timeout=timeout)
    return requests.get(url, headers=_headers(), timeout=timeout)


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
    r = _request("GET", "/product", timeout=30)
    r.raise_for_status()
    data = r.json()
    _cat_cache["data"] = data
    _cat_cache["at"] = time.time()
    return data


def get_transaction(tx_id: str) -> dict:
    r = _request("GET", f"/transaction/{tx_id}", timeout=30)
    r.raise_for_status()
    return r.json()


def activate(product_ref: str, value: float, ref: str, currency: str = "") -> dict:
    """הפעלת קוד (transaction). ref ייחודי לכל שורה = אידמפוטנטיות בצד שלנו.
    מחזיר dict בלי לזרוק — הקורא מחליט מה לעשות עם שגיאות (504/507 וכו')."""
    payload = {"amount": {"value": float(value), "currency": currency or CURRENCY},
               "productRef": str(product_ref), "storeRef": STORE_REF, "ref": str(ref)}
    try:
        r = _request("POST", "/transaction", payload=payload, timeout=40)
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
            "max_value": MAX_VALUE, "max_qty": MAX_QTY, "key_set": bool(API_KEY),
            "relay": bool(STELLR_RELAY_URL)}
