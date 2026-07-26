"""
zap_scan.py — סריקה יומית של זאפ: איפה כל סמארטפון שלנו עומד מול המתחרים.

⚠️ הממצא שהוליד את המודול (26/07/2026): על דגמי הסמארטפון המרכזיים שלנו
**איננו מופיעים בזאפ בכלל** — לא ב-ld+json ולא בגוף הדף. במקביל זאפ כן הביא
לנו 148 הזמנות ו-₪162 אלף מאז 2025 — אבל מחזור המכשירים משם צנח מ-₪15,869
בפברואר 2026 ל-**אפס** ביולי. כלומר נשרנו מהערוץ בדיוק בחלון שבו המכירות נפלו.

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
def build_targets() -> list:
    """כל סמארטפון עם מלאי בקופה + מחיר האתר (מקור-האמת למחיר שלנו).
    ⚠️ מק"ט אחד בקופה יושב על כמה וריאציות באתר (eSIM / מקביל-רשמי) — לוקחים
    את הזולה שבהן, כי היא זו שמתחרה בזאפ."""
    import requests
    import poller
    base = os.getenv("WC_STORE_URL", "https://greenmobile.co.il").rstrip("/")
    auth = (os.getenv("WC_CONSUMER_KEY", ""), os.getenv("WC_CONSUMER_SECRET", ""))
    try:
        prods = poller.client().get_all_products(category=3)
    except Exception as e:  # noqa: BLE001
        logger.warning("zap: POS catalog failed: %s", e)
        return []
    live = [p for p in prods if p.get("isActive") and (p.get("currentStock") or 0) > 0]

    prices = {}
    for chunk in range(0, len(live), 50):
        skus = ",".join(str(p["id"]) for p in live[chunk:chunk + 50])
        try:
            r = requests.get(f"{base}/wp-json/wc/v3/products", auth=auth, timeout=60,
                             params={"sku": skus, "per_page": 100,
                                     "_fields": "sku,price,status,stock_status"})
            for w in (r.json() if r.ok else []):
                p = float(w.get("price") or 0)
                if p > 1 and w.get("status") == "publish":
                    k = str(w.get("sku"))
                    prices[k] = min(p, prices.get(k, p))
        except Exception as e:  # noqa: BLE001
            logger.warning("zap: wc price chunk failed: %s", e)

    out = []
    for p in live:
        sku = str(p["id"])
        out.append({"sku": sku, "name": p["name"], "brand": brand_of(p["name"]),
                    "stock": p.get("currentStock") or 0,
                    "our_price": prices.get(sku) or (p.get("price") if (p.get("price") or 0) > 1 else None)})
    return out


# ─────────────────────────── זאפ ───────────────────────────
CAP_RE = re.compile(r"(\d+)\s*(TB|GB)\b", re.I)
ACCESSORY = ("case", "cover", "כיסוי", "מגן", "נרתיק", "sandstone", "silicone",
             "כבל", "מטען", "אוזני", "סוללה", "מעמד", "זכוכית")


# תוספות דגם שמבדילות בין גרסאות — חייבות להיות זהות בשני הצדדים.
# ⚠️ הסדר חשוב: "pro max" נבדק לפני "pro", אחרת Pro Max יזוהה כ-Pro.
QUALIFIERS = (("pro max", "promax"), ("pro+", "proplus"), ("pro", "pro"),
              ("plus", "plus"), ("ultra", "ultra"), ("mini", "mini"),
              ("air", "air"), ("lite", "lite"), ("fe", "fe"), ("edge", "edge"))


def _capacity(s: str) -> str | None:
    """נפח האחסון, מנורמל ל-GB. ⚠️ בכותרות זאפ מופיע גם ה-RAM ("12GB+256GB"),
    ולכן לוקחים את **הגדול** מבין הערכים — האחסון תמיד ≥ הזיכרון."""
    vals = [int(n) * (1024 if u.upper() == "TB" else 1)
            for n, u in CAP_RE.findall(s or "")]
    return str(max(vals)) if vals else None


def _quals(s: str) -> set:
    low = " " + re.sub(r"[^a-z0-9+ ]", " ", (s or "").lower()) + " "
    out, seen = set(), low
    for pat, tag in QUALIFIERS:
        if f" {pat} " in seen:
            out.add(tag)
            seen = seen.replace(f" {pat} ", " ")   # שלא ייספר שוב כחלק קצר יותר
    return out


def _model_tokens(s: str) -> set:
    """אסימוני דגם: מספרים ושילובי אות+מספר (17, A57, X9, V6, Note)."""
    low = (s or "").lower()
    low = CAP_RE.sub(" ", low)                       # הנפח נבדק בנפרד
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
    ta, tb = _model_tokens(our_name), _model_tokens(zap_title)
    return bool(ta & tb) if ta else True


def _model_title(mid: int) -> str:
    try:
        html = _get(f"{BASE}/model.aspx?modelid={mid}")
    except Exception:  # noqa: BLE001
        return ""
    m = re.search(r"<title>(.*?)</title>", html, re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def resolve_modelid(name: str, sku: str) -> int | None:
    """modelid של הדגם בזאפ, **מאומת מול שם המוצר**. נשמר לצמיתות.
    '0' = חיפשנו ולא נמצאה התאמה תקפה, כדי לא לחפש שוב כל יום."""
    key = f"zap_mid:{sku}"
    cached = db.sales_state_get(key)
    if cached:                            # '' = אופס ידנית → מחשבים מחדש
        return int(cached) or None
    if any(a in (name or "").lower() for a in ACCESSORY):
        db.sales_state_set(key, "0")      # אביזר שסווג בטעות כטלפון בקופה
        return None
    q = re.sub(r"\s+", " ", (name or "")).strip()
    q = re.sub(r"\s*-\s*(שחור|לבן|כחול|סגול|ורוד|ירוק|אדום|זהב|כסוף|אפור|תכלת|כתום|"
               r"black|white|blue|purple|pink|green|red|gold|silver|gray|grey|orange|navy)\b.*$",
               "", q, flags=re.I)
    try:
        html = _get(f"{BASE}/search.aspx?keyword={urllib.parse.quote(q)}")
    except Exception as e:  # noqa: BLE001
        logger.warning("zap search failed for %s: %s", q, e)
        return None                       # שגיאת רשת — לא נועלים מטמון
    mid = 0
    for cand in list(dict.fromkeys(re.findall(r"modelid=(\d+)", html)))[:5]:
        title = _model_title(int(cand))
        if _match_ok(q, title):
            mid = int(cand)
            db.sales_state_set(f"zap_title:{sku}", title[:160])
            break
        time.sleep(0.4)
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
    mid = resolve_modelid(t["name"], t["sku"])
    row = {"sku": t["sku"], "name": t["name"], "brand": t["brand"],
           "stock": t["stock"], "our_price": t.get("our_price"), "modelid": mid}
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
    row.update({
        "url": f"{BASE}/model.aspx?modelid={mid}",
        "zap_title": db.sales_state_get(f"zap_title:{t['sku']}") or "",
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
    """סבב מלא. ~250 דגמים ≈ 5 דקות. נשמר תחת zap_snap:<date> ו-zap_snap:latest."""
    targets = build_targets()
    if limit:
        targets = targets[:limit]
    rows = []
    for t in targets:
        try:
            rows.append(analyse(t))
        except Exception as e:  # noqa: BLE001
            logger.warning("zap analyse failed for %s: %s", t.get("sku"), e)
        time.sleep(sleep)
    ranked = [r for r in rows if r.get("rank")]
    listed = [r for r in rows if r.get("status") == "listed"]
    missing = [r for r in rows if r.get("status") == "missing"]
    gaps = [r["gap_pct"] for r in rows if r.get("gap_pct") is not None]
    snap = {
        "date": date.today().isoformat(),
        "rows": rows,
        "summary": {
            "scanned": len(rows),
            "on_zap": len(listed) + len(missing),      # יש דף מודל בזאפ
            "listed": len(listed),
            "missing": len(missing),                   # יש דגם — ואנחנו לא בו
            "no_model": sum(1 for r in rows if r.get("status") == "no_model"),
            "in_top5": sum(1 for r in ranked if r["rank"] <= 5),
            "in_top3": sum(1 for r in ranked if r["rank"] <= 3),
            "median_rank": sorted(r["rank"] for r in ranked)[len(ranked) // 2] if ranked else None,
            "avg_gap_pct": round(sum(gaps) / len(gaps), 1) if gaps else None,
        },
    }
    try:
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
