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
import re
from datetime import date, datetime

import db


# ── כללי זמינות ברירת-מחדל (הכרעות אסי 09/07/2026; דריסה ידנית תמיד גוברת) ──
# שלב השקה: רק קטגוריית סמארטפונים. הרחבה עתידית (שעונים/טאבלטים) — הוספת מילות מפתח.
ALLOWED_CATEGORY_KEYWORDS = ["סמארטפון", "smartphone"]
FOLDABLE_KEYWORDS = ["fold", "flip", "razr", "מתקפל"]          # מתקפלים — ללא שום חבילה
IPHONE_KEYWORDS = ["iphone", "אייפון"]                          # אייפון — ללא חבילת בסיס
GC_BASIC_PRICE_CAP = 2999                                        # מעל זה — ללא חבילת בסיס


_HE_FINALS = str.maketrans("ךםןףץ", "כמנפצ")


def _norm_he(s: str) -> str:
    """נרמול להשוואה: lowercase + המרת אותיות סופיות לרגילות.
    ("סמארטפון" מסתיים ב-ן' סופית, אבל בתוך "סמארטפונים" זו נ' רגילה — בלי
    הנרמול ההכלה נכשלת בשקט.)"""
    return str(s or "").lower().translate(_HE_FINALS)


def default_availability(name: str, price: float, categories) -> dict:
    """זמינות ברירת-המחדל לפי כללי העסק (כשאין דריסה שמורה). מחזיר:
    {enabled, tier_gc, tier_gcp, reasons:[טקסטים בעברית להצגה בקונסולה]}"""
    nm = _norm_he(name)
    cats = " ".join(_norm_he(c) for c in (categories or []))
    if not any(_norm_he(k) in cats for k in ALLOWED_CATEGORY_KEYWORDS):
        return {"enabled": False, "tier_gc": False, "tier_gcp": False,
                "reasons": ["מחוץ לקטגוריית מכשירים — כבוי בשלב ההשקה"]}
    if any(_norm_he(k) in nm for k in FOLDABLE_KEYWORDS):
        return {"enabled": False, "tier_gc": False, "tier_gcp": False,
                "reasons": ["מכשיר מתקפל — ללא חבילות"]}
    reasons, gc_on = [], True
    if any(_norm_he(k) in nm for k in IPHONE_KEYWORDS):
        gc_on = False
        reasons.append("אייפון — ללא חבילת בסיס")
    elif float(price or 0) > GC_BASIC_PRICE_CAP:
        gc_on = False
        reasons.append("מעל ₪2,999 — ללא חבילת בסיס")
    return {"enabled": True, "tier_gc": gc_on, "tier_gcp": True, "reasons": reasons}


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
def compute_effective(product_price: float, override: dict, defaults: dict = None) -> dict:
    """מאחד את נוסחת ברירת-המחדל עם הדריסה השמורה. מחזיר:
    {enabled, tiers:{gc:{on,price,source}, gcp:{on,price,source}}}
    source='override' אם המחיר נדרס ידנית, אחרת 'formula'.
    defaults (אופציונלי) = default_availability(...) — קובע את מתגי הזמינות כשאין דריסה."""
    ov = override or {}
    df = defaults or {"enabled": True, "tier_gc": True, "tier_gcp": True}
    enabled = bool(int(ov.get("enabled", 1))) if ov else bool(df.get("enabled", True))
    on_gc = bool(int(ov.get("tier_gc", 1))) if ov else bool(df.get("tier_gc", True))
    on_gcp = bool(int(ov.get("tier_gcp", 1))) if ov else bool(df.get("tier_gcp", True))

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


# ══════════════════════════════════════════════════════════════════════
# ניהול שירות פר-לקוח בתוך הזמנה: פוליסה, מימושים, השתתפות עצמית, טרייד-אין
# ══════════════════════════════════════════════════════════════════════

# תוויות המסלולים (לתצוגה בקונסולה).
PLAN_LABELS = {
    "gc":  "Green Care — הרחבת שנה שנייה",
    "gcp": "Green Care Plus — הגנה מלאה 24ח׳",
}
PLAN_SUBTITLES = {
    "gc":  "כיסוי אחריות שנה שנייה (12ח׳ מתום שנת היצרן)",
    "gcp": "הגנה מלאה 24ח׳ מהרכישה — שברים, מסך, אובדן מוחלט",
}

# מגבלות התוכנית (פרמטרים עסקיים — ניתנים לכיוונון).
FREE_COMPONENT_CLAIMS = 4     # שברי רכיבים חינם: מצלמה קדמית/אחורית/גב/שקע טעינה
COMPONENT_PARTS = ["מצלמה קדמית", "מצלמה אחורית", "זכוכית גב", "שקע טעינה"]
SCREEN_MAX = 1                # החלפת מסך פעם אחת (בהשתתפות עצמית)
TOTAL_LOSS_MAX = 1            # אובדן מוחלט פעם אחת (gcp בלבד, שנה שנייה)


# ── חישוב תאריכים ──
def _parse_date(s) -> date:
    """ISO date/datetime → date. ריק/כשל → היום."""
    if not s:
        return date.today()
    txt = str(s).strip()
    try:
        return datetime.fromisoformat(txt.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(txt[:10], "%Y-%m-%d").date()
        except ValueError:
            return date.today()


def _add_months(d: date, months: int) -> date:
    """הוספת חודשים לתאריך בלי תלות ב-dateutil (מטפל בגלישת יום החודש)."""
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    # יום אחרון תקין לחודש היעד (למשל 31/01 + 1ח׳ → 28/02)
    dim = [31, 29 if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0) else 28,
           31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, dim))


def policy_dates(plan: str, purchase_date) -> tuple:
    """(start_iso, end_iso) לפי המסלול.
    gc  → מתחיל בתום שנת היצרן (רכישה+12ח׳), מסתיים רכישה+24ח׳.
    gcp → מתחיל ברכישה, מסתיים רכישה+24ח׳."""
    p = _parse_date(purchase_date)
    if plan == "gcp":
        start, end = p, _add_months(p, 24)
    else:
        start, end = _add_months(p, 12), _add_months(p, 24)
    return start.isoformat(), end.isoformat()


def policy_status_now(policy: dict) -> str:
    """מצב תוקף אפקטיבי: 'cancelled' אם בוטלה; 'pending' אם טרם החל (gc לפני שנה2);
    'expired' אם עבר תום; אחרת 'active'."""
    st = (policy or {}).get("status") or "active"
    if st == "cancelled":
        return "cancelled"
    today = date.today()
    start = _parse_date(policy.get("start_date"))
    end = _parse_date(policy.get("end_date"))
    if today < start:
        return "pending"
    if today > end:
        return "expired"
    return "active"


def in_second_year(policy: dict) -> bool:
    """האם היום נמצא בשנה השנייה של הכיסוי (רלוונטי ל-total_loss ב-gcp).
    ל-gcp שנה2 = מ-start+12ח׳ עד end. ל-gc כל התוקף הוא כבר 'שנה שנייה'."""
    today = date.today()
    end = _parse_date(policy.get("end_date"))
    if (policy or {}).get("plan") == "gcp":
        y2 = _add_months(_parse_date(policy.get("start_date")), 12)
        return y2 <= today <= end
    return _parse_date(policy.get("start_date")) <= today <= end


# ── סיכום שימוש ומגבלות ──
def usage_summary(policy: dict, claims: list) -> dict:
    """מונים + נותרים + דגלים לכל סוג מימוש."""
    claims = claims or []
    comp = sum(1 for c in claims if c.get("claim_type") == "component")
    screen = sum(1 for c in claims if c.get("claim_type") == "screen")
    tl = sum(1 for c in claims if c.get("claim_type") == "total_loss")
    repair = sum(1 for c in claims if c.get("claim_type") == "repair")
    is_gcp = (policy or {}).get("plan") == "gcp"
    tl_allowed = bool(is_gcp and in_second_year(policy) and tl < TOTAL_LOSS_MAX)
    return {
        "component": {"used": comp, "limit": FREE_COMPONENT_CLAIMS,
                      "remaining": max(0, FREE_COMPONENT_CLAIMS - comp)},
        "screen": {"used": screen, "limit": SCREEN_MAX, "used_up": screen >= SCREEN_MAX},
        "total_loss": {"used": tl, "limit": (TOTAL_LOSS_MAX if is_gcp else 0),
                       "used_up": tl >= TOTAL_LOSS_MAX, "allowed": tl_allowed,
                       "plan_ok": is_gcp, "year2": in_second_year(policy)},
        "repair": {"used": repair, "limit": None},   # אחריות רגילה — ללא הגבלה
        "total_claims": len(claims),
    }


def validate_claim(policy: dict, claims: list, claim_type: str) -> tuple:
    """(ok, error_he) — אכיפת מגבלות התוכנית לפני רישום מימוש חדש."""
    u = usage_summary(policy, claims)
    if claim_type == "component":
        if u["component"]["remaining"] <= 0:
            return False, (f"נוצלו כבר כל {FREE_COMPONENT_CLAIMS} מימושי שברי הרכיבים "
                           "החינמיים — לא ניתן לרשום מימוש נוסף.")
    elif claim_type == "screen":
        if u["screen"]["used_up"]:
            return False, "החלפת מסך כבר נוצלה בפוליסה זו (פעם אחת בלבד)."
    elif claim_type == "total_loss":
        if (policy or {}).get("plan") != "gcp":
            return False, "אובדן מוחלט מכוסה רק במסלול Green Care Plus (gcp)."
        if not in_second_year(policy):
            return False, "אובדן מוחלט מכוסה רק בשנה השנייה של הכיסוי."
        if u["total_loss"]["used_up"]:
            return False, "אובדן מוחלט כבר נוצל בפוליסה זו (פעם אחת בלבד)."
    elif claim_type == "repair":
        pass   # אחריות רגילה — ללא הגבלה
    else:
        return False, "סוג מימוש לא מוכר."
    return True, ""


# ── השתתפות עצמית: מסך (50% ממחיר תיקון עדכני) ──
_SCREEN_KEYS = ["מסך", "screen", "display", "מסך + סוללה", "מסך+סוללה"]


def _screen_price_from_repairs(repairs: dict) -> int:
    """מחיר תיקון המסך מתוך מפת התיקונים של דגם (המחיר הזול/מקורי הראשון שנמצא)."""
    for key, tiers in (repairs or {}).items():
        if any(sk in str(key).lower() or sk == str(key) for sk in
               [k.lower() for k in _SCREEN_KEYS]):
            prices = [int(t["price"]) for t in (tiers or []) if t.get("price")]
            if prices:
                return min(prices)
    return 0


def _norm_model(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").lower()).strip()


def _best_repair_match(model_name: str, devices: dict) -> tuple:
    """(matched_key, display) — התאמה חכמה של שם דגם למפתחות המחירון."""
    q = _norm_model(model_name)
    if not q or not devices:
        return "", ""
    if q in devices:
        return q, devices[q].get("display", q)
    # הכלה דו-כיוונית, ואז מירב מילים משותפות
    cands = [k for k in devices if q in k or k in q]
    if not cands:
        qw = set(w for w in q.split() if len(w) > 1)
        scored = []
        for k in devices:
            kw = set(w for w in k.split() if len(w) > 1)
            common = len(qw & kw)
            if common:
                scored.append((common, -abs(len(kw) - len(qw)), k))
        if scored:
            scored.sort(reverse=True)
            best = scored[0][2]
            return best, devices[best].get("display", best)
        return "", ""
    best = max(cands, key=len)   # ההתאמה הספציפית ביותר
    return best, devices[best].get("display", best)


def screen_deductible(model_name: str, pricelist: dict = None) -> dict:
    """השתתפות עצמית להחלפת מסך = 50% ממחיר תיקון המסך העדכני.
    מחזיר {matched, display, repair_price, deductible, models?}.
    אם לא נמצאה התאמה — models = רשימת מפתחות זמינים לבורר ידני."""
    try:
        pl = pricelist if pricelist is not None else __import__("repair_prices").fetch_and_parse()
    except Exception as e:  # noqa: BLE001
        return {"matched": "", "display": "", "repair_price": 0, "deductible": 0,
                "error": str(e)[:120], "models": []}
    devices = (pl or {}).get("devices", {})
    key, disp = _best_repair_match(model_name, devices)
    if not key:
        return {"matched": "", "display": "", "repair_price": 0, "deductible": 0,
                "models": sorted(v.get("display", k) for k, v in devices.items())}
    rp = _screen_price_from_repairs(devices[key].get("repairs", {}))
    return {"matched": key, "display": disp, "repair_price": rp,
            "deductible": int(round(rp * 0.5)), "models": []}


# ── שווי יד-שנייה + השתתפות אובדן מוחלט (gcp, שנה2) ──
_SH_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data", "secondhand_catalog.json")
_SH_CACHE = None


def _load_secondhand() -> dict:
    global _SH_CACHE
    if _SH_CACHE is None:
        try:
            with open(_SH_PATH, encoding="utf-8") as f:
                _SH_CACHE = json.load(f)
        except Exception:  # noqa: BLE001
            _SH_CACHE = {}
    return _SH_CACHE


def _match_brand(brand: str, catalog: dict) -> str:
    b = _norm_model(brand)
    for k in catalog:
        if _norm_model(k) == b or (b and (b in _norm_model(k) or _norm_model(k) in b)):
            return k
    return ""


def _match_model(model: str, models: dict) -> str:
    q = _norm_model(model)
    if not q:
        return ""
    for k in models:
        if _norm_model(k) == q:
            return k
    cands = [k for k in models if q in _norm_model(k) or _norm_model(k) in q]
    if cands:
        return max(cands, key=lambda k: len(_norm_model(k)))
    qw = set(w for w in q.split() if len(w) > 1)
    scored = []
    for k in models:
        kw = set(w for w in _norm_model(k).split() if len(w) > 1)
        common = len(qw & kw)
        if common:
            scored.append((common, -abs(len(kw) - len(qw)), k))
    if scored:
        scored.sort(reverse=True)
        return scored[0][2]
    return ""


def tl_valuation(brand: str, model: str, storage: str = "") -> dict:
    """שווי יד-שנייה עדכני (אינדקס 1 = 'תקין'). מחזיר
    {matched, brand, model, storage, value, storages?, brands?, models?}."""
    cat = _load_secondhand()
    if not cat:
        return {"matched": False, "value": 0, "brands": [], "models": [], "storages": []}
    bk = _match_brand(brand, cat)
    if not bk:
        return {"matched": False, "value": 0, "brands": sorted(cat.keys()),
                "models": [], "storages": []}
    models = cat[bk]
    mk = _match_model(model, models)
    if not mk:
        return {"matched": False, "brand": bk, "value": 0,
                "brands": sorted(cat.keys()), "models": sorted(models.keys()),
                "storages": []}
    storages = models[mk]
    sk = ""
    q = _norm_model(storage)
    if q:
        for k in storages:
            if _norm_model(k) == q or q in _norm_model(k):
                sk = k
                break
    if not sk:   # ברירת מחדל = הנפח הזול ביותר (שמרני)
        sk = min(storages.keys(),
                 key=lambda k: (storages[k][1] if len(storages[k]) > 1 else 0))
    vals = storages.get(sk) or []
    value = int(vals[1]) if len(vals) > 1 else (int(vals[0]) if vals else 0)
    return {"matched": True, "brand": bk, "model": mk, "storage": sk,
            "value": value, "brands": sorted(cat.keys()),
            "models": sorted(models.keys()), "storages": sorted(storages.keys())}


def default_tl_deductible(product_price: float) -> int:
    """הצעת השתתפות עצמית לאובדן מוחלט (פרמטר עסקי — ניתן לכיוונון):
    ~10% ממחיר המכשיר, מעוגל ל-₪10, מוצמד ל-[₪149, ₪499]."""
    p = float(product_price or 0)
    rounded = round((p * 0.10) / 10.0) * 10
    return int(max(149, min(499, rounded)))
