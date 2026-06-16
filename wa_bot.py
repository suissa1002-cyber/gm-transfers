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

    # ── יציאה גלובלית מכל מצב (גם מתוך חיפוש/הזמנה שבולעים טקסט) ──
    if rid == "menu" or low in _ESCAPE:
        return _menu(phone)
    if rid == "agent" or any(w in low for w in ["נציג", "אנושי", "בנאדם", "מישהו"]):
        return _to_agent(phone)

    # ── זרימות תלויות-מצב ──
    if state == "await_order_number" and not rid:
        return _order_status(phone, text)
    if state == "new_search" and not rid:
        return _new_order_results(phone, text)
    if state == "new_pick" and rid.startswith("prod:"):
        return _product_card(phone, rid, sess.get("data") or {})
    # ── זרימת הזמנה בצ'אט ──
    if rid.startswith("buy:"):
        return _start_order(phone, rid.split(":", 1)[1], sess)
    if state == "order_color" and rid.startswith("color:"):
        d = sess.get("data") or {}
        return _show_storage(phone, d.get("pid"), d.get("product") or {},
                             d.get("variations") or [], rid.split(":", 1)[1])
    if state == "order_storage" and rid.startswith("storage:"):
        d = sess.get("data") or {}
        st = rid.split(":", 1)[1]
        prod = d.get("product") or {}
        vs = [v for v in (d.get("variations") or [])
              if (not d.get("color") or v.get("color") == d.get("color")) and v.get("storage") == st]
        v = vs[0] if vs else {}
        label = " ".join(x for x in [_short_name(prod.get("name")), d.get("color"), st] if x)
        return _order_checkout(phone, label, v.get("price"), prod.get("permalink"))

    # ── ניתוב תפריט ──
    if rid == "status" or ("סטטוס" in low and "הזמנ" in low):
        wa.send_text(phone, "מה מספר ההזמנה? (ספרות בלבד)\nאו כתוב/י *תפריט* לחזרה.")
        db.bot_session_set(phone, "await_order_number", {})
        return
    if rid == "branches" or "סניפ" in low:
        wa.send_text(phone, BRANCHES)
        return _menu_tail(phone)
    if rid == "shipping" or "משלוח" in low:
        wa.send_text(phone, SHIPPING)
        return _menu_tail(phone)
    if rid == "new" or rid == "search_again":
        wa.send_text(phone, "מה שם המוצר שאתה מחפש? 🔍\n(למשל: אייפון 17 פרו, גלקסי S25, אוזניות JBL)"
                            "\nאו כתוב/י *תפריט* לחזרה.")
        db.bot_session_set(phone, "new_search", {})
        return
    if rid == "lab":
        return _to_agent(phone, note="פנייה למעבדה")

    # ── טקסט חופשי → חיפוש מוצר (מיידי) או אורי (לשאלות/השוואות, אם זמין) ──
    if low and low not in _GREETINGS and len(low) >= 3:
        import main
        results = main.bot_product_search(text, limit=6)
        question_like = bool(_re.search(
            r"\?|מה ההבדל|הבדל בין|השוואה|עדיף|מה מתאים|כדאי|להמליץ|המלצ|תקציב|עד \d|"
            r"יבואן|אחריות|האם|כמה עולה|מה יותר", text))
        # שאלה מורכבת או אין תוצאות → אורי (אם הגשר חי); אחרת תוצאות חיפוש
        if (question_like or not results) and _ask_uri(phone, text):
            return
        if results:
            return _new_order_results(phone, text)
        wa.send_text(phone, "לא הצלחתי למצוא 🙁 נסה/י שם מוצר אחר, או כתוב/י *נציג*.")
        return
    _menu(phone)


_GREETINGS = {"היי", "שלום", "הי", "הייי", "hello", "hi", "hey", "בוקר טוב",
              "ערב טוב", "צהריים טובים", "מה קורה", "מה נשמע", "שלום רב", "אהלן"}

# מילות יציאה גלובליות — חוזרות לתפריט מכל מצב (גם מתוך חיפוש/הזמנה)
_ESCAPE = {"תפריט", "menu", "חזור", "חזרה", "ביטול", "בטל", "התחל", "מהתחלה",
           "start", "צא", "יציאה", "ראשי", "עצור", "די"}


def _uri_alive() -> bool:
    """האם גשר אורי על המק חי (heartbeat מ-2.5 הדקות האחרונות)."""
    import db
    last = db.sales_state_get("uri_bridge_ping")
    if not last:
        return False
    from datetime import datetime
    try:
        t = datetime.fromisoformat(str(last))
        return (datetime.now(t.tzinfo) - t).total_seconds() < 150
    except Exception:  # noqa: BLE001
        return False


def _ask_uri(phone, question) -> bool:
    """מנתב שאלה לאורי (תשובה תישלח אוטומטית כשתחזור). False אם הגשר לא זמין.
    מצרף מוצרים מועמדים מהחיפוש כדי שאורי יענה מהר — בלי סבב כלים."""
    if not _uri_alive():
        return False
    import main
    wa.send_text(phone, "רגע, בודק/ת עבורך 🔍")
    q = question or ""
    try:
        cands = main.bot_product_search(question, limit=6) or []
    except Exception:  # noqa: BLE001
        cands = []
    if cands:
        lines = "\n".join(
            f"- {c.get('name')} | ₪{c.get('price')} | "
            f"{'במלאי' if c.get('stock_status') == 'instock' else 'אזל'} | {c.get('permalink')}"
            for c in cands)
        q += ("\n\n[מוצרים מועמדים (כבר חיפשתי באתר — השתמש באלה, אל תחפש שוב "
              f"אלא אם אף אחד לא מתאים):\n{lines}]")
    db.uri_job_add(phone, q, source="bot")
    return True


import re as _re

_GENERIC_PREFIX = ["סמארטפון", "טלפון סלולרי", "טלפון סלולארי", "מכשיר סלולרי",
                   "מכשיר סלולארי", "טלפון סלולרי -", "מכשיר", "טלפון", "שעון חכם",
                   "סלולרי", "אוזניות אלחוטיות", "אוזניות"]


def _short_name(name: str) -> str:
    """שם מוצר קצר וקריא לרשימה — מסיר תחיליות גנריות ('סמארטפון...') כדי שהדגם
    יופיע בהתחלה ולא ייחתך. למשל 'סמארטפון OPPO X9 Pro 5G' → 'OPPO X9 Pro 5G'."""
    n = _re.sub(r"\s+", " ", (name or "")).strip()
    for pre in _GENERIC_PREFIX:
        if n.startswith(pre + " ") or n == pre:
            n = n[len(pre):].strip(" -–—")
            break
    return n or (name or "מוצר")


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
        short = _short_name(p.get("name"))
        # מחיר + שארית השם (נפח/דגם) בתיאור — עוד 72 תווים של מידע
        rest = short[24:].strip() if len(short) > 24 else ""
        price_s = (f"₪{int(float(price)):,}" if price not in (None, "", "0") else "לפרטים")
        desc = f"{price_s} · {rest}"[:72] if rest else price_s
        rows.append((pid, short[:24], desc))
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
    pid = rid.split(":", 1)[1]
    btns = [(f"buy:{pid}", "🛒 הזמן עכשיו"), ("search_again", "🔍 מוצר אחר"), ("agent", "👤 נציג")]
    img = p.get("image") or ""
    try:                                   # כרטיס עם תמונה; אם נכשל — fallback לטקסט
        wa.send_buttons(phone, body, btns, header_image=img if img.startswith("http") else "")
    except Exception as e:  # noqa: BLE001
        logger.warning("product card image failed, text fallback: %s", e)
        wa.send_text(phone, body)
        wa.send_buttons(phone, "מה הלאה?", btns)
    # שומרים את המוצר לשלב הרכישה
    db.bot_session_set(phone, "viewing", {"pid": pid, "product": p})


# ── זרימת הזמנה ותשלום בצ'אט ──
def _start_order(phone, pid, sess):
    import main
    prod = (sess.get("data") or {}).get("product") or {}
    variations = main.bot_get_variations(pid)
    if not variations:                       # מוצר פשוט
        return _order_checkout(phone, _short_name(prod.get("name")),
                               prod.get("price"), prod.get("permalink"))
    colors = []
    seen = set()
    for v in variations:
        c = v.get("color")
        if c and c not in seen:
            seen.add(c)
            colors.append(c)
    if len(colors) > 1:
        rows = [(f"color:{c}", c, "") for c in colors[:10]]
        wa.send_list(phone, f"איזה צבע ל-{_short_name(prod.get('name'))}?", rows,
                     button_label="צבעים", section_title="בחר/י צבע")
        db.bot_session_set(phone, "order_color",
                           {"pid": pid, "product": prod, "variations": variations})
        return
    return _show_storage(phone, pid, prod, variations, colors[0] if colors else "")


def _show_storage(phone, pid, prod, variations, color):
    vs = [v for v in variations if (not color or v.get("color") == color)]
    storages = []
    seen = set()
    for v in vs:
        st = v.get("storage")
        if st and st not in seen:
            seen.add(st)
            storages.append(st)
    if len(storages) > 1:
        rows = [(f"storage:{st}", st, "") for st in storages[:10]]
        wa.send_list(phone, "איזה נפח אחסון?", rows, button_label="נפח", section_title="בחר/י נפח")
        db.bot_session_set(phone, "order_storage",
                           {"pid": pid, "product": prod, "variations": variations, "color": color})
        return
    v = vs[0] if vs else {}                   # וריאציה יחידה — נקבעה
    label = " ".join(x for x in [_short_name(prod.get("name")), color, v.get("storage")] if x)
    return _order_checkout(phone, label, v.get("price"), prod.get("permalink"))


def _short_link(url: str) -> str:
    """slug עברי / תווים לא-אנגליים בקישור → TinyURL ‏gm-<אנגלית מתוך ה-slug>
    (קישור עברי ארוך נראה שבור/חשוד בוואטסאפ). slug אנגלי תקין → מוחזר כמו שהוא."""
    try:
        from urllib.parse import urlsplit, quote, unquote
        path = unquote(urlsplit(url).path)
        if path.isascii():                       # slug אנגלי — קישור ישיר
            return url
        slug = path.rstrip("/").split("/")[-1]
        ascii_part = _re.sub(r"[^a-z0-9]+", "-", _re.sub(r"[^\x00-\x7f]", "", slug.lower())).strip("-")
        alias = "gm-" + (ascii_part[:40].strip("-") or "p")
        try:
            import requests as _rq
            r = _rq.get(f"https://tinyurl.com/api-create.php?url={quote(url, safe='')}&alias={alias}",
                        timeout=10)
            if r.ok and r.text.startswith("http"):
                return r.text.strip()
        except Exception:  # noqa: BLE001
            pass
        return f"https://tinyurl.com/{alias}"     # alias כבר קיים מריצה קודמת — דטרמיניסטי
    except Exception:  # noqa: BLE001
        return url


def _order_checkout(phone, label, price, permalink):
    """סגירת הזמנה — כפתור CTA שפותח את עמוד המוצר באתר (כל השדות + תשלומים +
    תשלום מאובטח). slug עברי → קישור מקוצר gm-. מחליף את איסוף-השם השביר."""
    pstr = f" (₪{int(float(price)):,})" if price not in (None, "", "0") else ""
    db.bot_session_clear(phone)
    if not permalink:
        wa.send_text(phone, f"מעולה — {label}{pstr}! נציג יחזור אליך לסגור את ההזמנה 🙏")
        return _to_agent(phone, note=f"הזמנה: {label}")
    link = _short_link(permalink)
    body = (f"מעולה — {label}{pstr}! 🛒\n\n"
            f"להשלמת ההזמנה (פרטים מלאים + עד 12 תשלומים + תשלום מאובטח) — לחצ/י על הכפתור.\n"
            f"או כתוב/י *נציג* ואחד מהצוות יסגור איתך 🙏")
    try:
        wa.send_cta_url(phone, body, "🛒 להשלמת ההזמנה", link)
    except Exception:  # noqa: BLE001 — נפילה לקישור-טקסט אם CTA נכשל
        wa.send_text(phone, f"{body}\n\n{link}")


def _ask_name(phone, order):
    price = order.get("price")
    pstr = f" (₪{int(float(price)):,})" if price not in (None, "", "0") else ""
    wa.send_text(phone, f"מעולה — {order.get('label')}{pstr} 🛒\nלשם מי ההזמנה? (שם מלא)")
    db.bot_session_set(phone, "order_name", order)


def _finalize_order(phone, name, order):
    import main
    db.bot_session_clear(phone)
    try:
        res = main.bot_create_order(order["pid"], order.get("vid") or 0,
                                    order.get("price"), name, phone)
    except Exception as e:  # noqa: BLE001
        logger.warning("bot order finalize failed: %s", e)
        wa.send_text(phone, "הייתה תקלה ביצירת ההזמנה 🙁 נציג יחזור אליך לסיים. מצטערים!")
        return _to_agent(phone, note=f"הזמנה אוטומטית נכשלה: {order.get('label')}")
    msg = (f"✅ ההזמנה נוצרה!\nהזמנה #{res.get('number')} · סה\"כ ₪{res.get('total')}\n\n"
           f"💳 להשלמת התשלום (מאובטח):\n{res.get('pay_link')}\n\n"
           f"לאחר התשלום נעדכן אותך כאן 🙏")
    wa.send_text(phone, msg)
    try:
        main._tg_admin(f"🛒 <b>הזמנה חדשה מהבוט!</b>\n#{res.get('number')} · {order.get('label')} · "
                       f"₪{res.get('total')}\n{phone} ({name})")
    except Exception:  # noqa: BLE001
        pass


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
