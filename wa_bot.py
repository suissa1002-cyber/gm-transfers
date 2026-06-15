"""בוט WhatsApp native — שלב 6 (ליבה דטרמיניסטית).

מבוסס על ניתוח 310K הודעות: הבוט הישן היה טופס-תפריט שלא ידע לענות → 40% ברחו
לנציג. כאן הליבה **עונה בפועל** על מה שהבוט הישן כשל בו (קודם כל: סטטוס הזמנה
אמיתי מ-WooCommerce במקום "בטיפול" גנרי). טקסט חופשי → אורי (שלב 6ג, בהמשך).

⚠️ גלגול בטוח: רץ **רק** על מספרים ב-BOT_WHITELIST. כל השאר ממשיכים לקונקטופ.
מופעל מה-webhook אחרי השמירה, עטוף ב-try/except — לעולם לא משפיע על לקוחות אחרים.
"""
from __future__ import annotations

import logging
import os

import db
import wa

logger = logging.getLogger("wa_bot")


def whitelist() -> set:
    return {p.strip() for p in os.getenv("BOT_WHITELIST", "").replace(" ", "").split(",") if p.strip()}


def enabled_for(phone: str) -> bool:
    """האם הבוט ה-native פעיל למספר הזה (whitelist בלבד — גלגול בטוח)."""
    wl = whitelist()
    return bool(wl) and str(phone) in wl


# ── תפריט ראשי (משופר מהניתוח) ──
MENU = [
    ("status",   "📦 סטטוס הזמנה", "מעקב אחר הזמנה קיימת"),
    ("branches", "📍 סניפים ושעות", "כתובות ושעות פתיחה"),
    ("shipping", "🚚 משלוחים", "אפשרויות ומחירים"),
    ("new",      "🛒 הזמנה חדשה", "מוצרים, מחירים וזמינות"),
    ("lab",      "🔧 מעבדה / תיקון", "הצעת מחיר לתיקון"),
    ("agent",    "👤 נציג אנושי", "מעבר לשירות אנושי"),
]
_TITLE2ID = {t: rid for rid, t, _ in MENU}

GREET = "היי! 👋 הגעת לגרין מובייל — רשת חנויות סלולר, גיימינג ומחשבים. במה אפשר לעזור?"

BRANCHES = ("📍 *הסניפים שלנו (אשדוד):*\n\n"
            "*סטאר סנטר* — ז'בוטינסקי 45 · 08-9477402\n"
            "*סיטי* — הציונות 13\n"
            "*גן העיר* · *עד הלום*\n\n"
            "🕘 א'-ה' 09:00-20:00 · ו' 09:00-15:00")

SHIPPING = ("🚚 *אפשרויות משלוח:*\n\n"
            "1. משלוח חינם — עד 7 ימי עסקים\n"
            "2. משלוח מהיר — 1-3 ימי עסקים (89₪)\n"
            "3. איסוף עצמי מהסניף / נקודת מסירה בת״א\n\n"
            "* משלוח חינם בהזמנות מעל 500₪")


def _menu(phone):
    wa.send_list(phone, GREET, MENU, button_label="לתפריט", section_title="במה לעזור?")
    db.bot_session_set(phone, "menu", {})


def handle(phone: str, text: str, mtype: str = "text", reply_id: str = ""):
    """נקודת הכניסה — מקבלת הודעת לקוח ומגיבה. מנוהל מצב ב-wa_bot_session.
    reply_id = id של כפתור/רשימה (ניתוב מדויק); נופלים לטקסט אם אין."""
    text = (text or "").strip()
    sess = db.bot_session_get(phone)
    state = sess.get("state")
    rid = reply_id or _TITLE2ID.get(text, "")   # id מהבחירה, או מיפוי מהכותרת
    low = text

    # ── זרימות תלויות-מצב ──
    if state == "await_order_number" and not rid:
        return _order_status(phone, text)
    if state == "new_search" and not rid:
        return _new_order_results(phone, text)
    if state == "new_pick" and rid.startswith("prod:"):
        return _product_card(phone, rid, sess.get("data") or {})

    # ── ניתוב תפריט ──
    if rid == "status" or ("סטטוס" in low and "הזמנ" in low):
        wa.send_text(phone, "מה מספר ההזמנה? (ספרות בלבד)")
        db.bot_session_set(phone, "await_order_number", {})
        return
    if rid == "branches" or "סניפ" in low:
        wa.send_text(phone, BRANCHES)
        return _menu_tail(phone)
    if rid == "shipping" or "משלוח" in low:
        wa.send_text(phone, SHIPPING)
        return _menu_tail(phone)
    if rid == "new" or rid == "search_again":
        wa.send_text(phone, "מה שם המוצר שאתה מחפש? 🔍\n(למשל: אייפון 17 פרו, גלקסי S25, אוזניות JBL)")
        db.bot_session_set(phone, "new_search", {})
        return
    if rid == "menu":
        return _menu(phone)
    if rid == "agent" or any(w in low for w in ["נציג", "אנושי", "בנאדם", "מישהו"]):
        return _to_agent(phone)
    if rid == "lab":
        return _to_agent(phone, note="פנייה למעבדה")

    # ברכה / טקסט חופשי / לא מזוהה → תפריט (שלב 6ג: כאן ייכנס אורי)
    _menu(phone)


def _new_order_results(phone, query):
    """חיפוש מוצר חי → רשימת תוצאות עם מחירים (התיקון מס' 1 של 'הזמנה חדשה')."""
    import main
    results = main.bot_product_search(query, limit=8)
    if not results:
        wa.send_text(phone, f"לא מצאתי '{query}' 🙁 נסה שם אחר, או כתוב 'נציג'.")
        return
    rows, data = [], {}
    for p in results:
        pid = f"prod:{p['id']}"
        price = p.get("price")
        desc = (f"₪{int(float(price)):,}" if price not in (None, "", "0") else "לפרטים")
        rows.append((pid, (p.get("name") or "מוצר")[:24], desc))
        data[pid] = {"name": p.get("name"), "price": price, "permalink": p.get("permalink"),
                     "sku": p.get("sku"), "stock": p.get("stock_status"),
                     "image": p.get("image")}
    rows.append(("search_again", "🔍 חיפוש חדש", ""))
    wa.send_list(phone, f"מצאתי {len(results)} תוצאות ל'{query}'. בחר/י לפרטים:",
                 rows, button_label="לתוצאות", section_title="תוצאות חיפוש")
    db.bot_session_set(phone, "new_pick", data)


def _product_card(phone, rid, data):
    """כרטיס מוצר אמיתי — תמונה + שם + מחיר חי + זמינות + קישור רכישה + כפתורים."""
    p = (data or {}).get(rid)
    if not p:
        wa.send_text(phone, "לא מצאתי את הפריט, ננסה שוב 🙏")
        return _menu(phone)
    price = p.get("price")
    lines = [f"*{p.get('name')}*"]
    if price not in (None, "", "0"):
        lines.append(f"💰 מחיר: ₪{int(float(price)):,}")
    if p.get("stock") == "instock":
        lines.append("✅ זמין במלאי")
    elif p.get("stock") == "outofstock":
        lines.append("⏳ אזל — ניתן להשיג מהספק (שאל נציג)")
    if p.get("permalink"):
        lines.append(f"\n🔗 לרכישה ולפרטים:\n{p['permalink']}")
    body = "\n".join(lines)
    btns = [("search_again", "🔍 מוצר אחר"), ("agent", "👤 נציג"), ("menu", "↩️ תפריט")]
    img = p.get("image") or ""
    try:                                   # כרטיס עם תמונה; אם נכשל — fallback לטקסט
        wa.send_buttons(phone, body, btns, header_image=img if img.startswith("http") else "")
    except Exception as e:  # noqa: BLE001
        logger.warning("product card image failed, text fallback: %s", e)
        wa.send_text(phone, body)
        wa.send_buttons(phone, "מה הלאה?", btns)
    db.bot_session_clear(phone)


def _menu_tail(phone):
    wa.send_buttons(phone, "עוד משהו?", [("menu", "↩️ לתפריט"), ("agent", "👤 נציג")])


def _to_agent(phone, note: str = ""):
    msg = "מעביר אותך לנציג אנושי — אחד מהצוות יחזור אליך בהקדם 🙏"
    if note:
        msg = f"בשמחה ({note}). " + msg
    wa.send_text(phone, msg)
    db.bot_session_set(phone, "agent", {"note": note})
    try:                                  # מסמן 'אנושי' שהבוט לא יקפוץ + מתריע
        import main
        main._tg_admin(f"👤 <b>לקוח ביקש נציג (בוט native)</b>\n{phone}{(' · ' + note) if note else ''}")
    except Exception:  # noqa: BLE001
        pass


def _order_status(phone, raw_num: str):
    """התיקון מס' 1: סטטוס הזמנה *אמיתי* מ-WooCommerce (במקום 'בטיפול' גנרי)."""
    num = "".join(ch for ch in (raw_num or "") if ch.isdigit())
    if not num:
        wa.send_text(phone, "מספר ההזמנה הוא ספרות בלבד 🙏 נסה שוב, או כתוב 'נציג'.")
        return
    info = _lookup_order(num)
    db.bot_session_clear(phone)
    if not info:
        wa.send_text(phone, f"לא מצאתי הזמנה מספר {num}. בדוק/י את המספר ונסה/י שוב.")
        return _menu_tail(phone)
    wa.send_text(phone, info)
    _menu_tail(phone)


def _lookup_order(num: str):
    """שולף סטטוס אמיתי + מעקב משלוח להזמנה לפי מספר."""
    try:
        import main
        import requests as _rq
        creds = main._wc_creds()
        if not creds:
            return None
        base, k, s = creds
        r = _rq.get(f"{base}/wp-json/wc/v3/orders",
                    params={"search": num, "per_page": 5}, auth=(k, s), timeout=20)
        if not r.ok:
            return None
        orders = [o for o in r.json() if str(o.get("number")) == str(num)]
        if not orders:
            return None
        o = orders[0]
        label = main._wc_statuses(base, k, s).get(o.get("status"), o.get("status"))
        meta = {m.get("key"): m.get("value") for m in (o.get("meta_data") or [])}
        cs = main._cargo_status(meta, (o.get("billing") or {}).get("email") or "")
        msg = f"📦 הזמנה #{num}\nסטטוס: *{label}*"
        if cs and cs.get("text"):
            msg += f"\nמשלוח: {cs['text']}"
        if cs and cs.get("track_url"):
            msg += f"\n🔗 מעקב: {cs['track_url']}"
        return msg
    except Exception as e:  # noqa: BLE001
        logger.warning("bot order lookup failed for %s: %s", num, e)
        return None
