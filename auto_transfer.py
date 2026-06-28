"""
שידור אוטומטי של בקשות העברה לאתר — מהזמנות אתר (WooCommerce).

הרעיון (אסי, 12/06/2026): כל הזמנה ששולמה באתר ובה פריט שמחובר לקופה
(SKU == מק"ט NewOrder) — אם לסניף האתר (5) אין מלאי זמין, נוצרת אוטומטית
שורת תוכנית העברה מהסניף שמחזיק מלאי ומשודרת מיד למסך שלו.
עדיפות מקור: סטאר (2) ראשון, אחר כך שאר חנויות אשדוד.

מכוסות גם ההזמנות שנוצרות מ-GreenOS (מלאי חי / וואטסאפ) — גם הן הזמנות WC.
טריגר: scheduler כל 5 דק' + ריצה מיידית אחרי אישור תשלום (IPN).

ריצה ראשונה מסמנת את ההזמנות הקיימות כ"נראו" בלי לשדר — כדי לא להציף
את הסניפים בבקשות על הזמנות שכבר טופלו.
"""
import logging
import os
import re
from datetime import datetime

import requests

import config as cfg
import db
import poller
import alerts

logger = logging.getLogger("auto_transfer")

SITE_BRANCH = 5
PREF_SOURCE = [2, 1, 3, 4]          # סטאר ראשון — הוראת אסי; אח"כ שאר הסניפים
# ── איזון אוטומטי: ניצול הזמנת אתר לאזן מלאי בין הסניפים (אסי 19/06) ──
# אחרי מילוי הזמנה, הסניף-העודף מחלק את כל העודף שלו (כל מה מעל 1) לסניפים הריקים (0).
# סימטריה: לוקחים מהחלש (PREF_SOURCE: גן מועדף כמקור), ממלאים את החזק (BALANCE_TARGET:
# סטאר=מרכז קודם, אז סיטי החזק, ...גן החלש אחרון). מקור תמיד שומר ≥1 (אין cascade).
STAR_BRANCH = 2
BALANCE_TARGET = [2, 3, 4, 1]       # יעד השלמה לפי חוזק/חשיבות: סטאר→סיטי→עד הלום→גן
REPL_MIN_SOURCE = 2                 # מקור חייב ≥2 כדי לתת 1 ולהישאר עם ≥1
# processing = שולם. אפשר להרחיב ב-env, מופרד בפסיקים (למשל "processing,on-hold")
STATUSES = [s.strip() for s in os.getenv("AUTO_TR_STATUSES", "processing").split(",") if s.strip()]
# קטגוריות שמשדרים בהן בקשת **כמות** (מק"ט בלבד), בלי סריאל ספציפי — גם אם המוצר
# מנוהל-סריאל בקופה. אוזניות/שמע = מוצר קטן עם הרבה יחידות זהות; לחפש סריאל ספציפי
# בסניף זה מטרד מיותר (בקשת אסי 14/06). הסניף לוקח כל יחידה מאותו מק"ט.
NO_SERIAL_BCAST_CATEGORIES = {"שמע", "אוזניות גיימינג"}


def _wc_creds():
    base = os.getenv("WC_STORE_URL", "").rstrip("/")
    k = os.getenv("WC_CONSUMER_KEY", "")
    s = os.getenv("WC_CONSUMER_SECRET", "")
    return (base, k, s) if (base and k and s) else None


_addon_sku_cache = {}


def _wc_sku_for_product(wc_id):
    """SKU של מוצר WC לפי id (cached) — לפענוח תוספי Product Add-Ons → מק\"ט קופה."""
    wc_id = str(wc_id)
    if wc_id in _addon_sku_cache:
        return _addon_sku_cache[wc_id]
    sku = None
    creds = _wc_creds()
    if creds:
        base, k, s = creds
        try:
            r = requests.get(f"{base}/wp-json/wc/v3/products/{int(wc_id)}",
                             params={"_fields": "id,sku"}, auth=(k, s), timeout=20)
            if r.ok:
                sku = (r.json().get("sku") or "").strip() or None
        except Exception as e:  # noqa: BLE001
            logger.warning("addon sku lookup failed for %s: %s", wc_id, e)
    _addon_sku_cache[wc_id] = sku
    return sku


def _order_addon_items(o):
    """תוספי Product Add-Ons מכל שורות ההזמנה → שורות וירטואליות {sku, quantity, name}
    שישודרו לליקוט כמו פריט רגיל. מזוהים לפי '(#<wc_id>)' ב-display_key ו-'N x' בערך
    (הזמנה 46875: ראש מטען 20W #15478, נשמר כ-meta על שורת האייפון, לא כשורה)."""
    out = []
    for li in (o.get("line_items") or []):
        line_qty = max(1, int(li.get("quantity") or 1))
        for m in (li.get("meta_data") or []):
            key = str(m.get("display_key") or m.get("key") or "")
            if key.startswith("_"):
                continue
            mid = re.search(r"#(\d{2,})", key)
            if not mid:
                continue
            val = str(m.get("display_value") if m.get("display_value") is not None else (m.get("value") or ""))
            qm = re.search(r"(\d+)\s*[xX×]", val)
            if not qm:           # חתימת Product Add-On מסוג מוצר היא 'N x ...' — בלעדיה זה לא תוסף
                continue
            sku = _wc_sku_for_product(mid.group(1))   # ריק/None אם למוצר התוסף אין מק"ט
            qty = int(qm.group(1)) * line_qty
            name = re.sub(r"\s*\(#\d+\)\s*", "", key).strip() or ("מוצר " + mid.group(1))
            # sku ריק → השורה תיפול ל-_mark_unmatched בלולאה (גלוי, לא יושמט בשקט).
            out.append({"sku": sku or "", "quantity": qty, "name": name + " · תוסף", "_addon": True})
    return out


def _fetch_recent_orders():
    creds = _wc_creds()
    if not creds:
        return []
    base, k, s = creds
    out = []
    for status in STATUSES:
        try:
            r = requests.get(f"{base}/wp-json/wc/v3/orders",
                             params={"status": status, "per_page": 30,
                                     "orderby": "date", "order": "desc"},
                             auth=(k, s), timeout=40)
            if r.ok:
                out.extend(r.json())
        except requests.RequestException as e:
            logger.warning("orders fetch failed (%s): %s", status, e)
    return out


def _mark_oos(number, name, partial=False):
    """מסמן הזמנה כחסרה במלאי. partial=True כשחלק מההזמנה כן שודר ("שודר חלקי")
    וחלק חסר. נשמר כרשימת JSON אחת (עד 100 אחרונות)."""
    import json
    try:
        raw = db.sales_state_get("order_oos_list")
        lst = json.loads(raw) if raw else []
        num = str(number)
        existing = next((x for x in lst if str(x.get("number")) == num), None)
        if existing:                            # כבר ברשימה → לעדכן דגל 'חלקי' (ריצה חוזרת
            existing["partial"] = bool(partial)  # אחרי הצמדת מק"ט: שודר-חלקי במקום חסר-מלא)
            existing["item"] = name
        else:
            lst.insert(0, {"number": num, "item": name, "partial": bool(partial)})
        db.sales_state_set("order_oos_list", json.dumps(lst[:100], ensure_ascii=False))
    except Exception as e:  # noqa: BLE001
        logger.warning("oos mark failed for %s: %s", number, e)


def _unmark_oos(number):
    """מסיר הזמנה מרשימת ה-OOS — אחרי שכל הפריטים שודרו/זמינים (ריצה חוזרת)."""
    import json
    try:
        raw = db.sales_state_get("order_oos_list")
        if not raw:
            return
        lst = json.loads(raw)
        num = str(number)
        new = [x for x in lst if str(x.get("number")) != num]
        if len(new) != len(lst):
            db.sales_state_set("order_oos_list", json.dumps(new[:100], ensure_ascii=False))
    except Exception as e:  # noqa: BLE001
        logger.warning("oos unmark failed for %s: %s", number, e)


def _mark_unmatched(number, name):
    """מסמן הזמנה עם פריט פיזי שלא זוהה בקופה (אין SKU / SKU לא בקטלוג) — כדי
    שלא תיפול בשקט (לא שודרה ולא סומנה OOS). שומר רשימת JSON + התראת טלגרם חד-פעמית."""
    import json
    try:
        raw = db.sales_state_get("order_unmatched_list")
        lst = json.loads(raw) if raw else []
        num = str(number)
        if any(str(x.get("number")) == num for x in lst):
            return                              # כבר מסומן — בלי כפילות/התראה חוזרת
        lst.insert(0, {"number": num, "item": name})
        db.sales_state_set("order_unmatched_list", json.dumps(lst[:100], ensure_ascii=False))
        try:
            alerts._send(os.getenv("TELEGRAM_ADMIN_CHAT", "448181407"),
                         f"⚠️ <b>הזמנה דורשת טיפול ידני</b>\n"
                         f"הזמנה <b>#{num}</b>: הפריט <b>{name}</b> ללא מק\"ט מחובר לקופה —\n"
                         f"לא ניתן לשריין מלאי/לשדר. הוסף מק\"ט למוצר ב-WooCommerce.")
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001
        logger.warning("unmatched mark failed for %s: %s", number, e)


def _unmark_unmatched(number):
    """מסיר הזמנה מרשימת ה'דורש טיפול ידני' — אחרי שחובר מק"ט והפריט זוהה
    (כדי שבאנר 'פריט ללא מק"ט מחובר' יתנקה בשידור-מחדש)."""
    import json
    try:
        raw = db.sales_state_get("order_unmatched_list")
        if not raw:
            return
        lst = json.loads(raw)
        num = str(number)
        new = [x for x in lst if str(x.get("number")) != num]
        if len(new) != len(lst):
            db.sales_state_set("order_unmatched_list",
                               json.dumps(new[:100], ensure_ascii=False))
    except Exception as e:  # noqa: BLE001
        logger.warning("unmatched unmark failed for %s: %s", number, e)


def _pickup_branch(o: dict) -> int:
    """סניף האיסוף שנבחר בהזמנת איסוף עצמי (מ-meta _gm_pickup_branch). None אם לא איסוף."""
    titles = " ".join((sl.get("method_title") or "") for sl in (o.get("shipping_lines") or []))
    if "איסוף" not in titles:
        return None
    meta = {m.get("key"): m.get("value") for m in (o.get("meta_data") or [])}
    raw = str(meta.get("_gm_pickup_branch") or "").split(" - ")[0].replace("סניף", "").strip()
    if not raw:
        return None
    for bid, nm in cfg.BRANCHES.items():
        if bid != SITE_BRANCH and (nm == raw or raw in nm or nm in raw):
            return bid
    return None


def _has_open_balance(sku) -> bool:
    """האם כבר קיימת השלמה אוטומטית פתוחה על המק"ט (מונע איזון כפול בכל סריקה/הזמנה)."""
    try:
        for l in db.plan_list():
            if (str(l.get("product_id")) == str(sku)
                    and str(l.get("created_by") or "").startswith("השלמה אוטומטית")):
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _handle_order(o: dict, catalog: dict) -> list:
    """בודק שורות הזמנה ומשדר בקשות העברה לפי הצורך. מחזיר את מה ששודר.

    ⚠️ מלאי האתר (סניף 5) תמיד שמור להזמנות קודמות שבקליטה — לא נחשב זמין להזמנה חדשה.
    לכן כל הזמנה מקבלת יחידה ייעודית שמשודרת מסניף אמיתי → אתר. בהזמנת איסוף עצמי
    מעדיפים את סניף האיסוף כמקור (אם יש לו מלאי) כדי לחסוך שינוע."""
    no = poller.client()
    pickup_b = _pickup_branch(o)
    # סדר מקור: בהזמנת איסוף — סניף האיסוף ראשון; אחר כך סטאר/אשדוד כרגיל
    src_pref = ([pickup_b] + [b for b in PREF_SOURCE if b != pickup_b]) if pickup_b else PREF_SOURCE
    created = []
    oos_names = []           # פריטים חסרים בכל הסניפים — לזיהוי "שודר חלקי" בסוף
    had_unmatched = False     # פריט פיזי ללא מק"ט בריצה הזו (לעדכון הבאנר)
    # שורות ההזמנה + תוספי Product Add-Ons (ראש מטען וכו') כשורות וירטואליות — כך
    # שגם התוסף משודר לליקוט, לא רק המוצר הראשי (פער שהתגלה בהזמנה 46875).
    for li in (list(o.get("line_items", [])) + _order_addon_items(o)):
        sku = str(li.get("sku") or "").strip()
        qty = max(1, int(li.get("quantity") or 1))
        if not sku:
            # פריט פיזי ללא מק"ט — בעיית נתונים (המוצר הועלה בלי SKU). לא לדלג
            # בשקט: לסמן את ההזמנה לטיפול ידני (אחרת לא משודרת ולא מסומנת OOS).
            had_unmatched = True
            _mark_unmatched(o.get("number"), li.get("name") or "פריט ללא מק\"ט")
            continue
        if sku not in catalog:
            # מק"ט שלא נמצא בקטלוג הקופה. מק"ט **ג'אנק** (תבנית שכפול ליסט, '\d+(-\d+)+',
            # למשל 123456-3-1-2-1) = לא מחובר ל-NewOrder → מסמנים לטיפול ידני כדי
            # שההזמנה לא תיתקע אפורה בלי דגל. מק"ט רגיל שלא בקטלוג = כנראה מוצר דיגיטלי
            # WC-only (גיפט קארד/קוד) → מדלגים בשקט כמקודם.
            if re.fullmatch(r"\d+(-\d+)+", sku):
                had_unmatched = True
                _mark_unmatched(o.get("number"), li.get("name") or sku)
            continue
        cat_it = catalog.get(sku) or {}
        if cat_it.get("is_stock") is False:
            continue                       # מוצר דיגיטלי/לא-מנוהל-מלאי (גיפט קארד/קוד) — אין שידור/OOS
        name = cat_it.get("name") or li.get("name") or sku
        try:
            stock = no.get_product_stock(sku)
        except Exception as e:  # noqa: BLE001
            logger.warning("stock read failed for %s: %s", sku, e)
            continue
        # מוצר סריאלי (ולא אוזניות/שמע) → מצמידים סריאלים ספציפיים. טוענים מראש
        # את הסריאלים הזמינים לכל סניף (כדי לפצל נכון בין סניפים).
        by_qty = (cat_it.get("category") or "") in NO_SERIAL_BCAST_CATEGORIES
        is_serial = (catalog.get(sku) or {}).get("kind") == "serial" and not by_qty
        serials_by_branch = {}
        if is_serial:
            try:
                for u in (no.get_product_serials(sku) or []):
                    if int(u.get("status") or 0) == 1 and u.get("serial"):
                        serials_by_branch.setdefault(str(u.get("branchId")), []).append(u)
                for b in serials_by_branch:
                    serials_by_branch[b].sort(key=lambda u: str(u.get("insertDate") or ""))
            except Exception as e:  # noqa: BLE001
                logger.warning("serial pick failed for %s: %s", sku, e)
        # ⚠️ פיצול בין סניפים: ממלאים את הכמות מכמה סניפים לפי המלאי בכל אחד (סדר
        # src_pref). מלאי האתר (5) לא נחשב — לא ב-src_pref. כך הזמנת 2 יח׳ ש-1 בגן
        # העיר ו-1 בסיטי תשדר יחידה מכל סניף — ולא 2 מסניף שיש בו רק 1.
        lines = []
        remaining = qty
        used = []
        # מעדיפים סניף יחיד (לפי עדיפות) שמכסה את **כל** הכמות — פחות העברות וריקון
        # מיותר; רק אם אין כזה מפצלים בין סניפים.
        solo = next((b for b in src_pref if int(stock.get(b) or 0) >= qty), None)
        order_src = [solo] if solo else src_pref
        for b in order_src:
            if remaining <= 0:
                break
            avail = int(stock.get(b) or 0)
            if avail <= 0:
                continue
            take = min(remaining, avail)
            units = serials_by_branch.get(str(b), [])[:take] if is_serial else []
            for u in units:
                lines.append({"product_id": sku, "name": f"{name} — סריאל {u['serial']}",
                              "from_branch": b, "to_branch": SITE_BRANCH, "qty": 1,
                              "serial": str(u["serial"])})
            qrem = take - len(units)           # יתרה בלי סריאל באותו סניף → בקשת כמות
            if qrem > 0:
                lines.append({"product_id": sku, "name": name, "from_branch": b,
                              "to_branch": SITE_BRANCH, "qty": qrem})
            remaining -= take
            used.append((b, take))
        if not lines:                          # אין מלאי בשום סניף אמיתי
            logger.info("order %s: no source stock for %s", o.get("number"), sku)
            oos_names.append(name)
            continue
        ids = db.plan_add(lines, created_by=f"אוטו · הזמנת אתר #{o.get('number')}")
        db.plan_mark_broadcast(used[0][0], ids)   # line_ids ניתנו → מסמן את כל השורות (כל הסניפים)
        for b, tk in used:
            created.append({"sku": sku, "name": name, "src": b, "qty": tk})
        if remaining > 0:                      # לא הספיק מלאי לכל הכמות — היתרה חסרה (חלקי)
            oos_names.append(name)
        logger.info("order %s: broadcast %s x%s split across %s -> site",
                    o.get("number"), sku, qty, used)
        # ── איזון אוטומטי: מפזרים את עודף הסניפים (כל מה מעל 1) לסניפים הריקים (0),
        # יעד לפי BALANCE_TARGET (סטאר קודם), מקור = העודף הגדול ביותר, שומר ≥1 ──
        try:
            # ⚠️ איזון אוטומטי **רק למכשירים סריאליים** — לא לאביזרים/לא-סריאליים
            # (הוראת אסי). אחרת השלמה מיותרת מבלבלת את הסניף (הזמנה 47343: Anker
            # לא-סריאלי הפיק השלמה עד הלום→סיטי ששמואל שלח בטעות).
            if is_serial and not _has_open_balance(sku):
                eff = {b: int(stock.get(b) or 0) for b in (1, 2, 3, 4)}
                for b, tk in used:
                    eff[b] = eff.get(b, 0) - tk     # מה שההזמנה כבר לקחה
                for tgt in BALANCE_TARGET:
                    if eff.get(tgt, 0) != 0:
                        continue                    # ממלאים רק סניף ריק (0)
                    # מקור: העודף הגדול ביותר (≥2) מבין הסניפים — **לא סטאר** (המרכז
                    # רק מתמלא, לא מרוקנים אותו לטובת אחרים).
                    src = max((b for b in (1, 3, 4)
                               if b != tgt and eff.get(b, 0) >= REPL_MIN_SOURCE),
                              key=lambda b: eff[b], default=None)
                    if src is None:
                        break                       # אין יותר עודף לחלק
                    bids = db.plan_add(
                        [{"product_id": sku, "name": name, "from_branch": src,
                          "to_branch": tgt, "qty": 1}],
                        created_by=f"השלמה אוטומטית · הזמנת אתר #{o.get('number')}")
                    db.plan_mark_broadcast(src, bids)
                    created.append({"sku": sku, "name": name, "src": src,
                                    "qty": 1, "balance": True, "to": tgt})
                    eff[src] -= 1
                    eff[tgt] += 1
                    logger.info("order %s: auto-balance %s 1u %s -> %s",
                                o.get("number"), sku, src, tgt)
        except Exception as e:  # noqa: BLE001
            logger.warning("auto-balance failed for %s: %s", sku, e)
    # אם כבר אין פריט ללא מק"ט (חובר מק"ט) — מנקים את ההזמנה מרשימת הטיפול הידני
    if not had_unmatched:
        _unmark_unmatched(o.get("number"))
    # סימון OOS בסוף — אם חלק מההזמנה כן שודר (created) זה "שודר חלקי" ולא חוסר מלא
    if oos_names:
        _mark_oos(o.get("number"), oos_names[0], partial=bool(created))
    else:
        _unmark_oos(o.get("number"))   # הכל זמין/שודר → לנקות דגל OOS ישן (ריצה חוזרת)
    if created:                        # דגל עמיד "שודר אי-פעם" — מונע שידור כפול בריפוי-עצמי
        db.sales_state_set(f"auto_tr_bcast:{o.get('number')}", "1")
    return created


def _il_now_dt():
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Jerusalem"))
    except Exception:  # noqa: BLE001
        return datetime.utcnow()


def _il_dow() -> int:
    """יום בשבוע בשעון ישראל: ראשון=0 ... שבת=6 (להתאמת הסידור)."""
    return (_il_now_dt().weekday() + 1) % 7   # Mon=0..Sun=6 → ראשון=0..שבת=6


def _cur_week_start() -> str:
    """תאריך ראשון של השבוע הנוכחי (שעון ישראל), YYYY-MM-DD."""
    from datetime import timedelta
    d = _il_now_dt().date()
    return (d - timedelta(days=(d.weekday() + 1) % 7)).isoformat()


# שעות פעילות סניף: dow→(פתיחה, סגירה). ראשון=0..שבת=6. שבת סגור (לא ברשימה).
_BRANCH_HOURS = {0: (9, 21), 1: (9, 21), 2: (9, 21), 3: (9, 21), 4: (9, 21), 5: (9, 14)}


def _branch_open_now() -> bool:
    """האם עכשיו בתוך שעות פעילות הסניפים (שעון ישראל)."""
    now = _il_now_dt()
    hrs = _BRANCH_HOURS.get((now.weekday() + 1) % 7)
    return bool(hrs) and hrs[0] <= now.hour < hrs[1]


def _hhmm_min(s):
    try:
        h, m = str(s).strip().split(":"); return int(h) * 60 + int(m)
    except Exception:  # noqa: BLE001
        return None


def _covers_now(hours) -> bool:
    """האם משמרת בשעות אלה (HH:MM-HH:MM) מכסה את הרגע הנוכחי. ריק → כל היום."""
    p = (hours or "").strip().split("-")
    if len(p) != 2:
        return True
    a, b = _hhmm_min(p[0]), _hhmm_min(p[1])
    if a is None or b is None:
        return True
    # ⚠️ הסידור שומר חלק מהשעות **הפוך** (באג RTL — "20:00-09:00" במקום "09:00-20:00").
    # ממיינים כדי לנרמל את ההיפוך לטווח יום תקין. (כל המשמרות יומיות; אין משמרות לילה
    # חוצות-חצות. אם יתווספו כאלה בעתיד — צריך לתקן את שמירת השעות בסידור, לא כאן.)
    lo, hi = (a, b) if a <= b else (b, a)
    now = _il_now_dt()
    return lo <= now.hour * 60 + now.minute <= hi


def _shift_ids_on_now(src_branch):
    """telegram_ids של עובדים שמשובצים בסניף *עכשיו* (שעות המשמרת מכסות את הרגע)."""
    emps = db.shift_employees_on(int(src_branch), _il_dow(), _cur_week_start())
    names = [e.get("employee") for e in emps
             if e.get("employee") and _covers_now(e.get("hours"))]
    return db.shift_telegram_ids_for_names(names) if names else []


def shift_send_chat(chat_id, text, reply_markup=None) -> bool:
    """שליחת הודעה בודדת דרך בוט המשמרות (@Greenm_alert_bot). מחזיר הצלחה."""
    tok = os.getenv("SHIFT_BOT_TOKEN", "").strip()
    if not tok or not chat_id:
        return False
    import requests as _rq
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = _rq.post(f"https://api.telegram.org/bot{tok}/sendMessage", json=payload, timeout=12)
        return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def shift_dm_branch(src_branch, text) -> int:
    """DM אישי לעובדים שבמשמרת *עכשיו* בסניף. מחוץ לשעות הפעילות → נדחה ונשלח בבוקר
    הפתיחה (09:15) לצוות של אותו יום — שלא נפנה למי שכבר סיים/לפני שנפתח."""
    if not os.getenv("SHIFT_BOT_TOKEN", "").strip():
        return 0
    # ⚠️ הקריטריון הוא "מי במשמרת *עכשיו*", לא "שעות הפעילות". סניף שעובד בלילה
    # (גן העיר 20:00-09:00) "סגור" לפי שעות העסק אבל העובד **כן** שם — חייב לקבל מיד.
    try:
        ids = _shift_ids_on_now(src_branch)
    except Exception:  # noqa: BLE001
        ids = []
    if ids:                                   # יש מי שבמשמרת עכשיו (גם בלילה) → שולחים מיד
        sent = 0
        for cid in ids:
            if shift_send_chat(cid, text):
                sent += 1
        return sent
    # אף אחד לא במשמרת כרגע (מחוץ-לשעות / רווח באמצע יום) → נדחה לבוקר הפתיחה
    try:
        db.shift_alert_enqueue(int(src_branch), text)
        logger.info("shift alert deferred (nobody on shift now) for branch %s", src_branch)
    except Exception as e:  # noqa: BLE001
        logger.warning("shift alert enqueue failed: %s", e)
    return 0


def flush_pending_shift_alerts() -> int:
    """עבודת בוקר (09:15): שולח התראות שנדחו מחוץ-לשעות לצוות הבוקר של אותו סניף.
    רץ רק כשהסניפים פתוחים (יום עסקים). מחזיר כמה DM נשלחו."""
    if not os.getenv("SHIFT_BOT_TOKEN", "").strip() or not _branch_open_now():
        return 0
    pend = db.shift_alerts_pending()
    if not pend:
        return 0
    done, sent = [], 0
    for a in pend:
        try:
            for cid in _shift_ids_on_now(a["branch_id"]):
                if shift_send_chat(cid, a["text"]):
                    sent += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("flush shift alert %s failed: %s", a.get("id"), e)
        done.append(a["id"])   # מסומן כטופל גם אם אין מי שבמשמרת (מונע שליחה כפולה)
    db.shift_alerts_clear(done)
    if sent:
        logger.info("flushed %d deferred shift alerts (%d DMs)", len(done), sent)
    return sent


def _alert_created(o, created):
    """התראות טלגרם על בקשות העברה ששודרו (אדמין + צ'אט סניף המקור + DM אישי למשמרת)."""
    if not created:
        return
    lines = "\n".join(
        f"• {c['name']} ×{c['qty']} — מ{cfg.branch_name(c['src'])}"
        + (f" 🔄 השלמה אוטומטית → {cfg.branch_name(c.get('to', STAR_BRANCH))}"
           if c.get('balance') else "")
        for c in created)
    alerts._send(os.getenv("TELEGRAM_ADMIN_CHAT", "448181407"),
                 f"📦 <b>שודרה בקשת העברה אוטומטית לאתר</b>\n"
                 f"הזמנה <b>#{o.get('number')}</b>:\n{lines}")
    for c in created:                  # גם לצ'אט של סניף המקור, אם מוגדר
        chat = alerts._branch_chat(c["src"])
        if chat:
            alerts._send(chat, f"📦 בקשת העברה חדשה לאתר: {c['name']} ×{c['qty']}\n"
                               f"(הזמנת אתר #{o.get('number')} — שודר אוטומטית)")
    # 🔔 DM אישי לעובדים שבמשמרת בסניף המקור (לפי הסידור)
    bysrc = {}
    for c in created:
        bysrc.setdefault(c["src"], []).append(c)
    for src, items in bysrc.items():
        # יעד מפורש לכל פריט: הזמנה → לאתר; השלמה → לסניף היעד (מונע שליחה למקום הלא נכון)
        body = "\n".join(
            f"• {c['name']} ×{c['qty']} → "
            + (f"השלמת מלאי ל{cfg.branch_name(c.get('to'))}" if c.get('balance') else "לאתר 🌐")
            for c in items)
        is_bal = all(c.get('balance') for c in items)
        head = ("🔄 <b>השלמת מלאי</b>" if is_bal else f"🔔 <b>בקשת העברה — הזמנת אתר #{o.get('number')}</b>")
        shift_dm_branch(src, f"{head} · מ{cfg.branch_name(src)}\n{body}\n\nנא להכין להעברה ליעד המצוין.")


def _intl_phone(raw) -> str:
    """טלפון ישראלי → בינלאומי בלי + (לפורמט של Meta)."""
    d = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if d.startswith("972"):
        return d
    if d.startswith("0"):
        return "972" + d[1:]
    if len(d) == 9 and d.startswith("5"):
        return "972" + d
    return d


def _send_order_confirm(o):
    """הודעת 'הזמנה התקבלה' נייטיב ללקוח (template order_update_1 עם כפתור סטטוס
    דינמי). חד-פעמי לכל הזמנה, מאחורי דגל WA_SEND_ORDER_CONFIRM (כבוי עד אישור)."""
    if os.getenv("WA_SEND_ORDER_CONFIRM", "0") != "1":
        return
    num = str(o.get("number"))
    if db.sales_state_get(f"order_confirm_sent:{num}"):
        return
    b = o.get("billing") or {}
    phone = _intl_phone(b.get("phone"))
    if len(phone) < 11:
        return
    try:
        import wa
        wa.send_order_confirm(phone, (b.get("first_name") or "").strip(), num)
        db.sales_state_set(f"order_confirm_sent:{num}", "1")
        logger.info("order confirm sent for %s -> %s", num, phone)
    except Exception as e:  # noqa: BLE001
        logger.warning("order confirm failed for %s: %s", num, e)


def _rescan_flagged(orders, catalog):
    """ריפוי-עצמי: הזמנות מדוגלות (חסר-מלאי / ללא-מק"ט) שעדיין **לא שודר** להן כלום —
    אולי חובר להן מק"ט / רוענן הקטלוג מאז. מריץ אותן מחדש כך שישדרו לבד.
    גארד כפול נגד שידור כפול: מדלג אם יש דגל 'שודר אי-פעם' או שורת תוכנית קיימת."""
    import json as _json
    import re as _re
    flagged = set()
    for key in ("order_oos_list", "order_unmatched_list"):
        raw = db.sales_state_get(key)
        for x in (_json.loads(raw) if raw else []):
            flagged.add(str(x.get("number")))
    if not flagged:
        return
    plan_nums = set()
    for ln in db.plan_list():
        m = _re.search(r"הזמנת אתר #(\d+)", ln.get("created_by") or "")
        if m:
            plan_nums.add(m.group(1))
    proc_nums = {str(o.get("number")) for o in orders}   # ההזמנות שעדיין 'בטיפול'
    # ── ניקוי דגלים שהתיישנו: הזמנה מדוגלת (חסר/ללא-מק"ט) שכבר **התקדמה** מעבר ל'בטיפול'
    # (שלב הפצה / מוכנה / נמסר / הושלם / בוטל) — טופלה ידנית (למשל דרופ-שיפ מהיבואן),
    # אז הדגל לא רלוונטי. בודקים סטטוס נוכחי ומסירים. ⚠️ STATUSES ברירת-מחדל=processing
    # בלבד, לכן הזמנה כזו כבר לא ב-orders ולא הייתה מתנקה לעולם. ──
    _ACTIONABLE = {"processing", "on-hold", "pending"}
    for num in (flagged - proc_nums):
        st = _order_status_by_number(num)
        if st and st not in _ACTIONABLE:
            _unmark_oos(num)
            _unmark_unmatched(num)
            logger.info("self-heal: cleared stale flag for order %s (status=%s)", num, st)
    for o in orders:
        num = str(o.get("number"))
        if num not in flagged:
            continue
        if num in plan_nums or db.sales_state_get(f"auto_tr_bcast:{num}"):
            continue                   # כבר שודר (שורה פעילה או דגל עמיד) → לא נוגעים
        created = _handle_order(o, catalog)
        if created:
            logger.info("self-heal: order %s broadcast after re-link/catalog", num)
            _alert_created(o, created)


def _order_status_by_number(number) -> str:
    """סטטוס WC נוכחי של הזמנה לפי מספרה (להזמנות מדוגלות שיצאו מסטטוס 'בטיפול').
    מחזיר '' אם לא נמצא/שגיאה — אז לא נוגעים בדגל (זהירות: לא לנקות בטעות)."""
    creds = _wc_creds()
    if not creds:
        return ""
    base, k, s = creds
    try:
        r = requests.get(f"{base}/wp-json/wc/v3/orders",
                         params={"search": str(number), "per_page": 10}, auth=(k, s), timeout=25)
        for o in (r.json() if r.ok else []):
            if str(o.get("number")) == str(number):
                return o.get("status") or ""
    except requests.RequestException as e:
        logger.warning("status lookup failed for order %s: %s", number, e)
    return ""


def scan_orders():
    """הג'וב הראשי — סורק הזמנות אחרונות ומשדר העברות לחדשות בלבד."""
    orders = _fetch_recent_orders()
    if not orders:
        return
    if db.sales_state_get("auto_tr_init") is None:
        # ריצה ראשונה: לסמן הכל כקיים, בלי לשדר (ההזמנות האלה כבר טופלו ידנית)
        for o in orders:
            db.sales_state_set(f"auto_tr_seen:{o['id']}", "init")
        db.sales_state_set("auto_tr_init", datetime.now().isoformat(timespec="seconds"))
        logger.info("auto_transfer initialized — %d existing orders marked seen", len(orders))
        return
    catalog = db.catalog_load()
    if not catalog:
        return
    for o in reversed(orders):             # ישן → חדש
        oid = o.get("id")
        if not oid or db.sales_state_get(f"auto_tr_seen:{oid}"):
            continue
        db.sales_state_set(f"auto_tr_seen:{oid}", datetime.now().isoformat(timespec="seconds"))
        _alert_created(o, _handle_order(o, catalog))
        _send_order_confirm(o)               # 'הזמנה התקבלה' נייטיב ללקוח (מאחורי דגל)
    # ריפוי-עצמי: הזמנות שדוגלו (חסר/ללא-מק"ט) ושעדיין לא שודר להן — אולי חובר מק"ט מאז
    _rescan_flagged(orders, catalog)
