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
from datetime import datetime

import requests

import config as cfg
import db
import poller
import alerts

logger = logging.getLogger("auto_transfer")

SITE_BRANCH = 5
PREF_SOURCE = [2, 1, 3, 4]          # סטאר ראשון — הוראת אסי; אח"כ שאר הסניפים
# processing = שולם. אפשר להרחיב ב-env, מופרד בפסיקים (למשל "processing,on-hold")
STATUSES = [s.strip() for s in os.getenv("AUTO_TR_STATUSES", "processing").split(",") if s.strip()]


def _wc_creds():
    base = os.getenv("WC_STORE_URL", "").rstrip("/")
    k = os.getenv("WC_CONSUMER_KEY", "")
    s = os.getenv("WC_CONSUMER_SECRET", "")
    return (base, k, s) if (base and k and s) else None


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
        if not any(str(x.get("number")) == num for x in lst):
            lst.insert(0, {"number": num, "item": name, "partial": bool(partial)})
            db.sales_state_set("order_oos_list", json.dumps(lst[:100], ensure_ascii=False))
    except Exception as e:  # noqa: BLE001
        logger.warning("oos mark failed for %s: %s", number, e)


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
    for li in o.get("line_items", []):
        sku = str(li.get("sku") or "").strip()
        qty = max(1, int(li.get("quantity") or 1))
        if not sku:
            # פריט פיזי ללא מק"ט — בעיית נתונים (המוצר הועלה בלי SKU). לא לדלג
            # בשקט: לסמן את ההזמנה לטיפול ידני (אחרת לא משודרת ולא מסומנת OOS).
            _mark_unmatched(o.get("number"), li.get("name") or "פריט ללא מק\"ט")
            continue
        if sku not in catalog:
            continue                       # יש מק"ט אך לא בקטלוג הקופה (דיגיטלי/לא מסונכרן)
        cat_it = catalog.get(sku) or {}
        if cat_it.get("is_stock") is False:
            continue                       # מוצר דיגיטלי/לא-מנוהל-מלאי (גיפט קארד/קוד) — אין שידור/OOS
        name = cat_it.get("name") or li.get("name") or sku
        try:
            stock = no.get_product_stock(sku)
        except Exception as e:  # noqa: BLE001
            logger.warning("stock read failed for %s: %s", sku, e)
            continue
        # מלאי האתר לא נחשב — תמיד מביאים יחידה מסניף אמיתי (לפי src_pref)
        src = next((b for b in src_pref if (stock.get(b) or 0) >= qty), None)
        if src is None:                    # אין סניף עם כל הכמות — מספיק שיש משהו
            src = next((b for b in src_pref if (stock.get(b) or 0) > 0), None)
        if src is None:
            logger.info("order %s: no source stock for %s", o.get("number"), sku)
            oos_names.append(name)             # נסמן בסוף (כדי לדעת אם חלקי)
            continue
        # מוצר סריאלי: מצמידים יחידות ספציפיות (הוותיקות) — כמו בבקשה ידנית.
        # כך היחידה מוצגת "משוריין לאתר" במלאי חי, וקליטתה סוגרת את הבקשה אוטומטית.
        lines = []
        if (catalog.get(sku) or {}).get("kind") == "serial":
            try:
                raw = no.get_product_serials(sku) or []
                units = [u for u in raw
                         if str(u.get("branchId")) == str(src) and int(u.get("status") or 0) == 1]
                units.sort(key=lambda u: str(u.get("insertDate") or ""))
                for u in units[:qty]:
                    if u.get("serial"):
                        lines.append({"product_id": sku, "name": f"{name} — סריאל {u['serial']}",
                                      "from_branch": src, "to_branch": SITE_BRANCH,
                                      "qty": 1, "serial": str(u["serial"])})
            except Exception as e:  # noqa: BLE001
                logger.warning("serial pick failed for %s: %s", sku, e)
        rem = qty - len(lines)
        if rem > 0:
            lines.append({"product_id": sku, "name": name,
                          "from_branch": src, "to_branch": SITE_BRANCH, "qty": rem})
        ids = db.plan_add(lines, created_by=f"אוטו · הזמנת אתר #{o.get('number')}")
        db.plan_mark_broadcast(src, ids)
        created.append({"sku": sku, "name": name, "src": src, "qty": qty})
        logger.info("order %s: broadcast transfer %s x%s from branch %s -> site",
                    o.get("number"), sku, qty, src)
    # סימון OOS בסוף — אם חלק מההזמנה כן שודר (created) זה "שודר חלקי" ולא חוסר מלא
    if oos_names:
        _mark_oos(o.get("number"), oos_names[0], partial=bool(created))
    return created


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
        created = _handle_order(o, catalog)
        if created:
            lines = "\n".join(f"• {c['name']} ×{c['qty']} — מ{cfg.branch_name(c['src'])}"
                              for c in created)
            alerts._send(os.getenv("TELEGRAM_ADMIN_CHAT", "448181407"),
                         f"📦 <b>שודרה בקשת העברה אוטומטית לאתר</b>\n"
                         f"הזמנה <b>#{o.get('number')}</b>:\n{lines}")
            for c in created:              # גם לצ'אט של סניף המקור, אם מוגדר
                chat = alerts._branch_chat(c["src"])
                if chat:
                    alerts._send(chat, f"📦 בקשת העברה חדשה לאתר: {c['name']} ×{c['qty']}\n"
                                       f"(הזמנת אתר #{o.get('number')} — שודר אוטומטית)")
