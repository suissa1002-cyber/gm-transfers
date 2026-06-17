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
    ("lab",      "🔧 מעבדה / תיקון", "הצעת מחיר / בדיקת סטטוס תיקון"),
    ("agent",    "👤 נציג אנושי", "מעבר לשירות אנושי"),
]
_TITLE2ID = {t: rid for rid, t, _ in MENU}

GREET = "היי תודה שפנית לגרין מובייל, רשת חנויות סלולר, גיימינג מחשבים ועוד. איך נוכל לעזור?"

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

SHIPPING = ("🚚 *אפשרויות משלוח*\n\n"
            "📦 *משלוח רגיל*\n"
            "1-6 ימי עסקים · חינם בהזמנות מעל 499₪\n\n"
            "⚡ *משלוח באותו היום* — 89₪\n"
            "להזמנות שנכנסות עד 13:00 בימים א׳-ה׳\n"
            "באזורי החלוקה באר שבע–חיפה\n\n"
            "📍 *נקודת מסירה — תל אביב*\n"
            "להזמנות שנכנסות עד 17:00\n"
            "מסירה למחרת · א׳-ה׳ · 10:00-16:00\n"
            "📌 י.ל פרץ 35, תל אביב\n\n"
            "🏬 *איסוף עצמי מהסניף*\n"
            "בחירת הסניף לאיסוף מתבצעת במהלך ההזמנה\n\n"
            "📧 *משלוח דיגיטלי*\n"
            "מייל / SMS — למוצרים דיגיטליים")


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
    # כניסה מעמוד מוצר: כפתור הוואטסאפ (Chaty) באתר ממלא "...לגבי המוצר: <שם> — <תיאור>".
    # מזהים, מחלצים את שם המוצר, וקופצים ישר אליו — במקום תפריט גנרי. המרה גבוהה.
    if not rid and "לגבי המוצר" in low:
        return _entry_product_flow(phone, text)
    # ברכה (גם בתוך מצב חיפוש) → תפריט, במקום "לא מצאתי 'היי'"
    if not rid and _is_greeting(low):
        return _menu(phone)

    # ── זרימות תלויות-מצב ──
    if state == "await_order_number" and not rid:
        return _order_status(phone, text)
    if state == "await_repair_id" and not rid and text:
        return _repair_status(phone, fix_id="".join(ch for ch in text if ch.isdigit()))
    if state == "await_repair_model" and not rid and text:
        return _repair_quote_result(phone, text)
    if state == "await_repair_part" and not rid and text:
        return _repair_part_result(phone, text)
    if state == "new_search" and not rid:
        return _new_order_results(phone, text)
    if state == "new_pick" and rid.startswith("prod:"):
        return _product_card(phone, rid, sess.get("data") or {})
    # כפתור "בדיקת סטטוס הזמנה" מטמפלייט קבלת ההזמנה — ה-payload נושא את מס' ההזמנה,
    # אז מציגים את סטטוס ההזמנה הזו אוטומטית, בלי שהלקוח יזין מספר.
    if rid.startswith("ordstatus:"):
        return _order_status(phone, rid.split(":", 1)[1])
    # כפתור "קיבלתי את ההזמנה" (טמפלייט הפצה) — מסמן 'נמסרה' ושולח את טמפלייט חוו"ד.
    # אם חוו"ד נשלחה — היא ההודעה (כוללת תודה); אחרת תודה קצרה כ-fallback.
    if rid.startswith("received:"):
        import main
        if not main.bot_confirm_received(rid.split(":", 1)[1]):
            wa.send_text(phone, "תודה שאישרת את קבלת ההזמנה! 🙏 שמחים שהמשלוח הגיע אליך.")
        return
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
    if state == "order_attr" and not rid and text and (sess.get("data") or {}).get("ask_attr"):
        # הקלדת שם הערך (כשיש >10 ולא נכנס לרשימה, או פשוט הקליד) — התאמה לכל הערכים
        d = sess.get("data") or {}
        attr = d.get("ask_attr")
        allopts = d.get("all_options") or [[o, o] for o in (d.get("ask_options") or [])]
        tl = text.strip().lower()
        exact = [o for o in allopts if (o[1] or "").lower() == tl]
        partial = [o for o in allopts if tl in (o[1] or "").lower() and o not in exact]
        match = exact + partial
        if match:
            chosen = d.get("chosen") or {}
            chosen[attr] = match[0][0]
            d["chosen"] = chosen
            for key_ in ("ask_attr", "ask_options", "all_options"):
                d.pop(key_, None)
            db.bot_session_set(phone, "order_attr", d)
            return _ask_next_attr(phone)
        wa.send_text(phone, f"לא זיהיתי '{text}' 🤔 כתוב/י את השם המדויק מהרשימה, "
                            f"או בחר/י מהרשימה למעלה.")
        return

    # ── ניתוב תפריט ──
    if rid == "status" or ("סטטוס" in low and "הזמנ" in low):
        return _order_status_auto(phone)
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
        wa.send_buttons(phone, "🔧 *מעבדה / תיקון*\nבמה אפשר לעזור?",
                        [("repair_status", "🔍 סטטוס תיקון"),
                         ("repair_quote", "💰 הצעת מחיר"), ("menu", "↩️ תפריט")])
        return
    if rid == "repair_quote":
        wa.send_text(phone, "💰 *הצעת מחיר לתיקון*\nכתוב/י את דגם המכשיר "
                            "(למשל: אייפון 13, גלקסי A54, iPhone 14 Pro):")
        db.bot_session_set(phone, "await_repair_model", {})
        return
    if rid == "repair_status":
        return _repair_status(phone)
    if rid.startswith("rmodel:"):
        d = (sess.get("data") or {})
        idx = int(rid.split(":", 1)[1]) if rid.split(":", 1)[1].isdigit() else -1
        cands = d.get("cands") or []
        if 0 <= idx < len(cands):
            return _repair_ask_part(phone, cands[idx])
        return _menu(phone)
    if rid.startswith("rpart:") and state == "await_repair_part":
        d = (sess.get("data") or {})
        parts = d.get("parts") or []
        idx = int(rid.split(":", 1)[1]) if rid.split(":", 1)[1].isdigit() else -1
        if d.get("device") and 0 <= idx < len(parts):
            return _send_repair_prices(phone, d["device"], [parts[idx]])
        return _menu(phone)

    # ── טקסט חופשי → שאלה שיחתית לאורי, אחרת חיפוש מוצר (שמפיל לאורי אם ריק) ──
    if low and low not in _GREETINGS and len(low) >= 3:
        question_like = bool(_re.search(
            r"\?|מה ההבדל|הבדל בין|השוואה|עדיף|מה מתאים|כדאי|להמליץ|המלצ|תקציב|עד \d|"
            r"יבואן|אחריות|האם|כמה עולה|מה יותר", text))
        # שאלה מורכבת → אורי ישירות. אחרת → חיפוש חכם (שכבר מפיל לאורי אם ריק).
        if question_like and _ask_uri(phone, text, wamid):
            return
        return _new_order_results(phone, text)
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
    try:                                         # מועמדים מהמנוע החכם (אותו מוח כמו הבוט)
        cands = (main.bot_smart_search(question, limit=6) or {}).get("results") or []
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


_BRANDS = ("xiaomi", "redmi", "poco", "apple", "iphone", "ipad", "macbook", "samsung",
           "galaxy", "oppo", "realme", "honor", "vivo", "oneplus", "google", "pixel",
           "motorola", "nokia", "jbl", "sony", "anker", "bose", "razer", "logitech",
           "nothing", "huawei", "lenovo", "asus", "nubia", "redmagic", "tecno",
           "infinix", "marshall", "doogee", "ulefone")
_BRAND_HE = {"אנקר": "anker", "סמסונג": "samsung", "אפל": "apple", "שיאומי": "xiaomi",
             "סוני": "sony", "וואווי": "huawei", "אופו": "oppo", "ריאלמי": "realme",
             "הונור": "honor", "וויוו": "vivo", "מרשל": "marshall", "בוס": "bose"}


def _name_parts(name: str, brand: str = ""):
    """(כותרת-דגם, תיאור): מחלץ את חלק המותג+דגם — איפה שהוא בשם, גם אם בסוף —
    לכותרת, והשאר לתת-כותרת. 'טלפון... Xiaomi Redmi 15C' → ('Redmi 15C', 'עם מסך...').
    כך הכותרת תמיד הדגם, לא תיאור גנרי. brand = המותג האמיתי של המוצר (taxonomy) —
    אם סופק, מאתרים אותו בשם במדויק (עובד לכל מותג בחנות, בלי תלות ברשימה הקשיחה)."""
    n = _re.sub(r"\s+", " ", (name or "")).strip()
    for pre in _GENERIC_PREFIX:
        if n.startswith(pre + " ") or n == pre:
            n = n[len(pre):].strip(" -–—")
            break
    low = n.lower()
    positions = []
    # 1) המותג האמיתי של המוצר (Turtle Beach, SteelSeries... — כל מה שבחנות)
    b = (brand or "").lower().strip()
    if b and b in low:
        if low.startswith(b):
            positions.append(0)
        for sep in (" ", "-", "–", "—"):
            p = low.find(sep + b)
            if p >= 0:
                positions.append(p + 1)
    # 2) נפילה לרשימת המותגים הקשיחה (כשאין מותג משויך / לא נמצא בשם)
    for b2 in _BRANDS:
        if low.startswith(b2):
            positions.append(0)
        for sep in (" ", "-", "–", "—"):
            p = low.find(sep + b2)
            if p >= 0:
                positions.append(p + 1)
    pos = min(positions) if positions else -1
    if pos > 0:                       # הדגם באמצע/בסוף — הכותרת ממנו
        title, desc = n[pos:].strip(), n[:pos].strip(" -–—,")
    else:                             # הדגם בהתחלה (או אין מותג מזוהה)
        title, desc = n, ""
    for redundant in ("Xiaomi Redmi", "Apple iPhone", "Samsung Galaxy"):
        if title.lower().startswith(redundant.lower()):
            title = title[len(redundant.split(" ", 1)[0]):].strip()
            break
    return (title or (name or "מוצר")), desc


def _short_name(name: str, brand: str = "") -> str:
    """שם דגם קצר וקריא לכותרת (ראה _name_parts)."""
    return _name_parts(name, brand)[0]


def _entry_product_name(text: str) -> str:
    """מחלץ שם מוצר מהודעת כניסה מעמוד מוצר: 'שלום, אני מתעניין/ת לגבי המוצר: <שם> —
    <תיאור>'. חותך את התיאור (אחרי —/–/-), מנקה תווים נסתרים (zero-width/כיווניות)
    ששוברים את השם (למשל 'Samsung'), ומכווץ רווחים."""
    m = _re.search(r"לגבי המוצר:\s*(.+)", text or "", _re.S)
    if not m:
        return ""
    name = m.group(1)
    name = _re.split(r"[\n\r]|—|–|\s-\s", name)[0]               # חותך בתיאור
    name = _re.sub(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]", "", name)  # tavim nistarim
    name = _re.sub(r"\s+", " ", name).strip(" -–—:·")
    # דגמים באתר תמיד באנגלית. אם יש לטינית — מצמצמים לטווח מהאות הלטינית הראשונה
    # ועד התו הלטיני/ספרה האחרון (מותג+דגם+מפרט כמו 256GB), וזורקים מילות קטגוריה
    # ('אוזניות', 'סמארטפון') ותיאור בעברית מסביב → התאמת חיפוש מדויקת בהרבה.
    core = _re.search(r"[A-Za-z].*[A-Za-z0-9]", name)
    if core and len(core.group(0)) >= 2:
        name = core.group(0).strip()
    return name[:80]


def _entry_product_flow(phone, text):
    """כניסה מעמוד מוצר → מציג ישר את המוצר. התאמה יחידה → כרטיס מוצר מלא; כמה
    תוצאות → רשימה לבחירה; אין → אורי/הזרימה הרגילה. כך כל קליק מעמוד מוצר באתר
    הופך מיד למעורבות במוצר הספציפי, לא לתפריט גנרי."""
    import main
    name = _entry_product_name(text)
    if not name:
        return _menu(phone)
    try:
        results = (main.bot_smart_search(name, limit=8) or {}).get("results") or []
    except Exception:  # noqa: BLE001
        results = []
    if len(results) == 1:                       # התאמה ודאית → ישר לכרטיס
        top = results[0]
        pid = f"prod:{top['id']}"
        data = {pid: {"name": top.get("name"), "price": top.get("price"),
                      "permalink": top.get("permalink"), "sku": top.get("sku"),
                      "stock": top.get("stock_status"), "type": top.get("type"),
                      "image": top.get("image"), "brand": top.get("brand")},
                "__q": name}
        db.bot_session_set(phone, "new_pick", data)
        wa.send_text(phone, "מצוין! הנה המוצר שהתעניינת בו 👇")
        return _product_card(phone, pid, data)
    return _new_order_results(phone, name)      # 0/רבים → רשימה או 'לא נמצא'/אורי


def _price_label(p, prefix=""):
    """מחיר לתצוגה. מוצר variable (נפחים/וריאציות שמשנים מחיר) → 'החל מ-' כי המחיר
    מ-WC הוא המינימום; מוצר פשוט → המחיר המדויק. מחזיר 'לפרטים' אם אין מחיר."""
    price = p.get("price")
    if price in (None, "", "0"):
        return "לפרטים"
    amt = f"₪{int(float(price)):,}"
    return (f"{prefix}החל מ-{amt}" if p.get("type") == "variable" else f"{prefix}{amt}")


def _new_order_results(phone, query):
    """חיפוש מוצר חי → רשימת תוצאות עם מחירים. רשימת WhatsApp מוגבלת ל-10 שורות
    (מטא), אז מציגים עד 9 מוצרים + שורת חיפוש; אם יש יותר — מציינים ומציעים לצמצם."""
    import main
    sr = main.bot_smart_search(query, limit=20)
    all_results = sr.get("results") or []
    meta = sr.get("meta") or {}
    results = all_results[:9]
    if not results:
        # אין תוצאות → קודם כל אורי (AI) מטפל אם הוא חי — מבין ניסוח, ממליץ חלופות,
        # משוחח. רק אם אורי לא זמין נופלים להודעה הישרה.
        if _ask_uri(phone, query):
            return
        note = meta.get("note") or ""
        if note.startswith("no_brand:"):        # מותג שצוין אך אין לו מוצר — תשובה ישרה
            bn = note.split(":", 1)[1]
            cat = meta.get("cat")
            msg = f"לא מצאתי מוצרי *{bn}*"
            msg += f" בקטגוריית {cat} 🙁" if cat else " 🙁"
            msg += ("\nרוצה לראות חלופות? כתוב/י את שם הקטגוריה בלבד "
                    "(למשל 'אוזניות'), או *נציג* לאדם.")
            wa.send_text(phone, msg)
        else:
            wa.send_text(phone, f"לא מצאתי תוצאות ל'{query}' 🙁\n"
                                f"נסה/י שם מוצר אחר (למשל: אייפון 17, גלקסי S25), "
                                f"או *תפריט* לחזרה / *נציג* לאדם.")
        return
    rows, data = [], {}
    for p in results:
        pid = f"prod:{p['id']}"
        price = p.get("price")
        title, desc_extra = _name_parts(p.get("name"), p.get("brand"))   # דגם לכותרת, תיאור לתת-כותרת
        extra = desc_extra or (title[24:].strip() if len(title) > 24 else "")
        price_s = _price_label(p)            # 'החל מ-' למוצר עם וריאציות שמשנות מחיר
        desc = f"{price_s} · {extra}"[:72] if extra else price_s
        rows.append((pid, title[:24], desc))
        data[pid] = {"name": p.get("name"), "price": price, "permalink": p.get("permalink"),
                     "sku": p.get("sku"), "stock": p.get("stock_status"), "type": p.get("type"),
                     "image": p.get("image"), "brand": p.get("brand")}
    rows.append(("search_again", "🔍 חיפוש חדש", ""))
    total = len(all_results or [])
    if total > len(results):     # יש יותר ממה שנכנס ברשימה (מגבלת 10 שורות בוואטסאפ)
        hdr = (f"מצאתי {total} תוצאות ל'{query}' — הנה {len(results)} הרלוונטיות. "
               f"לצמצום, הוסף/י דגם או סדרה (למשל JBL Flip / Charge). בחר/י לפרטים:")
    else:
        hdr = f"מצאתי {total} תוצאות ל'{query}'. בחר/י לפרטים:"
    wa.send_list(phone, hdr, rows, button_label="לתוצאות", section_title="תוצאות חיפוש")
    data["__q"] = query                  # שומרים את השאלה — לרמז צבע/וריאציה בהזמנה
    db.bot_session_set(phone, "new_pick", data)
    # כפתור "עוד באתר" — מבוסס על ה**קטגוריה המשותפת** של המוצרים שנמצאו (לא חיפוש
    # מילולי שנשבר על שאילתות עבריות שאינן בכותרות). ככה הקישור תמיד מוביל לתוצאות
    # אמיתיות וניתנות לסינון, גם אם הלקוח כתב מונח שלא קיים מילולית באתר.
    # כפתור "עוד באתר" — רק כשהבוט מצא יותר ממה שנכנס ברשימה (מגבלת 10 שורות).
    # היעד: הקטגוריה המשותפת של המוצרים שנמצאו + סינון מותג אם היצרן ברור בשאלה
    # (כך "אוזניות anker" → קטגוריית אוזניות + מותג Anker, תוצאה מדויקת).
    cat_freq = {}
    for p in (all_results or []):
        for sl in {c.get("slug") for c in (p.get("cats") or []) if c.get("slug")}:
            cat_freq[sl] = cat_freq.get(sl, 0) + 1
    low2 = (query or "").lower()
    brand = next((b for b in _BRANDS if b in low2), None) \
        or next((en for he, en in _BRAND_HE.items() if he in (query or "")), None)
    best = (main.bot_best_category(cat_freq, min_count=len(results))
            if (cat_freq and total > len(results)) else None)
    if best:
        try:
            # כתובת קנונית של ארכיון הקטגוריה (בלי redirect → נטען בלחיצה ראשונה
            # בדפדפן הפנימי של וואטסאפ). fallback ל-query string אם ה-permalink לא נמצא.
            url = main._wc_term_link("product_cat", best["slug"]) \
                or f"https://greenmobile.co.il/?product_cat={best['slug']}"
            lbl = best["name"]
            if brand:
                # WC 10.x = מותגים native (taxonomy product_brand). pwb-brand הישן לא
                # מסנן. product_brand כפרמטר עושה AND עם הקטגוריה, בלי לשבור את הקנוניות.
                sep = "&" if "?" in url else "?"
                url += f"{sep}product_brand={brand}"
                lbl = f"{best['name']} · {brand.upper()}"
            wa.send_cta_url(phone, f"לעיון בכל *{lbl}* באתר (וסינון):", "🔎 עוד באתר", url)
        except Exception:  # noqa: BLE001
            pass


def _product_card(phone, rid, data):
    """כרטיס מוצר אמיתי — תמונה + שם + מחיר חי + זמינות + קישור רכישה + כפתורים."""
    p = (data or {}).get(rid)
    if not p:
        wa.send_text(phone, "לא מצאתי את הפריט, ננסה שוב 🙏")
        return _menu(phone)
    price = p.get("price")
    lines = [f"*{p.get('name')}*"]
    if price not in (None, "", "0"):
        lines.append(f"💰 מחיר: {_price_label(p)}")
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
    # שומרים את המוצר לשלב הרכישה (כולל שאלת הלקוח — לרמז צבע/וריאציה)
    db.bot_session_set(phone, "viewing", {"pid": pid, "product": p, "q": (data or {}).get("__q", "")})


# ── זרימת הזמנה ותשלום בצ'אט ──
def _start_order(phone, pid, sess):
    import main
    prod = (sess.get("data") or {}).get("product") or {}
    variations = main.bot_get_variations(pid)
    if not variations:                       # מוצר פשוט
        return _order_checkout(phone, _short_name(prod.get("name"), prod.get("brand")),
                               prod.get("price"), pid, prod.get("permalink"))
    db.bot_session_set(phone, "order_attr",
                       {"pid": pid, "product": prod, "variations": variations, "chosen": {},
                        "q": (sess.get("data") or {}).get("q", "")})   # רמז הצבע מהשאלה
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
        if len(opts) > 1:                     # יש בחירה
            ql = (d.get("q") or "").lower()
            # רמז מהשאלה: ערך שהלקוח כבר ציין (למשל 'שחור' ב"שלט שחור") — מותאם לפי שם
            hinted = [o for o in opts if o[1] and o[1].lower() in ql]
            if len(hinted) == 1:              # ציין ערך יחיד וברור → בחירה אוטומטית, בלי לשאול
                chosen[attr] = hinted[0][0]
                d["chosen"] = chosen
                db.bot_session_set(phone, "order_attr", d)
                return _ask_next_attr(phone)
            # מיון: הרמוזים קודם (כדי שלא ייחתכו ב-10 הראשונים), ואז השאר. רשימת WhatsApp ≤10.
            ordered = hinted + [o for o in opts if o not in hinted]
            show = ordered[:10]
            rows = [(f"pick:{i}", show[i][1][:24], "") for i in range(len(show))]
            d["ask_attr"], d["ask_options"], d["chosen"] = attr, [o[0] for o in show], chosen
            d["all_options"] = [list(o) for o in opts]   # כל הערכים — לבחירה בהקלדה
            db.bot_session_set(phone, "order_attr", d)
            msg = f"בחר/י {attr} ל-{_short_name(prod.get('name'), prod.get('brand'))}:"
            if len(opts) > 10:                # יותר מ-10 ערכים — וואטסאפ מציג רק 10
                msg += f"\n(יש {len(opts)} אפשרויות — אם לא רואה את מה שרצית, כתוב/י את השם)"
            wa.send_list(phone, msg, rows, button_label="בחירה", section_title=attr[:24])
            return
        if len(opts) == 1:                    # ערך יחיד — אוטומטי, בלי לשאול
            chosen[attr] = opts[0][0]
    v = matching[0] if matching else {}       # כל התכונות נקבעו → סגירה
    disp = v.get("attrs_disp") or {}
    label = " ".join([_short_name(prod.get("name"), prod.get("brand"))]
                     + [disp.get(a, "") for a in order if (v.get("attrs") or {}).get(a)])
    return _order_checkout(phone, label.strip(), v.get("price"),
                           d.get("pid"), prod.get("permalink"), v)


def _short_link(url: str) -> str:
    """slug עברי / תווים לא-אנגליים בקישור → TinyURL gm-<אנגלית מתוך ה-slug>
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
    wa.send_buttons(phone, "אוכל לעזור במשהו נוסף?", [("menu", "↩️ לתפריט"), ("agent", "👤 נציג")])


def _to_agent(phone, note: str = ""):
    # 2 הודעות ברצף: אישור קבלה + תיאום ציפיות לגבי נציג אנושי
    wa.send_text(phone, "תודה, פנייתך התקבלה בהצלחה אחד מנציגינו יחזור אליך בהקדם האפשרי.")
    wa.send_text(phone, "😊 לפני שניפרד אני רוצה לתאם ציפיות. נציגי השירות שלנו הם בני אדם "
                        "(לא כמוני) ולכן הם נותנים כעת את תשומת ליבם ללקוחות קודמים. זה אומר "
                        "שאולי ייקח קצת זמן עד שיענו לך. תודה מראש על הסבלנות")
    db.bot_session_set(phone, "agent", {"note": note})
    try:                                  # מסמן 'אנושי' שהבוט לא יקפוץ + מתריע
        import main
        main._tg_admin(f"👤 <b>לקוח ביקש נציג (בוט native)</b>\n{phone}{(' · ' + note) if note else ''}")
    except Exception:  # noqa: BLE001
        pass


_REPAIR_EMOJI = {"ממתין": "⏳", "תוקן": "✅", "נמסר ללקוח": "📦",
                 "נשלח למעבדה חיצונית": "🔧", "נישלח לאחריות": "🛡️", "מושבת": "🚫"}


def _repair_status(phone, fix_id=None):
    """סטטוס תיקון. כרגע ה-API של NewOrder (/api/Fixes) לא תומך בשליפת תיקון בודד
    לפי טלפון/מספר (אין פרמטר סינון, ו-status=-1 מחזיר 500) — לכן עד שזה ייפתר אצלם
    מעבירים לנציג עם הסבר נעים, במקום הודעת 'לא נמצא' מטעה."""
    import main
    results = main.bot_repair_status(phone=("" if fix_id else phone), fix_id=fix_id)
    if not results:
        wa.send_text(phone, "בדיקת סטטוס תיקון מתבצעת ישירות מול צוות המעבדה 🔧\n"
                            "אני מעביר אותך לנציג שיבדוק עבורך את הסטטוס המדויק 🙏")
        return _to_agent(phone, note=(f"סטטוס תיקון #{fix_id}" if fix_id else "סטטוס תיקון"))
    _d = lambda x: (x or "").split(" ")[0]   # תאריך בלבד (בלי שעה)
    blocks = []
    for r in results[:5]:
        em = _REPAIR_EMOJI.get(r["status"], "🔧")
        seg = [f"{em} *{r['device'] or 'מכשיר'}* — תיקון #{r['fixId']}",
               f"סטטוס: *{r['status']}*"]
        if r.get("created"):
            seg.append(f"📅 התקבל: {_d(r['created'])}")
        if r["status"] == "נמסר ללקוח" and r.get("delivered"):
            seg.append(f"📦 נמסר: {_d(r['delivered'])}")
        elif r.get("estimated"):
            seg.append(f"💰 הערכת עלות: ₪{r['estimated']}")
        blocks.append("\n".join(seg))
    msg = "\n\n".join(blocks)
    if len(results) > 5:
        msg += f"\n\n(ועוד {len(results) - 5} תיקונים — לפרטים פנה/י לנציג)"
    wa.send_text(phone, msg)
    _menu_tail(phone)
    db.bot_session_set(phone, "menu", {})


_MODEL_BRANDS = {"iphone": "iPhone", "ipad": "iPad", "airpods": "AirPods",
                 "macbook": "MacBook", "imac": "iMac", "watch": "Watch"}
_MODEL_WORDS = {"pro": "Pro", "max": "Max", "plus": "Plus", "mini": "Mini",
                "se": "SE", "fe": "FE", "ultra": "Ultra", "lite": "Lite",
                "note": "Note", "gen": "Gen", "with": "With", "anc": "ANC",
                "edge": "Edge", "neo": "Neo", "air": "Air"}


def _pretty_model(name: str) -> str:
    """שם דגם לתצוגה: מותג נכון (iPhone), קוד דגם ב-CAPS (A16), מילים — אות ראשונה."""
    out = []
    for w in (name or "").split():
        wl = w.lower()
        if wl in _MODEL_BRANDS:
            out.append(_MODEL_BRANDS[wl])
        elif wl in _MODEL_WORDS:
            out.append(_MODEL_WORDS[wl])
        elif _re.search(r"\d", w) and _re.search(r"[a-zA-Z]", w):
            out.append(w.upper())            # a16 / 4g/5g / s23
        elif w.isascii() and w.isalpha():
            out.append(w.capitalize())       # galaxy → Galaxy
        else:
            out.append(w)                    # מספרים / + / עברית
    return " ".join(out)


_TIER_LABEL = {"אולד": "חילופי OLED", "oled": "חילופי OLED"}   # 'אולד' בגיליון = חילופי OLED
_REPAIR_ICON = {"מסך": "🖥️", "סוללה": "🔋", "שקע": "🔌", "גב": "🪟",
                "מצלמה אחורית": "📷", "מצלמה קדמית": "🤳", "מסגרת קומפלט": "🔲",
                "עדשות": "🔎", "אפכרסת": "🔊", "כפתור בית": "⚪", "הדלקה": "⏻",
                "ווליום": "🔉", "צלצלן": "🔔"}


def _repair_quote_result(phone, text):
    """אחרי שהלקוח כתב דגם — עובר לשאלת מהות התיקון, או רשימת דגמים אם רב-משמעי."""
    import main
    cands = main.bot_repair_quote(text)
    if not cands:
        wa.send_text(phone, f"לא מצאתי מחירון לדגם '{text}' 🤔\n"
                            f"נסה/י שם מדויק (למשל: אייפון 13, גלקסי A12), "
                            f"או *נציג* לבירור.")
        return
    if len(cands) == 1:
        return _repair_ask_part(phone, cands[0])
    rows = [(f"rmodel:{i}", _pretty_model(cands[i]["display"])[:24], "")
            for i in range(min(len(cands), 9))]
    db.bot_session_set(phone, "await_repair_model", {"cands": cands})
    wa.send_list(phone, "נמצאו כמה דגמים — איזה מהם?", rows,
                 button_label="בחר/י דגם", section_title="דגמים")


def _repair_ask_part(phone, device):
    """אחרי שזוהה הדגם — שואל מה צריך לתקן (לא מציג את כל המחירון)."""
    db.bot_session_set(phone, "await_repair_part", {"device": device})
    wa.send_text(phone, f"📱 *{_pretty_model(device['display'])}* — מה צריך לתקן?\n"
                        f"(למשל: מסך, סוללה, שקע טעינה, גב, מצלמה)")


def _repair_part_result(phone, text):
    """מזהה את מהות התיקון שהלקוח כתב ומציג רק את הסעיפים הרלוונטיים."""
    import main
    d = (db.bot_session_get(phone).get("data") or {})
    device = d.get("device")
    if not device:
        return _menu(phone)
    matched = main.bot_repair_match_part(device, text)
    if matched:
        reps = device.get("repairs") or {}
        priced = [r for r in matched if any(x.get("price") for x in (reps.get(r) or []))]
        missing = [r for r in matched if r not in priced]
        if priced:
            return _send_repair_prices(phone, device, priced, missing=missing)
        # סוג תיקון תקין אך אין לו מחיר לדגם הזה (טרם עודכן בגיליון)
        names = " / ".join(matched)
        wa.send_text(phone, f"המחיר ל{names} ב-{_pretty_model(device['display'])} עדיין "
                            f"לא עודכן 🛠️\nכתוב/י *נציג* ונשמח לתת לך הצעת מחיר.")
        db.bot_session_set(phone, "menu", {})
        return
    # לא זוהה — מציג את סוגי התיקון הזמינים לדגם כרשימה ללחיצה
    parts = [r for r, t in (device.get("repairs") or {}).items()
             if any(x.get("price") for x in t)]
    if not parts:
        return _to_agent(phone, note=f"תיקון {device['display']}")
    rows = [(f"rpart:{i}", f"{_REPAIR_ICON.get(parts[i], '🔧')} {parts[i]}"[:24], "")
            for i in range(min(len(parts), 9))]
    d["parts"] = parts
    db.bot_session_set(phone, "await_repair_part", d)
    wa.send_list(phone, f"לא זיהיתי '{text}' 🤔 מה לתקן ב-{_pretty_model(device['display'])}?",
                 rows, button_label="בחר/י תיקון", section_title="סוגי תיקון")


def _send_repair_prices(phone, device, reps=None, missing=None):
    """מציג מחיר רק לסוגי התיקון שנבחרו (reps) — לא את כל המחירון."""
    items = reps or list((device.get("repairs") or {}).keys())
    lines = [f"🔧 *{_pretty_model(device['display'])}* — הצעת מחיר:"]
    for rep in items:
        tiers = (device.get("repairs") or {}).get(rep) or []
        priced = sorted([t for t in tiers if t.get("price")], key=lambda t: t["price"])
        if not priced:
            continue
        ic = _REPAIR_ICON.get(rep, "🔧")
        lines.append(f"\n{ic} *{rep}*:")
        for t in priced:                     # רמה אחת בכל שורה
            label = _TIER_LABEL.get((t.get("tier") or "").lower(), t.get("tier") or "מחיר")
            lines.append(f"• {label} — ₪{t['price']:,}")
    if missing:
        lines.append(f"\nℹ️ {' / '.join(missing)} — המחיר לדגם זה עדיין לא עודכן, "
                     f"לבירור כתוב/י *נציג*.")
    lines.append("\n⚠️ הערכה לפי התיאור בלבד — המחיר הסופי נקבע לאחר בדיקת מעבדה.")
    lines.append("לתור או בירור — *נציג*.")
    wa.send_text(phone, "\n".join(lines))
    _menu_tail(phone)
    db.bot_session_set(phone, "menu", {})


def _order_status_auto(phone):
    """סטטוס הזמנה עם זיהוי אוטומטי לפי הטלפון. נמצאה הזמנה → מציג ישר (בלי לשאול
    מספר). יש כמה → מציג את הרלוונטית ומאפשר להקליד מספר אחר. אין → מבקש מספר."""
    orders = _lookup_orders_by_phone(phone)
    if not orders:
        wa.send_text(phone, "מה מספר ההזמנה? (ספרות בלבד)\nאו כתוב/י *תפריט* לחזרה.")
        db.bot_session_set(phone, "await_order_number", {})
        return
    wa.send_text(phone, orders[0]["msg"])
    if len(orders) > 1:
        nums = ", ".join(o["num"] for o in orders[1:4])
        wa.send_text(phone, f"יש לך גם הזמנות נוספות על שם זה ({nums}).\n"
                            f"לבדיקת אחת מהן — כתוב/י את מספר ההזמנה, או *תפריט* לחזרה.")
        db.bot_session_set(phone, "await_order_number", {})
        return
    db.bot_session_clear(phone)
    _menu_tail(phone)


_DONE_STATES = {"completed", "cancelled", "refunded", "failed", "trash"}


def _lookup_orders_by_phone(phone):
    """כל ההזמנות האחרונות של הלקוח לפי טלפון (התאמת 972-מנורמל מול billing.phone).
    הזמנות פעילות קודם, ואז לפי תאריך יורד. כל פריט: {num, msg}."""
    try:
        import main
        import requests as _rq
        creds = main._wc_creds()
        if not creds:
            return []
        base, k, s = creds
        target = main._il_phone(phone)
        if not target or len(target) < 9:
            return []
        local = "0" + target[3:] if target.startswith("972") else target
        found = {}
        for term in (local, target):           # מחפשים גם 05... וגם 972...
            try:
                r = _rq.get(f"{base}/wp-json/wc/v3/orders",
                            params={"search": term, "per_page": 10,
                                    "orderby": "date", "order": "desc"},
                            auth=(k, s), timeout=20)
                if not r.ok:
                    continue
                for o in r.json():
                    bp = main._il_phone((o.get("billing") or {}).get("phone"))
                    if bp and bp == target:    # התאמה ודאית — לא רק חיפוש טקסט
                        found[o.get("id")] = o
            except Exception:  # noqa: BLE001
                continue
        orders = sorted(found.values(),
                        key=lambda o: o.get("date_created") or "", reverse=True)
        orders.sort(key=lambda o: 1 if o.get("status") in _DONE_STATES else 0)
        out = []
        for o in orders[:5]:
            meta = {m.get("key"): m.get("value") for m in (o.get("meta_data") or [])}
            name = ((o.get("billing") or {}).get("first_name") or "").strip()
            num = str(o.get("number"))
            out.append({"num": num, "msg": _status_msg(o, meta, name, num)})
        return out
    except Exception as e:  # noqa: BLE001
        logging.warning("lookup orders by phone failed: %s", e)
        return []


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
        meta = {m.get("key"): m.get("value") for m in (o.get("meta_data") or [])}
        name = ((o.get("billing") or {}).get("first_name") or "").strip()
        return _status_msg(o, meta, name, num)
    except Exception as e:  # noqa: BLE001
        logger.warning("bot order lookup failed for %s: %s", num, e)
        return None


_ST_DONE = {"delivered", "completed"}
_ST_CANCEL = {"cancelled", "refunded", "failed"}
_ST_FULFILL = {"shipping-stage", "send-cargo", "order-ready", "tlv-pickup", "order-ready-pickup"}


def _status_msg(o, meta, name, num):
    """טקסט סטטוס מותאם לפי שלב ההזמנה + שיטת המשלוח (קרגו/אקספרס/איסוף סניף/נק׳ ת״א)."""
    import main
    head = f"היי {name or 'לקוח/ה יקר/ה'},\nעדכון בנוגע להזמנה מס' *{num}*\n"
    st = o.get("status")
    titles = " ".join((sl.get("method_title") or "") for sl in (o.get("shipping_lines") or []))
    is_tlv = st == "tlv-pickup" or "נקודת מסירה" in titles
    is_pickup = (not is_tlv) and "איסוף" in titles
    is_express = "אקספרס" in titles or "אותו היום" in titles
    if st in _ST_CANCEL:
        return head + "סטטוס: ההזמנה בוטלה. לכל שאלה אנחנו כאן 🙏"
    if st in _ST_DONE:
        return head + "סטטוס: הזמנתך נמסרה ✅ תודה שקנית בגרין מובייל!"
    if st in _ST_FULFILL:
        if is_tlv:
            return head + ("סטטוס: הזמנתך בדרך לנקודת המסירה 📍\n"
                           "במידה וההזמנה בוצעה עד 16:00 בימים א׳-ד׳ — תהיה מוכנה לאיסוף "
                           "*למחרת* בנקודת המסירה בתל אביב.\n"
                           "📌 כתובת: י.ל פרץ 35, תל אביב\n🕙 שעות פעילות: 10:00-16:00")
        if is_pickup:
            return head + "סטטוס: הזמנתך *מוכנה לאיסוף* מהסניף בו בחרת במהלך ההזמנה 🏬"
        if is_express:
            return head + ("סטטוס: משלוח אקספרס ⚡\n"
                           "המשלוח יימסר במסגרת בחירתך, ושליח מטעמנו יצור קשר "
                           "לתיאום מסירה לפני ההגעה.")
        msg = head + ("סטטוס: הזמנתך נמצאת *בשלב הפצה* ותימסר במסגרת ימי המשלוח "
                      "שנבחרו בהזמנה 🚚")
        cs = main._cargo_status(meta, (o.get("billing") or {}).get("email") or "")
        if cs and cs.get("track_url"):
            msg += f"\n🔗 מעקב משלוח: {cs['track_url']}"
        return msg
    return head + "סטטוס: הזמנתך נקלטה ונמצאת בטיפול — אנו מכינים אותה. נעדכן אותך בהמשך 🙏"
