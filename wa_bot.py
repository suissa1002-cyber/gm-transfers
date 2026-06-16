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

BRANCHES = ("📍 *הסניפים שלנו*\n"
            "☎️ ניתוב שיחות: 08-9350202\n\n"
            "*אשדוד · סטאר* (מתחם סטאר סנטר)\n"
            "ז'בוטינסקי 45, אשדוד\n"
            "🕘 א׳-ה׳ 09:00-21:00 · ו׳/ערב חג 09:00-15:00 · מוצ״ש: שעה מצאת השבת עד 22:00\n"
            "🔧 כולל מעבדה\n\n"
            "*אשדוד · גן העיר* (מתחם גן העיר)\n"
            "הגדוד העברי 5, אשדוד\n"
            "🕘 א׳-ה׳ 09:00-20:00 · ו׳/ערב חג 09:00-15:00\n"
            "🔧 כולל מעבדה\n\n"
            "*אשדוד · סיטי*\n"
            "הציונות 13, אשדוד\n"
            "🕘 א׳-ה׳ 09:00-21:00 · ו׳/ערב חג 09:00-15:00\n"
            "🔧 כולל מעבדה\n\n"
            "*אשדוד · עד הלום* (מתחם ADGARDEN)\n"
            "צומת עד הלום, קומה קרקע\n"
            "🕘 א׳-ה׳ 09:00-20:00 · ו׳/ערב חג 09:00-15:00\n\n"
            "*תל אביב · נקודת מסירה*\n"
            "י״ל פרץ 35, תל אביב\n"
            "🕘 א׳-ה׳ 10:00-16:00\n"
            "📦 מסירה/איסוף הזמנות + שירות לקוחות")

SHIPPING = ("🚚 *אפשרויות משלוח:*\n\n"
            "1. משלוח חינם — עד 7 ימי עסקים\n"
            "2. משלוח מהיר — 1-3 ימי עסקים (89₪)\n"
            "3. איסוף עצמי מהסניף / נקודת מסירה בת״א\n\n"
            "* משלוח חינם בהזמנות מעל 500₪")


def _menu(phone):
    wa.send_list(phone, GREET, MENU, button_label="לתפריט", section_title="במה לעזור?")
    db.bot_session_set(phone, "menu", {})


def handle(phone: str, text: str, mtype: str = "text", reply_id: str = "", wamid: str = ""):
    """נקודת הכניסה — מקבלת הודעת לקוח ומגיבה. מנוהל מצב ב-wa_bot_session.
    reply_id = id של כפתור/רשימה (ניתוב מדויק); נופלים לטקסט אם אין.
    wamid = מזהה ההודעה הנכנסת — לחיווי הקלדה בזמן המתנה לאורי."""
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
    # ברכה (גם בתוך מצב חיפוש) → תפריט, במקום "לא מצאתי 'היי'"
    if not rid and _is_greeting(low):
        return _menu(phone)

    # ── זרימות תלויות-מצב ──
    if state == "await_order_number" and not rid:
        return _order_status(phone, text)
    if state == "new_search" and not rid:
        return _new_order_results(phone, text)
    if state == "new_pick" and rid.startswith("prod:"):
        return _product_card(phone, rid, sess.get("data") or {})
    # ── זרימת הזמנה בצ'אט (וריאציה גנרית — כל תכונה שמשתנה) ──
    if rid.startswith("buy:"):
        return _start_order(phone, rid.split(":", 1)[1], sess)
    if state == "order_attr" and rid.startswith("pick:"):
        d = sess.get("data") or {}
        try:
            idx = int(rid.split(":", 1)[1])
            opts, attr = d.get("ask_options") or [], d.get("ask_attr")
            if attr and 0 <= idx < len(opts):
                chosen = d.get("chosen") or {}
                chosen[attr] = opts[idx]
                d["chosen"] = chosen
                d.pop("ask_attr", None)
                d.pop("ask_options", None)
                db.bot_session_set(phone, "order_attr", d)
        except Exception:  # noqa: BLE001
            pass
        return _ask_next_attr(phone)

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
        if (question_like or not results) and _ask_uri(phone, text, wamid):
            return
        if results:
            return _new_order_results(phone, text)
        wa.send_text(phone, "לא הצלחתי למצוא 🙁 נסה/י שם מוצר אחר, "
                            "או *תפריט* לחזרה / *נציג* לאדם.")
        return
    _menu(phone)


_GREETINGS = {"היי", "שלום", "הי", "הייי", "hello", "hi", "hey", "בוקר טוב",
              "ערב טוב", "צהריים טובים", "מה קורה", "מה נשמע", "שלום רב", "אהלן"}

# מילות יציאה גלובליות — חוזרות לתפריט מכל מצב (גם מתוך חיפוש/הזמנה)
_ESCAPE = {"תפריט", "menu", "חזור", "חזרה", "ביטול", "בטל", "התחל", "מהתחלה",
           "start", "צא", "יציאה", "ראשי", "עצור", "די"}
_GREET_WORDS = {"היי", "שלום", "הי", "הייי", "אהלן", "בוקר", "ערב", "צהריים",
                "הלו", "מה", "נשמע", "קורה", "yo", "hi", "hello", "hey", "שבת"}


def _is_greeting(text: str) -> bool:
    """ברכה/פתיח שיחה — לא חיפוש מוצר. מזהה גם 'היי בוקר טוב', 'שלום מה נשמע'."""
    t = (text or "").strip()
    if t in _GREETINGS:
        return True
    if len(t) <= 18:
        return bool(set(_re.split(r"\s+", t)) & _GREET_WORDS)
    return False


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


def _ask_uri(phone, question, wamid="") -> bool:
    """מנתב שאלה לאורי (תשובה תישלח אוטומטית כשתחזור). False אם הגשר לא זמין.
    מצרף מוצרים מועמדים מהחיפוש כדי שאורי יענה מהר — בלי סבב כלים."""
    if not _uri_alive():
        return False
    import main
    wa.send_text(phone, "רגע, בודק/ת עבורך 🔍")
    if wamid:                                    # חיווי הקלדה בזמן ההמתנה לאורי
        try:
            wa.send_typing(wamid)
        except Exception:  # noqa: BLE001
            pass
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
    """שם מוצר קצר וקריא לרשימה — מסיר תחיליות גנריות ('סמארטפון...') וכפילות מותג
    (Xiaomi לפני Redmi) כדי שהדגם המבדיל (Pro+/מסך) ייכנס ב-24 התווים של הכותרת.
    'סמארטפון Xiaomi Redmi Note 15 Pro+' → 'Redmi Note 15 Pro+'."""
    n = _re.sub(r"\s+", " ", (name or "")).strip()
    for pre in _GENERIC_PREFIX:
        if n.startswith(pre + " ") or n == pre:
            n = n[len(pre):].strip(" -–—")
            break
    # כפילות מותג — המותג מובלע בקו-המוצר; מסירים כדי לפנות מקום למבדיל
    for redundant in ("Xiaomi Redmi", "Apple iPhone", "Samsung Galaxy"):
        if n.startswith(redundant):
            n = n[len(redundant.split(" ", 1)[0]):].strip()
            break
    return n or (name or "מוצר")


def _new_order_results(phone, query):
    """חיפוש מוצר חי → רשימת תוצאות עם מחירים (התיקון מס' 1 של 'הזמנה חדשה')."""
    import main
    results = main.bot_product_search(query, limit=8)
    if not results:
        wa.send_text(phone, f"לא מצאתי תוצאות ל'{query}' 🙁\n"
                            f"נסה/י שם מוצר אחר (למשל: אייפון 17, גלקסי S25), "
                            f"או *תפריט* לחזרה / *נציג* לאדם.")
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
                               prod.get("price"), pid, prod.get("permalink"))
    db.bot_session_set(phone, "order_attr",
                       {"pid": pid, "product": prod, "variations": variations, "chosen": {}})
    return _ask_next_attr(phone)


def _ask_next_attr(phone):
    """זרימת וריאציה גנרית: שואל על כל תכונה שמשתנה (צבע / נפח / קישוריות / אחריות...)
    עד שהוריאציה נקבעת חד-משמעית, ואז סוגר הזמנה. תכונה עם ערך יחיד נבחרת אוטומטית."""
    sess = db.bot_session_get(phone)
    d = sess.get("data") or {}
    variations = d.get("variations") or []
    chosen = d.get("chosen") or {}
    prod = d.get("product") or {}
    matching = [v for v in variations
                if all((v.get("attrs") or {}).get(k) == val for k, val in chosen.items())] or variations
    order = (variations[0].get("attr_order") if variations else []) or []
    for attr in order:
        if attr in chosen:
            continue
        opts, seen = [], set()                # [(value, display), ...]
        for v in matching:
            val = (v.get("attrs") or {}).get(attr)
            disp = (v.get("attrs_disp") or {}).get(attr, val)
            if val and val not in seen:
                seen.add(val)
                opts.append((val, disp))
        if len(opts) > 1:                     # יש בחירה — שואלים את הלקוח (מציגים שם)
            rows = [(f"pick:{i}", opts[i][1][:24], "") for i in range(min(len(opts), 10))]
            d["ask_attr"], d["ask_options"], d["chosen"] = attr, [o[0] for o in opts], chosen
            db.bot_session_set(phone, "order_attr", d)
            wa.send_list(phone, f"בחר/י {attr} ל-{_short_name(prod.get('name'))}:",
                         rows, button_label="בחירה", section_title=attr[:24])
            return
        if len(opts) == 1:                    # ערך יחיד — אוטומטי, בלי לשאול
            chosen[attr] = opts[0][0]
    v = matching[0] if matching else {}       # כל התכונות נקבעו → סגירה
    disp = v.get("attrs_disp") or {}
    label = " ".join([_short_name(prod.get("name"))]
                     + [disp.get(a, "") for a in order if (v.get("attrs") or {}).get(a)])
    return _order_checkout(phone, label.strip(), v.get("price"),
                           d.get("pid"), prod.get("permalink"), v)


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


def _cart_url(parent_id, variation, parent_permalink=""):
    """קישור add-to-cart → צ'קאאוט: המוצר (כולל הוריאציה שנבחרה) כבר בעגלה, הלקוח
    נוחת ישר בצ'קאאוט. הוריאציה כוללת permalink עם ה-attributes שצריך ל-add-to-cart."""
    from urllib.parse import urlsplit
    base = ""
    src = (variation or {}).get("permalink") or parent_permalink or ""
    if src:
        sp = urlsplit(src)
        base = f"{sp.scheme}://{sp.netloc}"
    if not (base and parent_id):
        return parent_permalink or ""
    # נתיב הצ'קאאוט הקנוני (slug עברי מקודד: /מעבר-לתשלום/). חשוב: ?page_id=16 גורם
    # ל-redirect לנתיב הזה, וה-add-to-cart נדלק פעמיים (2 יחידות). הנתיב הישיר —
    # בלי redirect — מפעיל add-to-cart פעם אחת בלבד.
    co = os.getenv("WC_CHECKOUT_PATH",
                   "/%d7%9e%d7%a2%d7%91%d7%a8-%d7%9c%d7%aa%d7%a9%d7%9c%d7%95%d7%9d/")
    if variation and variation.get("id") and variation.get("permalink"):
        q = urlsplit(variation["permalink"]).query   # attribute_pa_...=...&...
        url = (f"{base}{co}?add-to-cart={parent_id}"
               f"&variation_id={variation['id']}&quantity=1")
        return url + (f"&{q}" if q else "")
    return f"{base}{co}?add-to-cart={parent_id}&quantity=1"   # מוצר פשוט


def _short_cart_link(url, parent_permalink, variation, parent_id):
    """מקצר את קישור הצ'קאאוט ל-TinyURL gm- (ייחודי לוריאציה, דטרמיניסטי). הקישור
    ארוך ומכיל עברית מקודדת — כלל: קישור עם עברית מקצרים."""
    from urllib.parse import urlsplit, quote
    slug = urlsplit(parent_permalink or "").path.rstrip("/").split("/")[-1]
    ascii_slug = _re.sub(r"[^a-z0-9]+", "-", _re.sub(r"[^\x00-\x7f]", "", slug.lower())).strip("-")[:30].strip("-")
    vid = (variation or {}).get("id") or parent_id or ""
    # 'co' = סכמת צ'קאאוט מתוקנת (page_id). alias של TinyURL לא ניתן לעדכון, ולכן
    # סיומת חדשה כשמשנים את כתובת היעד (אחרת הקישור הישן תקוע על /checkout/ השבור).
    alias = "gm-" + "-".join(x for x in [ascii_slug, "co" + str(vid)] if x)
    try:
        import requests as _rq
        r = _rq.get(f"https://tinyurl.com/api-create.php?url={quote(url, safe='')}&alias={alias}",
                    timeout=10)
        if r.ok and r.text.startswith("http"):
            return r.text.strip()
    except Exception:  # noqa: BLE001
        pass
    return f"https://tinyurl.com/{alias}"   # alias דטרמיניסטי — כבר קיים מריצה קודמת


def _order_checkout(phone, label, price, parent_id, parent_permalink, variation=None):
    """סגירת הזמנה — כפתור CTA שמוסיף את המוצר (עם הצבע/נפח שנבחרו) לעגלה ומוביל
    ישר לצ'קאאוט. נפילה: עמוד המוצר. slug עברי → קישור מקוצר gm-."""
    pstr = f" (₪{int(float(price)):,})" if price not in (None, "", "0") else ""
    db.bot_session_clear(phone)
    url = _cart_url(parent_id, variation, parent_permalink)
    if not url:
        wa.send_text(phone, f"מעולה — {label}{pstr}! נציג יחזור אליך לסגור את ההזמנה 🙏")
        return _to_agent(phone, note=f"הזמנה: {label}")
    body = (f"מעולה — {label}{pstr}! 🛒\n\n"
            f"המוצר מחכה לך בעגלה — לחצ/י להשלמת ההזמנה (פרטים + עד 12 תשלומים + "
            f"תשלום מאובטח).\nאו כתוב/י *נציג* ואחד מהצוות יסגור איתך 🙏")
    try:
        # הכפתור מסתיר את ה-URL — שולחים ישיר, בלי redirect של tinyurl (שנכשל
        # בטעינה ראשונה בדפדפן הפנימי של וואטסאפ).
        wa.send_cta_url(phone, body, "🛒 להשלמת ההזמנה", url)
    except Exception:  # noqa: BLE001 — נפילה לטקסט גלוי → אז כן מקצרים
        link = _short_cart_link(url, parent_permalink, variation, parent_id)
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
