"""
zap_scan.py — סריקה יומית של זאפ: איפה כל סמארטפון שלנו עומד מול המתחרים.

המטרה (אסי, 26/07/2026): כלי מעקב יומי שמציף **איפה שווה לנו להיות תחרותיים**.
⚠️ זאפ אינו ערוץ שרוצים להיות בו על כל דגם — התחרות שם צפופה והמרווח נשחק,
ובחלק גדול מהדגמים מעולם לא היינו רשומים, במכוון. לכן "לא רשומים" הוא
**החלטה עסקית ולא תקלה**, ואין להסיק ממנו על ירידה במכירות.

הדוח עונה על שתי שאלות:
  1. איפה אנחנו כן רשומים — מיקום בפועל, פער מהזול.
  2. איפה איננו רשומים — ובאיזה מחיר היינו נכנסים לטופ, אילו היינו שם.
    "לרדת ל-X ⇒ לעלות למקום Y" הוא הפלט המרכזי: כלי החלטה, לא דוח.

מקור: זאפ מטמיע ב-model.aspx בלוק schema.org עם offerCount / lowPrice / highPrice
ורשימת offers מלאה (price + seller). אין צורך בדפדפן — בניגוד ל-KSP, זאפ נענה
ל-urllib עם User-Agent רגיל (אומת 26/07/2026).

היקף: כל סמארטפון עם מלאי בקופה (קטגוריה 3) — ~252 דגמים, כולל כל ליין
Trinity הסיני (Oppo / Honor / Huawei / Vivo / Xiaomi / Realme / ZTE).
מיפוי דגם→modelid נשמר ב-sales_state ולכן החיפוש רץ פעם אחת לדגם.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta

import db

logger = logging.getLogger("transfers.zap")

BASE = "https://www.zap.co.il"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
HDRS = {"User-Agent": UA, "Accept-Language": "he-IL,he;q=0.9"}
OURS = ("גרין מובייל", "greenmobile", "green mobile", "גרין-מובייל")

BRANDS = (("iphone", "Apple"), ("galaxy", "Samsung"), ("samsung", "Samsung"),
          ("oppo", "Oppo"), ("honor", "Honor"), ("huawei", "Huawei"), ("vivo", "Vivo"),
          ("redmi", "Xiaomi"), ("poco", "Xiaomi"), ("xiaomi", "Xiaomi"),
          ("pixel", "Google"), ("oneplus", "OnePlus"), ("razr", "Motorola"),
          ("motorola", "Motorola"), ("nothing", "Nothing"), ("realme", "Realme"),
          ("redmagic", "RedMagic"), ("nubia", "ZTE"), ("zte", "ZTE"), ("nokia", "Nokia"))


def brand_of(name: str) -> str:
    n = (name or "").lower()
    for k, lab in BRANDS:
        if k in n:
            return lab
    return "אחר"


def _get(url: str, timeout: int = 30) -> str:
    with urllib.request.urlopen(urllib.request.Request(url, headers=HDRS), timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# ─────────────────────────── מה סורקים ───────────────────────────
def _pos_stock() -> dict:
    """מק"ט → מלאי בקופה. נכשל בשקט: מלאי הוא חיווי, לא תנאי לסריקה."""
    try:
        import poller
        return {str(p["id"]): (p.get("currentStock") or 0)
                for p in poller.client().get_all_products(category=3)
                if p.get("isActive")}
    except Exception as e:  # noqa: BLE001
        logger.warning("zap: POS stock failed: %s", e)
        return {}


def _feed_skus(pids: list) -> dict:
    """מזהה מוצר → כל המק"טים שמתחתיו, כדי לצרף מלאי קופה.
    ⚠️ ל-45 מתוך 65 רשומות הפיד אין CATALOG_NUMBER (המק"ט על הוריאציה),
    ולכן עמודת המלאי הראתה 0 לכל שורה (אסי, 27/07/2026). למוצר צל אין מלאי
    משלו — לוקחים את הוריאציות של ההורה שתואמות לנפח שהצל מייצג."""
    import requests
    base = os.getenv("WC_STORE_URL", "https://greenmobile.co.il").rstrip("/")
    auth = (os.getenv("WC_CONSUMER_KEY", ""), os.getenv("WC_CONSUMER_SECRET", ""))
    import zap_price
    info, out, caps_by = {}, {}, {}
    for i in range(0, len(pids), 50):
        try:
            r = requests.get(f"{base}/wp-json/wc/v3/products", auth=auth, timeout=60,
                             params={"include": ",".join(pids[i:i + 50]), "per_page": 100,
                                     "_fields": "id,sku,type,external_url,meta_data"})
            for p in (r.json() if r.ok else []):
                info[str(p["id"])] = p
        except Exception as e:  # noqa: BLE001
            logger.warning("zap: sku batch failed: %s", e)

    _vcache = {}

    def variations(par, cap=None):
        if par not in _vcache:
            try:
                v = requests.get(f"{base}/wp-json/wc/v3/products/{par}/variations", auth=auth,
                                 timeout=45, params={"per_page": 100,
                                                     "_fields": "sku,attributes"})
                _vcache[par] = v.json() if v.ok else []
            except Exception:  # noqa: BLE001
                _vcache[par] = []
        rows = _vcache[par]
        if cap:
            rows = [x for x in rows if zap_price._cap_from_attrs(x.get("attributes")) == cap]
        return [str(x["sku"]) for x in rows if x.get("sku")]

    def caps(par):
        variations(par)
        cs = {zap_price._cap_from_attrs(x.get("attributes")) for x in _vcache.get(par) or []}
        return sorted(c for c in cs if c)

    for pid in pids:
        p = info.get(pid)
        if not p:
            continue
        if p.get("type") == "variable":
            caps_by[pid] = caps(pid)
        if p.get("type") == "external":
            meta = {m["key"]: str(m["value"]) for m in (p.get("meta_data") or [])}
            par = meta.get("gm_parent_product_id")
            if par:
                out[pid] = variations(par, zap_price._cap_slug(p.get("external_url") or ""))
        elif p.get("type") == "variable":
            out[pid] = variations(pid)
        elif p.get("sku"):
            out[pid] = [str(p["sku"])]
    return {"skus": out, "caps": caps_by}


def _catalog(cat: int = 1934) -> list:
    """כל הסמארטפונים המפורסמים באתר — **גם** אלה שמוסתרים מזאפ.
    ⚠️ בלי זה הכלי היה מעגל סגור: ברגע שמסתירים מוצר הוא נופל מהפיד, נעלם
    מהמסך, ואין דרך להחזיר אותו (אסי, 27/07/2026). 109 מוצרים בקטלוג מול 64
    בפיד — 41 מוסתרים, וביניהם iPhone 17 Pro Max ו-Pixel 10 Pro Fold."""
    import requests
    base = os.getenv("WC_STORE_URL", "https://greenmobile.co.il").rstrip("/")
    auth = (os.getenv("WC_CONSUMER_KEY", ""), os.getenv("WC_CONSUMER_SECRET", ""))
    out, page = [], 1
    while page <= 6:
        try:
            r = requests.get(f"{base}/wp-json/wc/v3/products", auth=auth, timeout=60,
                             params={"category": cat, "per_page": 100, "page": page,
                                     "status": "publish", "_fields":
                                     "id,name,type,price,permalink,meta_data"})
            rows = r.json() if r.ok else []
        except Exception as e:  # noqa: BLE001
            logger.warning("zap: catalog page %s failed: %s", page, e)
            break
        if not isinstance(rows, list) or not rows:
            break
        out += rows
        page += 1
    return out


def build_targets() -> list:
    """היקף הכלי: **כל הסמארטפונים בקטלוג**, לא רק מה שמשודר כרגע.
    הפיד קובע מה זאפ באמת מקבל, אבל מוצר שמוסתר חייב להישאר על המסך כדי
    שאפשר יהיה להחזיר אותו או להחליט עליו. לכל שורה נקבעת `reason` —
    למה היא לא מחוברת לדגם בזאפ — כי בלי זה אי אפשר לפעול."""
    import zap_price
    try:
        feed = zap_price.feed(1934)
    except Exception as e:  # noqa: BLE001
        logger.warning("zap: feed failed: %s", e)
        feed = []
    catalog = _catalog(1934)
    if not feed and not catalog:
        return []

    shadows = {}
    try:
        shadows = zap_price.shadow_map()
    except Exception as e:  # noqa: BLE001
        logger.warning("zap: shadow_map failed: %s", e)

    feed_by_pid, shadow_ids = {}, {sh["id"] for v in shadows.values() for sh in v}
    for f in feed:
        pid = str(f.get("num") or "").strip()
        if pid:
            feed_by_pid[pid] = f
    # הורה שהצללים שלו בפיד מיוצג על ידם — לא כופלים אותו כשורה נפרדת
    parents_via_shadow = {par for par, shs in shadows.items()
                          if any(str(sh["id"]) in feed_by_pid for sh in shs)}

    stock = _pos_stock()
    pids = list(dict.fromkeys(list(feed_by_pid) + [str(p["id"]) for p in catalog]))
    _m = _feed_skus(pids)
    skumap, capmap = _m["skus"], _m["caps"]
    meta_by_pid = {str(p["id"]): p for p in catalog}

    out = []
    for pid in pids:
        if pid in parents_via_shadow:
            continue
        f = feed_by_pid.get(pid)
        c = meta_by_pid.get(pid) or {}
        name = (f or {}).get("name") or c.get("name") or ""
        if not name:
            continue
        meta = {m["key"]: str(m["value"]) for m in (c.get("meta_data") or [])}
        hidden = 1 if meta.get("_woocommerce_zap_disable") == "yes" else 0
        try:
            price = float((f or {}).get("price") or c.get("price") or 0)
        except (TypeError, ValueError):
            price = 0
        skus = skumap.get(pid) or ([str((f or {}).get("sku") or "")] if (f or {}).get("sku") else [])
        caps = capmap.get(pid) or []
        out.append({
            "id": pid, "sku": (f or {}).get("sku") or (skus[0] if skus else ""),
            "name": name, "brand": brand_of(name),
            "stock": sum(stock.get(k, 0) for k in skus),
            "our_price": price or None, "product_id": pid,
            "site_url": (f or {}).get("url") or c.get("permalink"),
            "in_feed": 1 if f else 0, "zap_hidden": hidden,
            "is_shadow": 1 if int(pid) in shadow_ids else 0,
            "shadows": len(shadows.get(pid) or []), "caps": caps,
        })
    return out


def _reason(row: dict) -> tuple:
    """(קוד, טקסט) — למה השורה אינה מחוברת לדגם בזאפ. זה הפלט שמאפשר לפעול."""
    if row.get("status") == "listed":
        return ("listed", "רשומים בדף ההשוואה")
    if row.get("zap_hidden"):
        n = row.get("shadows") or 0
        return (("hidden_shadows", f"מוסתר בכוונה · {n} מוצרי צל משודרים במקומו")
                if n else ("hidden", "מוסתר מזאפ — לא משודר כלל"))
    if not row.get("in_feed"):
        return ("off_feed", "לא נכלל בפיד (בדוק מלאי/מחיר/קטגוריה)")
    caps = row.get("caps") or []
    if not row.get("modelid") and len(caps) > 1:
        return ("needs_shadow",
                f"{len(caps)} נפחים בכותרת אחת — זאפ דורש נפח (ורצוי RAM) בכותרת")
    if not row.get("modelid"):
        return ("no_model", "זאפ לא מכיר דגם תואם לכותרת הזו")
    return ("missing", "יש דף השוואה — איננו רשומים עליו")


# ─────────────────────────── זאפ ───────────────────────────
CAP_RE = re.compile(r"(\d+)\s*(TB|GB)\b", re.I)
# פריטים שיושבים בקטגוריית "טלפונים/סלולרי" בקופה אך אינם סמארטפון —
# אביזרים, טאבלטים ושעונים. בלי הסינון הם נכנסים לדוח ומתמפים לדגם אקראי.
ACCESSORY = ("case", "cover", "כיסוי", "מגן", "נרתיק", "sandstone", "silicone",
             "כבל", "מטען", "אוזני", "סוללה", "מעמד", "זכוכית", "headset", "headphone",
             "earbud", "buds", "airpod", "charger", "cable", "adapter", "מתאם",
             "ipad", "tab ", "tablet", "טאבלט", "watch", "שעון", "band", "כרטיס")


# תוספות דגם שמבדילות בין גרסאות — חייבות להיות זהות בשני הצדדים.
# ⚠️ הסדר חשוב: "pro max" נבדק לפני "pro", אחרת Pro Max יזוהה כ-Pro.
QUALIFIERS = (("pro max", "promax"), ("pro plus", "proplus"), ("pro+", "proplus"),
              ("pro", "pro"),
              ("plus", "plus"), ("ultra", "ultra"), ("mini", "mini"),
              ("air", "air"), ("lite", "lite"), ("fe", "fe"), ("edge", "edge"))


def _capacity(s: str) -> str | None:
    """נפח האחסון, מנורמל ל-GB. ⚠️ בכותרות זאפ מופיע גם ה-RAM ("12GB+256GB"),
    ולכן לוקחים את **הגדול** מבין הערכים — האחסון תמיד ≥ הזיכרון.
    ⚠️ וכשבשם אין אחסון בכלל ("Samsung Galaxy S26 12GB Ram") ה-RAM היה נקרא
    כאחסון והושווה ל-256GB של זאפ — הדף הנכון נדחה. אין היום סמארטפון עם
    פחות מ-64GB אחסון, ולכן ערך קטן מזה הוא זיכרון ולא נפח (27/07/2026)."""
    vals = [int(n) * (1024 if u.upper() == "TB" else 1)
            for n, u in CAP_RE.findall(s or "")]
    vals = [v for v in vals if v >= 64]
    return str(max(vals)) if vals else None


# ⚠️ \b חובה: בלעדיו "512GB RAM" מחזיר 12 (שתי הספרות האחרונות) ושובר את ההתאמה
RAM_RE = re.compile(r"\b(\d{1,2})\s*GB\s*RAM", re.I)


def _ram(s: str) -> str | None:
    """זאפ מחזיק דף נפרד לכל RAM ("512GB 12GB RAM" מול "512GB 16GB RAM"),
    ולכן בלי בדיקת RAM ההתאמה נופלת על הדף השכן (אסי, 26/07/2026)."""
    m = RAM_RE.search(s or "")
    return m.group(1) if m else None


def _quals(s: str) -> set:
    low = " " + re.sub(r"[^a-z0-9+ ]", " ", (s or "").lower()) + " "
    out, seen = set(), low
    for pat, tag in QUALIFIERS:
        if f" {pat} " in seen:
            out.add(tag)
            seen = seen.replace(f" {pat} ", " ")   # שלא ייספר שוב כחלק קצר יותר
    return out


# מספרי מפרט שיווקי — גודל מסך, סוללה, מגה-פיקסל, רענון, הספק טעינה.
# ⚠️ בלי הניקוי הזה "מסך 8.1״" תרם אסימוני דגם מדומים 8 ו-1, ומכיוון שבכותרת
# של זאפ ("Motorola Razr Fold 512GB 16GB RAM") אין מספרים כאלה, החיתוך יצא
# ריק והדגם נדחה — למרות שהכותרות זהות (אסי, 26/07/2026).
SPEC_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:[\"״”'׳″′’]|mah|mp|hz|w\b|inch|אינץ)", re.I)


def _model_tokens(s: str) -> set:
    """אסימוני דגם: מספרים ושילובי אות+מספר (17, A57, X9, V6, Note)."""
    low = (s or "").lower()
    low = CAP_RE.sub(" ", low)                       # הנפח נבדק בנפרד
    low = SPEC_RE.sub(" ", low)                      # וגם מפרט שיווקי אינו דגם
    toks = set(re.findall(r"\b([a-z]{0,2}\d{1,4}[a-z]?)\b", low))
    return {t for t in toks if not re.fullmatch(r"\d{4,}", t)}   # לא מק"טים


def _match_ok(our_name: str, zap_title: str) -> bool:
    """האם דף המודל בזאפ באמת מתאר את המוצר שלנו.
    ⚠️ בלי זה החיפוש מחזיר את התוצאה הראשונה בעמוד גם כשהיא דגם אחר לגמרי —
    כך 'Redmi A7 Pro' ו-'S26 Ultra' מופו בטעות ל-iPhone 17 (26/07/2026)."""
    if not zap_title:
        return False
    if brand_of(our_name) != brand_of(zap_title):
        return False
    ca, cb = _capacity(our_name), _capacity(zap_title)
    if ca and cb and ca != cb:
        return False
    if ca and not cb:
        return False
    if _quals(our_name) != _quals(zap_title):   # Pro / Pro Max / Ultra / FE חייבים להתאים
        return False
    ra, rb = _ram(our_name), _ram(zap_title)
    if ra and rb and ra != rb:
        return False
    ta, tb = _model_tokens(our_name), _model_tokens(zap_title)
    # דורשים חיתוך רק כששני הצדדים נושאים אסימוני דגם. כשלכותרת של זאפ אין
    # מספר דגם כלל ("Motorola Razr Fold") היעדר חיתוך אינו ראיה נגד — המותג,
    # הנפח וה-RAM כבר שמרו על ההתאמה.
    return bool(ta & tb) if (ta and tb) else True


HEB_RE = re.compile(r"[֐-׿]+")


def _clean_query(name: str) -> str:
    """שם המוצר בלי הצבע. ⚠️ רשימת צבעים קשיחה לא מספיקה — "Umber", "Moonstone"
    ו-"Graygreen" אינם בה, נשארו בשאילתה, וזאפ החזיר תוצאות אקראיות לגמרי.
    הכלל המבני: בשמות שלנו הצבע הוא תמיד הקטע האחרון אחרי " - " ואין בו נפח.
    שמות הפיד עברית-מובילה ("טלפון סלולרי OPPO Find X9 Ultra 1TB 16GB RAM"),
    והמילים העבריות — סוג המוצר, הצבע, "מציאון" — הן רעש שמדרדר את החיפוש;
    הליבה הלטינית+מספרית היא מה שזאפ מזהה."""
    q = re.sub(r"\s+", " ", (name or "")).strip()
    if " - " in q:
        # ⚠️ מי משני הצדדים נושא את הדגם? המבחן הוא **המותג**, לא "יש כאן
        # לטינית": בשם "סמארטפון עם מסך Super AMOLED... - Samsung Galaxy A07"
        # גם הראש לטיני, וקיצוץ הזנב הותיר את השאילתה "Super AMOLED"
        # (27/07/2026). הצד עם המותג הוא הצד עם הדגם.
        head, tail = q.rsplit(" - ", 1)
        hb, tb = brand_of(head), brand_of(tail)
        if hb != "אחר" and tb == "אחר" and not CAP_RE.search(tail):
            q = head          # הזנב הוא צבע / "מציאון"
        elif tb != "אחר" and hb == "אחר":
            q = tail          # הראש הוא טקסט שיווקי
    # ⚠️ ניקוי המפרט חייב לקרות **לפני** הסרת העברית: הגרשיים של גודל המסך
    # (״, U+05F4) הוא תו עברי, ולכן "מסך 6.59״" הפך ל-"6.59" חשוף שנקרא
    # כמספר דגם והרס גם את השאילתה וגם את ההתאמה (27/07/2026).
    q = SPEC_RE.sub(" ", q)
    core = re.sub(r"\s+", " ", HEB_RE.sub(" ", q)).strip(" -,")
    return core or q.strip(" -")


def _short_query(q: str) -> str:
    """נפילה אחורה: מותג + אסימוני דגם + נפח בלבד, בלי מילות תיאור."""
    b = brand_of(q)
    if b == "אחר":
        return ""
    toks = sorted(_model_tokens(q))
    cap = CAP_RE.search(q)
    parts = [b] + toks + ([cap.group(0)] if cap else [])
    out = " ".join(parts)
    return out if out.strip().lower() != q.strip().lower() else ""


def _model_title(mid: int) -> str:
    try:
        html = _get(f"{BASE}/model.aspx?modelid={mid}")
    except Exception:  # noqa: BLE001
        return ""
    m = re.search(r"<title>(.*?)</title>", html, re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


PHONE_LEAD = ("סמארטפון", "טלפון סלולרי", "טלפון חכם", "smartphone", "phone",
              "טלפון", "מכשיר סלולרי")


def _is_accessory(name: str) -> bool:
    """⚠️ המסנן נועד לקטלוג הקופה, שבו אביזרים יושבים בקטגוריית טלפונים.
    מול הפיד הוא הזיק: שמות שיווקיים מתארים תכונות ("וסוללה 6000mAh",
    "סוללה עוצמתית"), והמילה "סוללה" פסלה את שתי עמדות המלאי הגדולות שלנו —
    Redmi A7 Pro (92 יח׳) ו-Galaxy A07 (41 יח׳) — ששתיהן קיימות בזאפ
    (אסי, 27/07/2026). שם שנפתח ב"סמארטפון"/"טלפון סלולרי" הוא טלפון, נקודה."""
    low = (name or "").strip().lower()
    if any(low.startswith(p) for p in PHONE_LEAD):
        return False
    return any(a in low for a in ACCESSORY)


def resolve_modelid(name: str, sku: str) -> int | None:
    """modelid של הדגם בזאפ, **מאומת מול שם המוצר**. נשמר לצמיתות.
    '0' = חיפשנו ולא נמצאה התאמה תקפה, כדי לא לחפש שוב כל יום.
    ⚠️ `sku` כאן הוא מזהה-הזהות של השורה, ולא בהכרח מק"ט: ל-45 מתוך 65
    מוצרי הפיד אין CATALOG_NUMBER (המק"ט יושב על הוריאציה, לא על ההורה),
    ואז כולם חלקו את המפתח הריק `zap_mid:` — הראשון שנפתר קבע, ו-45 דגמים
    שונים מופו לאותו modelid (אסי, 26/07/2026). בלי מזהה — לא ממטמנים."""
    if not (sku or "").strip():
        logger.warning("zap: refusing to cache modelid without an id (%s)", name[:60])
        return None
    key = f"zap_mid:{sku}"
    cached = db.sales_state_get(key)
    if cached:                            # '' = אופס ידנית → מחשבים מחדש
        return int(cached) or None
    if _is_accessory(name):
        db.sales_state_set(key, "0")      # אביזר שסווג בטעות כטלפון בקופה
        return None
    q = _clean_query(name)
    mid = 0
    # שתי שאילתות: המלאה, ואם נכשלה — מקוצרת (מותג + אסימוני דגם + נפח). זאפ
    # מדרדר לתוצאות אקראיות כשיש בשאילתה מילה שהוא לא מכיר, ואז המועמדים
    # חסרי קשר לגמרי (Oppo Find X9 → אייפון, מקבוק, מקרר).
    for attempt in [a for a in (q, _short_query(q)) if a]:
        try:
            html = _get(f"{BASE}/search.aspx?keyword={urllib.parse.quote(attempt)}")
        except Exception as e:  # noqa: BLE001
            logger.warning("zap search failed for %s: %s", attempt, e)
            return None                   # שגיאת רשת — לא נועלים מטמון
        for cand in list(dict.fromkeys(re.findall(r"modelid=(\d+)", html)))[:6]:
            title = _model_title(int(cand))
            if _match_ok(q, title):
                mid = int(cand)
                db.sales_state_set(f"zap_title:{sku}", title[:160])
                break
            time.sleep(0.35)
        if mid:
            break
    if not mid:
        logger.info("zap: no valid match for %s", q)
    db.sales_state_set(key, str(mid))
    return mid or None


def fetch_model(modelid: int) -> dict | None:
    """כל ההצעות על דף מודל, ממוין מהזול."""
    try:
        html = _get(f"{BASE}/model.aspx?modelid={modelid}")
    except Exception as e:  # noqa: BLE001
        logger.warning("zap model %s failed: %s", modelid, e)
        return None
    i = html.find('"offerCount"')
    if i < 0:
        return None
    blob = html[i:html.find("]", html.find('"offers"', i)) + 1]
    seen, offers = set(), []
    for p, s in re.findall(r'"price":\s*"([\d.]+)".*?"name":\s*"([^"]+)"', blob, re.S):
        key = (round(float(p), 2), s.strip())
        if key in seen:
            continue
        seen.add(key)
        offers.append({"price": round(float(p), 2), "seller": s.strip()})
    offers.sort(key=lambda x: x["price"])
    cnt = re.search(r'"offerCount":\s*"(\d+)"', html)
    low = (html.lower())
    return {"modelid": modelid, "offers": offers,
            "offer_count": int(cnt.group(1)) if cnt else len(offers),
            "we_listed": any(k in low for k in ("גרין מובייל", "greenmobile", "green mobile"))}


def _price_for_rank(offers: list, rank: int) -> float | None:
    """המחיר שצריך כדי להיות במקום `rank` (שקל מתחת למי שתופס אותו עכשיו)."""
    if len(offers) < rank:
        return None
    return round(offers[rank - 1]["price"] - 1, 0)


def analyse(t: dict) -> dict:
    ident = t.get("id") or t.get("sku")
    mid = resolve_modelid(t["name"], ident)
    row = {"id": ident, "sku": t["sku"], "name": t["name"], "brand": t["brand"],
           "stock": t["stock"], "our_price": t.get("our_price"), "modelid": mid,
           "product_id": t.get("product_id"), "site_url": t.get("site_url"),
           "in_feed": t.get("in_feed", 1), "zap_hidden": t.get("zap_hidden", 0),
           "is_shadow": t.get("is_shadow", 0), "shadows": t.get("shadows", 0),
           "caps": t.get("caps") or []}
    if not mid:
        row["status"] = "no_model"        # אין דגם תואם בזאפ
        return row
    data = fetch_model(mid)
    if not data or not data["offers"]:
        row["status"] = "no_offers"
        return row
    offers = data["offers"]
    ours = next((o for o in offers if any(k in o["seller"].lower() for k in OURS)), None)
    prices = [o["price"] for o in offers]
    # רשת ביטחון: אם המחיר שלנו רחוק מסדר-הגודל של השוק לדגם הזה, כמעט בוודאות
    # ההתאמה שגויה (כך Redmi A7 ב-₪499 הושווה לרשימת iPhone 17). עדיף לסמן
    # "חשוד" מאשר להציג מיקום שקרי שעליו מתקבלת החלטת תמחור.
    op = t.get("our_price")
    if op and not ours and (op < prices[0] / 2.5 or op > prices[-1] * 2.5):
        row.update({"status": "suspect", "url": f"{BASE}/model.aspx?modelid={mid}",
                    "zap_title": db.sales_state_get(f"zap_title:{ident}") or "",
                    "sellers": data["offer_count"], "low": prices[0], "high": prices[-1],
                    "note": "המחיר שלנו רחוק מטווח הדגם בזאפ — ההתאמה כנראה שגויה"})
        logger.warning("zap: suspect match sku=%s ours=%s range=%s-%s",
                       t["sku"], op, prices[0], prices[-1])
        return row
    row.update({
        "url": f"{BASE}/model.aspx?modelid={mid}",
        "zap_title": db.sales_state_get(f"zap_title:{ident}") or "",
        "sellers": data["offer_count"],
        "low": prices[0], "low_seller": offers[0]["seller"],
        "median": prices[len(prices) // 2], "high": prices[-1],
        # רשימת המתחרים המלאה — לתצוגת ההרחבה בדוח (מי, באיזה מקום, באיזה מחיר)
        "competitors": [{"rank": i + 1, "seller": o["seller"], "price": o["price"]}
                        for i, o in enumerate(offers[:25])],
        "listed": bool(ours) or data["we_listed"],
        "status": "listed" if (ours or data["we_listed"]) else "missing",
        # כמה צריך לגבות כדי להגיע למקום 1 / 3 / 5 — זה כלי ההחלטה
        "p_top1": _price_for_rank(offers, 1),
        "p_top3": _price_for_rank(offers, 3),
        "p_top5": _price_for_rank(offers, 5),
    })
    if ours:
        row["our_price"] = ours["price"]
        row["rank"] = offers.index(ours) + 1
    elif row.get("our_price"):
        row["rank"] = sum(1 for p in prices if p < row["our_price"]) + 1
    if row.get("our_price"):
        row["gap_to_low"] = round(row["our_price"] - row["low"], 2)
        row["gap_pct"] = round((row["our_price"] / row["low"] - 1) * 100, 1)
        for k in (1, 3, 5):
            p = row.get(f"p_top{k}")
            row[f"cut_top{k}"] = round(max(0.0, row["our_price"] - p), 0) if p else None
    return row


def _summarise(rows: list, partial: bool = False) -> dict:
    ranked = [r for r in rows if r.get("rank")]
    listed = [r for r in rows if r.get("status") == "listed"]
    missing = [r for r in rows if r.get("status") == "missing"]
    gaps = [r["gap_pct"] for r in rows if r.get("gap_pct") is not None]
    return {
        "scanned": len(rows),
        "on_zap": len(listed) + len(missing),          # יש דף מודל מאומת בזאפ
        "listed": len(listed),
        "missing": len(missing),                       # יש דגם — ואנחנו לא בו
        "no_model": sum(1 for r in rows if r.get("status") == "no_model"),
        "catalog": len(rows),
        "in_feed": sum(1 for r in rows if r.get("in_feed")),
        "hidden": sum(1 for r in rows if r.get("zap_hidden")),
        "needs_shadow": sum(1 for r in rows if r.get("reason_code") == "needs_shadow"),
        "off_feed": sum(1 for r in rows if r.get("reason_code") == "off_feed"),
        "suspect": sum(1 for r in rows if r.get("status") == "suspect"),
        "in_top5": sum(1 for r in ranked if r["rank"] <= 5),
        "in_top3": sum(1 for r in ranked if r["rank"] <= 3),
        "median_rank": sorted(r["rank"] for r in ranked)[len(ranked) // 2] if ranked else None,
        "avg_gap_pct": round(sum(gaps) / len(gaps), 1) if gaps else None,
        "partial": partial,
    }


def reset_mapping() -> int:
    """מנקה את מטמון דגם→modelid. נדרש אחרי שינוי בלוגיקת ההתאמה — אחרת
    מיפויים שנוצרו בגרסה קודמת נשארים תקועים לנצח (המטמון לצמיתות בכוונה)."""
    n = 0
    for pref in ("zap_mid:", "zap_title:"):
        for k, _ in db.sales_state_prefix(pref):
            db.sales_state_set(k, "")     # ריק ≠ '0' → ייחשב כלא-נבדק ויחושב מחדש
            n += 1
    return n


def run(limit: int | None = None, sleep: float = 1.1) -> dict:
    """סבב מלא. ⚠️ שורה = **דגם בזאפ**, לא מק"ט: דף השוואה אחד מייצג את כל
    הצבעים של אותה תצורה, ולכן המלאי מצטבר (אסי: "Oppo X9 Ultra 512 —
    2 שחור + 2 כתום ⇒ 4 במלאי"), וכך אפשר להחליט מהר אם שווה לחתוך מחיר."""
    targets = build_targets()
    # ⚠️ סריקה בלי יעדים לא כותבת תמונת מצב. כשל רגעי בפיד (Cloudflare/timeout)
    # החזיר 0 יעדים, ו-run() דרס את התמונה הטובה של היום באפס שורות — כל
    # הנתונים נעלמו מהמסך (אסי, 27/07/2026). אין נתונים ⇒ משאירים מה שהיה.
    if not targets:
        logger.warning("zap: 0 targets — משאירים את התמונה הקודמת על כנה")
        return {"ok": False, "error": "הפיד לא החזיר מוצרים — התמונה הקודמת נשמרה",
                "rows": [], "summary": _summarise([])}
    if limit:
        targets = targets[:limit]
    total = len(targets)
    by_model: dict = {}
    orphans = []
    for i, t in enumerate(targets, 1):
        try:
            r = analyse(t)
        except Exception as e:  # noqa: BLE001
            logger.warning("zap analyse failed for %s: %s", t.get("sku"), e)
            continue
        mid = r.get("modelid")
        if not mid:
            orphans.append(r)
        else:
            g = by_model.get(mid)
            if g is None:
                g = dict(r)
                g["variants"] = []
                g["stock"] = 0
                by_model[mid] = g
            g["variants"].append({"sku": r["sku"], "name": r["name"],
                                  "stock": r.get("stock") or 0,
                                  "price": r.get("our_price"),
                                  "caps": r.get("caps") or []})
            g["stock"] += r.get("stock") or 0
            # המחיר שמתחרה בזאפ הוא הזול מבין הצבעים
            if r.get("our_price") and (not g.get("our_price") or r["our_price"] < g["our_price"]):
                g["our_price"] = r["our_price"]
                g["sku"] = r["sku"]
        if i % 15 == 0 or i == total:
            try:
                db.sales_state_set("zap_progress", json.dumps(
                    {"done": i, "total": total, "at": t.get("name", "")[:60]}, ensure_ascii=False))
                db.sales_state_set("zap_snap:partial", json.dumps(
                    {"date": date.today().isoformat(), "partial": True,
                     "rows": list(by_model.values()) + orphans,
                     "summary": _summarise(list(by_model.values()) + orphans, partial=True)},
                    ensure_ascii=False))
            except Exception:  # noqa: BLE001
                pass
        time.sleep(sleep)
    # מחיר/מיקום מחושבים מחדש על המחיר הזול של הקבוצה
    for g in by_model.values():
        g["colors"] = len(g["variants"])
        g["caps"] = sorted({c for v in g["variants"] for c in (v.get("caps") or [])}) or g.get("caps") or []
        if g.get("our_price") and g.get("low"):
            g["gap_pct"] = round((g["our_price"] / g["low"] - 1) * 100, 1)
    rows = sorted(by_model.values(), key=lambda r: -(r.get("stock") or 0)) + orphans
    for r in rows:
        r["reason_code"], r["reason"] = _reason(r)
    # רשת ביטחון שנייה: קריסה חדה במספר השורות היא כמעט תמיד תקלה ולא שינוי
    # אמיתי בקטלוג. עדיף להציג נתון של אתמול מאשר מסך ריק.
    prev = latest() or {}
    pn, cn = len(prev.get("rows") or []), len(rows)
    if not limit and pn >= 10 and cn < pn * 0.5:
        logger.warning("zap: %d שורות מול %d קודמות — לא שומרים", cn, pn)
        return {"ok": False, "error": f"נסרקו {cn} שורות מול {pn} — נראה כתקלה, "
                                      "התמונה הקודמת נשמרה",
                "rows": rows, "summary": _summarise(rows)}
    snap = {"date": date.today().isoformat(), "rows": rows, "summary": _summarise(rows)}
    try:
        db.sales_state_set("zap_progress", "")
        db.sales_state_set(f"zap_snap:{snap['date']}", json.dumps(snap, ensure_ascii=False))
        db.sales_state_set("zap_snap:latest", json.dumps(snap, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001
        logger.warning("zap snapshot save failed: %s", e)
    logger.info("zap scan: %s", snap["summary"])
    return snap


def latest() -> dict | None:
    raw = db.sales_state_get("zap_snap:latest")
    return json.loads(raw) if raw else None


def history(days: int = 30) -> list:
    out = []
    for i in range(days):
        d = (date.today() - timedelta(days=i)).isoformat()
        raw = db.sales_state_get(f"zap_snap:{d}")
        if raw:
            try:
                out.append({"date": d, **json.loads(raw).get("summary", {})})
            except Exception:  # noqa: BLE001
                pass
    return list(reversed(out))
