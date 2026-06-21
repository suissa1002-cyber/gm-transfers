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


def cutover_mode() -> str:
    """מצב ה-cutover (נשמר ב-DB → החלפה מיידית בלי redeploy):
    'live' = כל הלקוחות לבוט ה-native; 'halt' = חירום, כולם לקונקטופ;
    אחרת (ברירת מחדל 'off') = רק BOT_WHITELIST (מצב בדיקה הנוכחי)."""
    try:
        return (db.sales_state_get("wa_cutover") or "off").strip().lower()
    except Exception:  # noqa: BLE001
        return "off"


def enabled_for(phone: str) -> bool:
    """האם הבוט ה-native מטפל בשולח — לפי מצב ה-cutover (ראה cutover_mode)."""
    mode = cutover_mode()
    if mode == "halt":          # חירום → אף אחד native, הכל חוזר לקונקטופ
        return False
    if mode == "live":          # cutoff → כל הלקוחות native
        return True
    wl = whitelist()            # off (ברירת מחדל) → רק רשימת הבדיקה
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

# כפתורים/טקסטים חיצוניים שאינם פריטי התפריט שלנו — WhatsApp Ice Breakers, כפתורי
# Chaty באתר, ושאריות מתפריט ConnectOp הישן. בלי המיפוי הזה הם נופלים לחיפוש מוצר
# ("מצאתי 9 תוצאות ל'שירות לקוחות'") במקום לכוונה הנכונה.
_BTN_ALIASES = {
    "שירות לקוחות": "agent", "שירות לקוח": "agent", "שירות": "agent",
    "צור קשר": "agent", "יצירת קשר": "agent", "דבר עם נציג": "agent",
    "הזמנות": "status", "ההזמנות שלי": "status", "ההזמנה שלי": "status",
    "מעקב הזמנה": "status", "מעקב הזמנות": "status", "מעקב משלוח": "status",
    "סניפים": "branches", "כתובות": "branches", "שעות פתיחה": "branches",
    "משלוחים": "shipping", "מחירי משלוח": "shipping",
    "מעבדה": "lab", "תיקון": "lab", "תיקונים": "lab",
}

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


def _is_order_status_intent(low: str) -> bool:
    """כוונת 'איפה ההזמנה שלי' בטקסט חופשי → לנתב לזרימה הדטרמיניסטית (זיהוי לפי
    טלפון) במקום לאורי, שאין לו גישה למערכת ההזמנות ונכנס ללופ."""
    if any(w in low for w in ("הזמנה חדשה", "להזמין", "לקנות", "רוצה להזמין", "איך מזמינים")):
        return False
    has_order = any(w in low for w in ("הזמנה", "הזמנת", "המשלוח", "החבילה"))
    track = any(w in low for w in ("סטטוס", "איפה", "היכן", "מתי", "הגיע", "מעקב",
                                   "לא קיבלתי", "לא הגיע", "מה קורה עם", "מה עם"))
    return has_order and track


def handle(phone: str, text: str, mtype: str = "text", reply_id: str = "", wamid: str = ""):
    """נקודת הכניסה — מקבלת הודעת לקוח ומגיבה. מנוהל מצב ב-wa_bot_session.
    reply_id = id של כפתור/רשימה (ניתוב מדויק); נופלים לטקסט אם אין.
    wamid = מזהה ההודעה הנכנסת — לחיווי הקלדה בזמן המתנה לאורי."""
    text = (text or "").strip()
    sess = db.bot_session_get(phone)
    state = sess.get("state")
    rid = reply_id or _TITLE2ID.get(text, "")   # id מהבחירה, או מיפוי מהכותרת
    if rid in _BTN_ALIASES:                       # reply_id שנושא תווית במקום id
        rid = _BTN_ALIASES[rid]
    elif not rid:                                 # כפתור חיצוני (Ice Breaker/Chaty) → כוונה
        rid = _BTN_ALIASES.get(text, "")
    low = text

    # ── handoff לנציג אנושי: הבוט *שותק* כדי שאדם ישתלט. אבל אם הלקוח לוחץ כפתור/
    # 'תפריט'/ברכה — הוא רוצה את הבוט בחזרה → משחררים את ה-handoff וממשיכים. רק טקסט
    # חופשי משאיר את הבוט שקט (אדם מטפל). חייב להיות ראשון. ──
    # ⚠️ ההבחנה היא 'האם אדם ענה בפועל', לא 'שעות עבודה' (באג 20/06: אסי טיפל ידנית
    # בשבת והבוט פרץ בין ההודעות). שני מצבים בלבד: (א) אדם כבר ענה ידנית (human=True
    # ב-_bot_handoff_on) → הבוט שקט תמיד, בלי קשר לשעה — לא פורצים מעל נציג חי. (ב) אף
    # אדם לא ענה עדיין → אורי ממשיך לענות (גם בתוך שעות), כדי שלקוח שלחץ 'נציג' ואז שאל
    # שאלה לא יישאר תקוע בלי מענה (שיחות 543159043/546619676). הרגע שאסי כותב — שקט.
    if state == "agent" and _agent_handoff_active(sess):
        human_active = bool((sess.get("data") or {}).get("human"))
        reengage = (bool(rid) or low in _ESCAPE or _is_greeting(low)
                    or not human_active)
        if not reengage:
            return
        db.bot_session_clear(phone)
        sess = {"state": None, "data": {}}
        state = None

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
    # תמונה/מדיה ללא טקסט — לא לאבד את הלקוח לברכה ("היי תודה שפנית..." מרגיש כמו איפוס).
    # מאשרים בחום ומפעילים את אורי, כך שההמשך יישאר בשיחה (Apple Watch case).
    if not rid and not low and mtype in ("image", "video", "sticker", "document", "audio"):
        _mark_uri_engaged(phone)
        wa.send_text(phone, "קיבלתי 📷 איך אפשר לעזור? אפשר גם לתאר לי בקצרה במילים.")
        return

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
    # ── 🔔 הרשמה ל-Stock Watch (כפתור "עדכנו כשחוזר" / בחירת צבע) ──
    if rid.startswith("notify_stock:"):
        return _offer_stock_watch(phone, rid.split(":", 1)[1], sess)
    if rid.startswith("notify_sku:"):
        parts = rid.split(":")
        sku = parts[1] if len(parts) > 1 else ""
        spid = parts[2] if len(parts) > 2 else ""
        color = None
        if spid:
            try:
                import main as _m
                color = next((v.get("color") for v in (_m.bot_get_variations(spid) or [])
                              if str(v.get("sku")) == str(sku)), None)
            except Exception:  # noqa: BLE001
                color = None
        return _register_stock_watch(phone, sku,
                                     (sess.get("data") or {}).get("product") or {}, color=color)
    # "מתי חוזר למלאי?" בזמן צפייה במוצר → הצעת הרשמה ל-Stock Watch
    if state == "viewing" and not rid and _is_back_in_stock_intent(low):
        return _offer_stock_watch(phone, (sess.get("data") or {}).get("pid", ""), sess)
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
    if rid == "status" or _is_order_status_intent(low):
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

    # ── טקסט חופשי → אורי (שיחה) או חיפוש מוצר ──
    if low and low not in _GREETINGS:
        # הודעת סגירה/תודה טהורה ('הבנתי תודה') → תגובה קצרה בנימוס, בלי 'בודק עבורך'
        # ובלי לערב את אורי (זה רק סיום שיחה, אין מה לחפש).
        if _is_closing(low):
            wa.send_text(phone, "בשמחה! אם יש עוד שאלות — אנחנו כאן 😊")
            return
        # שאלת מחיר-תיקון בטקסט חופשי → מחירון המעבדה הדטרמיניסטי (לא אורי, שולח לנציג).
        # שיחה 972546619676: 'כמה עולה להחליף סוללה לאייפון 14 פרו' נפלה לאורי.
        if _is_repair_quote_intent(low):
            import main as _m
            if _m.bot_repair_quote(text):
                return _repair_quote_result(phone, text)
            wa.send_text(phone, "💰 *הצעת מחיר לתיקון*\nלאיזה דגם? (למשל: אייפון 14 פרו, גלקסי S23)")
            db.bot_session_set(phone, "await_repair_model", {})
            return
        prod = _product_query(low)
        # שיחה פעילה עם אורי: כל עוד זו לא שאילתת מוצר ברורה — הכל נשאר עם אורי,
        # כולל הודעות קצרות ('Ok'/'תודה') ומשפטים באנגלית ('I should receive it today').
        if _uri_engaged(phone) and not prod and _ask_uri(phone, text, wamid):
            return
        # צבע/נפח קצר אחרי בחירת מוצר → המשך הקשרי (Apple 16 ואז "ורוד"), לא חיפוש
        # "ורוד" גלובלי. אורי רואה את השיחה ועונה על הוריאציה; אם לא זמין — משלבים.
        if state in ("new_pick", "viewing") and _is_attr_followup(low):
            if _ask_uri(phone, text, wamid):
                return
            prevq = (sess.get("data") or {}).get("__q", "")
            if prevq:
                return _new_order_results(phone, f"{prevq} {text}".strip())
        if len(low) >= 3:
            question_like = bool(_re.search(
                r"\?|מה ההבדל|הבדל בין|השוואה|עדיף|מה מתאים|כדאי|להמליץ|המלצ|תקציב|עד \d|"
                r"יבואן|אחריות|האם|כמה עולה|מה יותר", text))
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

# צבע/נפח קצר אחרי בחירת מוצר = המשך הקשרי (צבע/וריאציה), לא חיפוש מוצר חדש
_COLORS = {"ורוד", "שחור", "לבן", "כחול", "אדום", "ירוק", "צהוב", "סגול", "כתום",
           "אפור", "זהב", "כסף", "תכלת", "חום", "ורד", "נייבי", "רוז", "גרפיט",
           "שמנת", "כהה", "בהיר", "טבעי", "סגלגל", "ירקרק", "כחלחל"}


def _is_attr_followup(low: str) -> bool:
    w = (low or "").strip()
    if w in _COLORS:
        return True
    if w.startswith("בצבע") or w.startswith("צבע "):
        return True
    if _re.fullmatch(r"\d{2,4}\s*(gb|tb|ג'יגה|טרה|ג״ב)?", w, _re.I):
        return True
    return False


# מילות סגירה/תודה — הודעה שכולה כאלה = סיום שיחה, לא שאלה. אסור להפעיל עליה
# 'בודק עבורך' (אורי) — רק להגיב בנימוס. **לא** כולל 'כן' (עלול להיות אישור רכישה).
_CLOSE_TOK = {"תודה", "רבה", "הבנתי", "מעולה", "אוקיי", "אוקי", "אוקייי", "סבבה",
              "מצוין", "מצויין", "יופי", "ברור", "אחלה", "מושלם", "טוב", "תודהה",
              "ok", "okay", "thanks", "thank", "you", "great", "perfect", "👍", "🙏"}


def _is_closing(low: str) -> bool:
    """True אם כל ההודעה היא מילות סגירה/תודה (עד 4 מילים) — 'הבנתי תודה', 'מעולה'."""
    s = _re.sub(r"[!.,?‏‎\s]+", " ", (low or "")).strip()
    words = [w for w in s.split() if w]
    return bool(words) and len(words) <= 4 and all(w in _CLOSE_TOK for w in words)


# זיהוי שאלת מחיר-תיקון בטקסט חופשי ('כמה עולה להחליף סוללה לאייפון 14') → לנתב למחירון
# המעבדה הדטרמיניסטי במקום לאורי (שלא מכיר אותו ושולח לנציג). דורש חלק-תיקון + פועל/הקשר-
# תיקון יחד, כדי לא לתפוס שאילתות מוצר ('מגן מסך', 'יש לכם מסך?').
_REPAIR_PART_W = ("סוללה", "מסך", "צג", "שקע", "מצלמה", "זכוכית", "רמקול", "אוזניה",
                  "מיקרופון", "טאצ", "לחצן", "כפתור", "ויברציה", "גב ", "דיבורית")
_REPAIR_VERB_W = ("להחליף", "החלפת", "החלפה", "מחליפים", "לתקן", "תיקון", "שבור", "שבורה",
                  "נשבר", "סדוק", "התקלקל", "מקולקל", "דפוק", "לא עובד", "לא דולק", "נשרט")


def _is_repair_quote_intent(low: str) -> bool:
    s = low or ""
    if "מגן" in s or "כיסוי" in s:        # 'מגן מסך'/'כיסוי' = מוצר, לא תיקון
        return False
    has_part = any(p in s for p in _REPAIR_PART_W)
    has_verb = any(v in s for v in _REPAIR_VERB_W)
    return has_part and has_verb


# מילות-מילוי שיחתיות — מוסרות בנרמול שאילתה כדי לזהות שאילתה חוזרת ('אני רוצה אבל
# אייפון 16' == 'אז יש לכם אייפון 16' → שתיהן 'אייפון 16 פרו מקס'). מונע לופ של אותה
# תשובת חיפוש שוב ושוב.
_Q_FILLER = {"אני", "רוצה", "אבל", "אז", "יש", "לכם", "שלכם", "האם", "אתם", "מחפש",
             "מחפשת", "את", "תגיד", "תגידי", "אולי", "עוד", "גם", "כן", "לא", "צריך",
             "מעוניין", "מעוניינת", "רק", "בבקשה", "עדיין", "זמין", "קיים", "מכשיר",
             "טלפון", "דגם", "מחיר", "כמה", "עולה", "של", "ה"}


def _norm_q(q: str) -> str:
    s = _re.sub("[^0-9a-z֐-׿]+", " ", (q or "").lower())
    return " ".join(w for w in s.split() if w and w not in _Q_FILLER)


# רמז-צמצום דינמי לפי המותג שזוהה — במקום דוגמה קבועה (JBL) שנראית אבסורדית בשאילתת
# אייפון. ברירת מחדל גנרית-הגיונית אם המותג לא זוהה.
_NARROW_HINT = {"apple": "iPhone 17 / iPad", "iphone": "iPhone 17 / iPad",
                "ipad": "iPad / iPhone", "macbook": "MacBook Air / Pro",
                "samsung": "Galaxy S25 / A55", "galaxy": "Galaxy S25 / A55",
                "xiaomi": "Redmi Note 14", "redmi": "Redmi Note 14", "poco": "Poco X6",
                "jbl": "Flip / Charge", "anker": "Soundcore / PowerBank",
                "sony": "WH-1000 / WF", "bose": "QuietComfort", "google": "Pixel 9",
                "pixel": "Pixel 9", "honor": "Honor Magic", "oppo": "Oppo Reno"}
_HINT_HE = {"אייפון": "apple", "אייפד": "ipad", "מקבוק": "macbook", "גלקסי": "samsung"}


def _narrow_hint(query: str) -> str:
    low = (query or "").lower()
    bkey = (next((b for b in _BRANDS if b in low), None)
            or next((en for he, en in _BRAND_HE.items() if he in (query or "")), None)
            or next((en for he, en in _HINT_HE.items() if he in (query or "")), None))
    return _NARROW_HINT.get(bkey, "דגם מדויק או נפח אחסון")


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


def _mark_uri_engaged(phone):
    """מסמן שהלקוח בשיחה פעילה עם אורי — כדי שהמשך ההודעות הקצרות שלו ('אלי', 'תודה',
    מספר טלפון) ימשיכו לאורי ולא ייתפסו כחיפוש מוצר."""
    from datetime import datetime, timezone
    try:
        db.sales_state_set(f"uri_eng:{phone}", datetime.now(timezone.utc).isoformat())
    except Exception:  # noqa: BLE001
        pass


def _uri_engaged(phone, minutes=5) -> bool:
    """האם יש שיחה פעילה עם אורי ב-5 הדקות האחרונות."""
    from datetime import datetime
    try:
        v = db.sales_state_get(f"uri_eng:{phone}")
        if not v:
            return False
        t = datetime.fromisoformat(str(v))
        return (datetime.now(t.tzinfo) - t).total_seconds() < minutes * 60
    except Exception:  # noqa: BLE001
        return False


def _ask_uri(phone, question, wamid="") -> bool:
    """מנתב שאלה לאורי (תשובה תישלח אוטומטית כשתחזור). False אם הגשר לא זמין.
    מצרף מוצרים מועמדים מהחיפוש כדי שאורי יענה מהר — בלי סבב כלים."""
    if not _uri_alive():
        return False
    import main
    # "כמה רגעים, בודק עבורך" — רק בתחילת שיחה (כשעוד לא מעורב עם אורי), פעם אחת.
    # החיווי הרציף מכסה את ההמתנה; חזרה על הטקסט על כל תשובה נראית רע (Hofit/Apple Watch).
    # זכר (אורי = שם זכר) — בלי 'בודק/ת'.
    if not _uri_engaged(phone):
        wa.send_text(phone, "כמה רגעים, בודק עבורך 🔎")
    # ── חיווי הקלדה רציף — מתחיל **מיד** (מכסה גם את החיפוש לפני שהמשימה נכנסת לתור,
    #    שם היה הפער של ~30ש), משדר מחדש כל 7ש כי החיווי פג אחרי ~25ש (וה'רגע בודק'
    #    מנקה אותו), ונעצר כשהמשימה done/error. העיבוד אסינכרוני בשירות נפרד. ──
    _ka = {"jid": None}
    if wamid:
        import threading
        import time as _t

        def _keepalive():
            try:
                wa.send_typing(wamid)          # מיד אחרי 'רגע בודק' — בלי פער
            except Exception:  # noqa: BLE001
                pass
            for _ in range(22):                # עד ~154ש
                _t.sleep(7)
                try:
                    jid = _ka["jid"]
                    if jid:
                        j = db.uri_job_get(jid)
                        if not j or j.get("status") in ("done", "error"):
                            return
                    wa.send_typing(wamid)
                except Exception:  # noqa: BLE001
                    return
        threading.Thread(target=_keepalive, daemon=True).start()
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
    jid = db.uri_job_add(phone, q, source="bot")
    _ka["jid"] = jid                  # מעכשיו ה-keepalive עוקב אחרי סטטוס המשימה
    _mark_uri_engaged(phone)          # שיחה פעילה — המשך ההודעות יישאר עם אורי
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

# מילות קטגוריה ברורות (לא 'טלפון'/'מסך' הגנריים) — לזיהוי שאילתת מוצר אמיתית
_PRODUCT_CATS = ("אוזניות", "סמארטפון", "אייפון", "גלקסי", "מטען", "כבל", "שעון חכם",
                 "טאבלט", "קונסולה", "רמקול", "פלייסטיישן", "מקלדת", "עכבר", "ראוטר",
                 "סוללה", "ספיקר", "באנדל", "כיסוי", "מגן מסך")


def _product_query(text: str) -> bool:
    """האם הטקסט נראה כשאילתת מוצר ברורה — מותג ידוע (אנגלית/עברית) או מילת קטגוריה.
    כך משפט שיחתי (גם באנגלית: 'I should receive it today') לא נחטף לחיפוש מוצר."""
    t = (text or "").lower()
    if any(b in t for b in _BRANDS):
        return True
    if any(he in text for he in _BRAND_HE):
        return True
    return any(c in text for c in _PRODUCT_CATS)


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
    name = m.group(1).split("\n")[0].split("\r")[0]   # shura rishona
    name = _re.sub(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]", "", name)  # tavim nistarim
    name = _re.sub(r"\s+", " ", name).strip(" -–—:·\"״׳")
    return name[:120]


def _model_core(title):
    """החלק הלטיני (מותג+דגם — תמיד באנגלית באתר) לחיפוש ממוקד, בלי קשר למיקומו."""
    m = _re.search(r"[A-Za-z][A-Za-z0-9.\s]*[A-Za-z0-9]", title or "")
    return m.group(0).strip() if m else ""


def _title_toks(t):
    t = _re.sub(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff\"\u05f4\u05f3]", "", t or "")
    t = _re.sub(r"[^\w֐-׿]+", " ", t.lower())
    return set(w for w in t.split() if w)


def _title_cov(title, name):
    """כיסוי: איזה חלק ממילות כותרת הכניסה מופיעות בשם המוצר באתר. שם המוצר ארוך
    מהכותרת (כולל תיאור), אז כיסוי עדיף על Jaccard — המוצר המדויק מקבל ~1.0."""
    a, b = _title_toks(title), _title_toks(name)
    return (len(a & b) / len(a)) if a else 0.0


def _entry_product_flow(phone, text):
    """כניסה מעמוד מוצר → מציג ישר את המוצר. חיפוש כותרת ישיר ב-WC (לא מנוע ה-facet
    שמשחרר ומחזיר מוצר שגוי) + דירוג לפי דמיון לכותרת. התאמה ודאית (ציון גבוה) →
    כרטיס ישיר; אחרת → רשימה/חיפוש רגיל, כדי לא להציג מוצר שגוי בביטחון."""
    import main
    title = _entry_product_name(text)
    if not title:
        return _menu(phone)
    # חיפוש לפי הדגם הלטיני (תמיד באנגלית באתר) — מוצא את המוצר בין אם הדגם לפני
    # המקף ובין אם אחריו. נפילה לכותרת המלאה אם אין דגם לטיני/אין תוצאות.
    key = _model_core(title) or title
    try:
        results = main.bot_wc_title_search(key, limit=20) or []
        if not results and key != title:
            results = main.bot_wc_title_search(title, limit=20) or []
    except Exception:  # noqa: BLE001
        results = []
    if not results:                              # לא נמצא כלום → הזרימה הרגילה
        return _new_order_results(phone, key)
    # דירוג לפי כיסוי (כמה ממילות הכותרת בשם המוצר), ואז שם קצר יותר = ספציפי יותר
    ranked = sorted(results, key=lambda p: (_title_cov(title, p.get("name", "")),
                                            -len(p.get("name", ""))), reverse=True)
    top = ranked[0]
    if _title_cov(title, top.get("name", "")) >= 0.7:   # התאמה ודאית → ישר לכרטיס
        pid = f"prod:{top['id']}"
        data = {pid: {"name": top.get("name"), "price": top.get("price"),
                      "permalink": top.get("permalink"), "sku": top.get("sku"),
                      "stock": top.get("stock_status"), "type": top.get("type"),
                      "image": top.get("image"), "brand": top.get("brand")},
                "__q": title}
        db.bot_session_set(phone, "new_pick", data)
        wa.send_text(phone, "מצוין! הנה המוצר שהתעניינת בו 👇")
        return _product_card(phone, pid, data)
    # יש מועמדים אך לא ודאי → רשימת תוצאות הכותרת (לא מנוע ה-facet שמחזיר מותג שגוי)
    return _entry_results_list(phone, ranked[:9], title)


def _entry_results_list(phone, results, query):
    """רשימת מוצרים מתוצאות חיפוש הכותרת (לא facet). לבחירה כשאין התאמה ודאית."""
    rows, data = [], {}
    for p in results:
        pid = f"prod:{p['id']}"
        ttl, extra = _name_parts(p.get("name"), p.get("brand"))
        price_s = _price_label(p)
        desc = f"{price_s} · {extra}"[:72] if extra else price_s
        rows.append((pid, ttl[:24], desc))
        data[pid] = {"name": p.get("name"), "price": p.get("price"),
                     "permalink": p.get("permalink"), "sku": p.get("sku"),
                     "stock": p.get("stock_status"), "type": p.get("type"),
                     "image": p.get("image"), "brand": p.get("brand")}
    rows.append(("search_again", "🔍 חיפוש חדש", ""))
    data["__q"] = query
    db.bot_session_set(phone, "new_pick", data)
    wa.send_list(phone, "מצאתי כמה אפשרויות — בחר/י את המוצר:", rows,
                 button_label="לתוצאות", section_title="תוצאות")


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
    # שאילתה חוזרת? (הלקוח שואל שוב את אותו דבר אחרי שכבר קיבל רשימה) → הרשימה לא
    # ענתה לו; אסור להציף שוב את אותן תוצאות (נראה תקוע). מזהים לפי הליבה המנורמלת.
    _prevq = (db.bot_session_get(phone).get("data") or {}).get("__q", "")
    _nq = _norm_q(query)
    is_repeat = bool(_nq) and _norm_q(_prevq) == _nq
    sr = main.bot_smart_search(query, limit=20)
    all_results = sr.get("results") or []
    meta = sr.get("meta") or {}
    results = all_results[:9]
    if results and is_repeat:
        # אורי (AI) מטפל בניואנס — דגם שאזל/לא קיים, ממליץ חלופה, משוחח. אם לא זמין —
        # מציעים נציג במקום אותה רשימה בפעם השנייה.
        if _ask_uri(phone, query):
            return
        wa.send_buttons(phone, "רואה שאתה מחפש את אותו דבר 🙂 אם הדגם המדויק לא מופיע "
                               "ברשימה — ייתכן שאינו במלאי כרגע. אפשר להעביר אותך לנציג "
                               "שיבדוק עבורך, או לחפש דגם אחר.",
                        [("agent", "👤 דבר/י עם נציג"), ("menu", "↩️ תפריט")])
        return
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
               f"לצמצום, הוסף/י דגם מדויק (למשל {_narrow_hint(query)}). בחר/י לפרטים:")
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
    pid = rid.split(":", 1)[1]
    q = (data or {}).get("__q", "")
    # מודעות-וריאציה: שולפים פעם אחת (צבע/מלאי/sku)
    vs = []
    if p.get("type") == "variable":
        try:
            import main as _m
            vs = _m.bot_get_variations(pid) or []
        except Exception:  # noqa: BLE001
            vs = []
    instock = [v for v in vs if v.get("stock") == "instock"]
    oos = _oos_variations(vs)
    asked = _asked_color(q, vs)       # הוריאציה שתואמת לצבע שהלקוח שאל עליו
    stock = p.get("stock")
    if asked and asked.get("stock") == "outofstock" and instock:
        # שאל על צבע שאזל ויש זמינים → קודם מציעים את הזמינים; watch רק אם מתעקש
        avail = ", ".join(v.get("color") for v in instock if v.get("color"))
        lines.append(f"⏳ הצבע *{asked.get('color')}* אזל כרגע.")
        lines.append(f"✅ זמין מיד: *{avail}*")
        btns = [(f"buy:{pid}", "🛒 הזמן זמין"),
                (f"notify_sku:{asked.get('sku')}:{pid}", f"💙 רק {(asked.get('color') or '')[:11]}"),
                ("agent", "👤 נציג")]
    else:
        if stock == "outofstock":
            lines.append("⏳ אזל כרגע")
        elif oos:
            cols = ", ".join(v.get("color") for v in oos if v.get("color"))
            lines.append("✅ זמין במלאי" + (f"\n⏳ אזל בצבעים: {cols}" if cols else ""))
        elif stock == "instock":
            lines.append("✅ זמין במלאי")
        if (stock == "outofstock" and p.get("sku")) or oos:
            btns = [(f"notify_stock:{pid}", "🔔 עדכנו כשחוזר"),
                    (f"buy:{pid}", "🛒 הזמן"), ("agent", "👤 נציג")]
        else:
            btns = [(f"buy:{pid}", "🛒 הזמן עכשיו"), ("search_again", "🔍 מוצר אחר"), ("agent", "👤 נציג")]
    if p.get("permalink"):
        # slug עברי → קישור מקוצר gm- (קישור עברי ארוך נראה שבור/חשוד); אנגלי נשאר ישיר
        lines.append(f"\n🔗 לרכישה ולפרטים:\n{_short_link(p['permalink'])}")
    body = "\n".join(lines)
    img = p.get("image") or ""
    try:                                   # כרטיס עם תמונה; אם נכשל — fallback לטקסט
        wa.send_buttons(phone, body, btns, header_image=img if img.startswith("http") else "")
    except Exception as e:  # noqa: BLE001
        logger.warning("product card image failed, text fallback: %s", e)
        wa.send_text(phone, body)
        wa.send_buttons(phone, "מה הלאה?", btns)
    # שומרים את המוצר לשלב הרכישה (כולל שאלת הלקוח — לרמז צבע/וריאציה)
    db.bot_session_set(phone, "viewing", {"pid": pid, "product": p, "q": (data or {}).get("__q", "")})
    # מפעילים את אורי — שאלות המשך אחרי כרטיס ("מה מחיר"/"יש צבעים?"/"מה ההבדל") יילכו
    # אליו עם הקשר השיחה, במקום ליפול לברכה (Apple Watch case).
    _mark_uri_engaged(phone)


# ── 🔔 Stock Watch: עדכון ללקוח כשמוצר/צבע שאזל חוזר למלאי ──
def _oos_variations(vs: list) -> list:
    """וריאציות שאזלו שכל הצבע שלהן אזל (לא רק נפח מסוים). אותו צבע יכול להופיע בכמה
    נפחים (TUNDRA UMBER 512GB אזל אך 1TB במלאי) — אם יש ולו וריאציה אחת זמינה באותו
    צבע, הצבע לא נחשב 'אזל'. בלי זה הבוט מכריז 'אזל בצבע X' שגוי כשיש X זמין בנפח אחר
    (שיחה 491633171111: יחידה אחרונה דווחה כאזלה)."""
    instock_colors = {(v.get("color") or "").strip() for v in (vs or [])
                      if v.get("stock") == "instock" and v.get("color")}
    return [v for v in (vs or []) if v.get("stock") == "outofstock" and v.get("sku")
            and (v.get("color") or "").strip() not in instock_colors]


def _asked_color(q: str, variations: list):
    """מזהה את הוריאציה שתואמת לצבע שהלקוח שאל עליו (רמז בשאלה), אם יש. אותו צבע יכול
    להופיע בכמה נפחים — מעדיפים וריאציה **זמינה** באותו צבע על פני אחת שאזלה, כדי לא
    לדווח 'אזל' כשיש יחידה זמינה באותו צבע בנפח אחר."""
    ql = (q or "")
    match = [v for v in (variations or [])
             if (v.get("color") or "").strip() and (v.get("color") or "").strip() in ql]
    if not match:
        return None
    return next((v for v in match if v.get("stock") == "instock"), match[0])


def _is_back_in_stock_intent(low: str) -> bool:
    import re
    return bool(re.search(r"(מתי|צפי).{0,14}(חוזר|במלאי|יהיה|זמין)"
                          r"|חוזר(ת|ות)?\s*למלאי|כש(י|ת)חזור|back in stock", low or ""))


def _offer_stock_watch(phone, pid, sess):
    """לקוח רוצה עדכון 'חזר למלאי' — מזהה את הוריאציה לפי רמז הצבע בשאלה ורושם,
    או שואל איזה צבע אם כמה אזלו."""
    if not pid:
        return _to_agent(phone)
    d = sess.get("data") or {}
    p = d.get("product") or {}
    q = d.get("q") or ""
    try:
        import main as _m
        vs = _m.bot_get_variations(pid) or []
    except Exception:  # noqa: BLE001
        vs = []
    oos = [v for v in vs if v.get("stock") == "outofstock" and v.get("sku")]
    if not oos and p.get("stock") == "outofstock" and p.get("sku"):
        return _register_stock_watch(phone, str(p.get("sku")), p)
    if not oos:
        wa.send_text(phone, "המוצר זמין במלאי כרגע 🙂 אשמח לעזור בהזמנה — לחצ/י *הזמן עכשיו*.")
        return
    hit = next((v for v in oos if v.get("color") and v["color"] in q), None)
    if not hit and len(oos) == 1:
        hit = oos[0]
    if hit:
        return _register_stock_watch(phone, str(hit.get("sku")), p, color=hit.get("color"))
    btns = [(f"notify_sku:{v.get('sku')}:{pid}", (v.get("color") or "צבע")[:20]) for v in oos[:3]]
    wa.send_buttons(phone, "לאיזה צבע לעדכן אותך כשחוזר למלאי? 🔔", btns)
    return


def _register_stock_watch(phone, sku, product=None, color=None):
    """רושם את הלקוח ב-Stock Watcher (notify=False — הבוט שולח אישור משלו)."""
    p = product or {}
    pname = p.get("name") or "המוצר"
    if color:
        pname = f"{pname} - {color}"
    name = ""
    try:
        name = (db.wa_contact_get(phone) or {}).get("name") or ""
    except Exception:  # noqa: BLE001
        pass
    try:
        import main as _m
        _m._stock_watch_add(_m.StockWatchAddIn(
            phone=phone, name=name, sku=str(sku), product_name=pname,
            product_url=p.get("permalink") or "", notes="נרשם דרך הבוט", notify=False))
    except Exception as e:  # noqa: BLE001
        logger.warning("bot stock-watch register failed for %s: %s", phone, e)
        wa.send_text(phone, "לא הצלחתי לרשום כרגע 🙏 מעביר אותך לנציג שיוסיף אותך ידנית.")
        return _to_agent(phone)
    wa.send_text(phone, f"מעולה! רשמתי אותך 🔔\nברגע ש*{pname}* חוזר למלאי — תקבל ממני הודעה.")
    return _menu_tail(phone)


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


def _il_now():
    """שעון ישראל (השרת רץ ב-UTC). ZoneInfo מטפל ב-DST; נפילה ל-UTC+3 אם חסר tzdata."""
    from datetime import datetime, timezone, timedelta
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Jerusalem"))
    except Exception:  # noqa: BLE001
        return datetime.now(timezone.utc) + timedelta(hours=3)


def _within_business_hours(now=None) -> bool:
    """שעות פעילות: א'-ה' 09:00-21:00, יום ו' 09:00-14:00, שבת סגור.
    weekday(): Mon=0..Sun=6 → ראשון=6, שני-חמישי=0-3, שישי=4, שבת=5."""
    now = now or _il_now()
    wd = now.weekday()
    h = now.hour + now.minute / 60.0
    if wd == 5:                # שבת
        return False
    if wd == 4:                # שישי
        return 9 <= h < 14
    return 9 <= h < 21         # ראשון-חמישי


_CLOSED_MSG = ("🕐 משרדינו סגורים כרגע.\n\n"
               "שעות הפעילות:\n"
               "ימים א׳-ה׳ | 09:00-21:00\n"
               "יום ו׳ | 09:00-14:00\n\n"
               "השאירו כאן את נושא הפנייה ונחזור אליכם בהקדם ביום העסקים הבא 🙏")


def _to_agent(phone, note: str = ""):
    from datetime import datetime, timezone
    open_now = _within_business_hours()
    if open_now:
        # בשעות הפעילות: 2 הודעות — אישור קבלה + תיאום ציפיות לגבי נציג אנושי
        wa.send_text(phone, "תודה, פנייתך התקבלה בהצלחה אחד מנציגינו יחזור אליך בהקדם האפשרי.")
        wa.send_text(phone, "😊 לפני שניפרד אני רוצה לתאם ציפיות. נציגי השירות שלנו הם בני אדם "
                            "(לא כמוני) ולכן הם נותנים כעת את תשומת ליבם ללקוחות קודמים. זה אומר "
                            "שאולי ייקח קצת זמן עד שיענו לך. תודה מראש על הסבלנות")
    else:
        # מחוץ לשעות הפעילות: הודעת "משרדינו סגורים" (אף נציג לא יענה כעת)
        wa.send_text(phone, _CLOSED_MSG)
    # state 'agent' + חותמת → הבוט משתתק (handle בודק בתחילתו) כדי שאדם ישתלט
    db.bot_session_set(phone, "agent", {"note": note,
                                        "ts": datetime.now(timezone.utc).isoformat()})
    try:                                  # מסמן 'אנושי' שהבוט לא יקפוץ + מתריע
        import main
        closed_tag = "" if open_now else "\n🌙 <i>מחוץ לשעות הפעילות — נשלחה הודעת 'משרדינו סגורים'</i>"
        main._tg_admin(f"👤 <b>לקוח ביקש נציג (בוט native)</b>\n{phone}{(' · ' + note) if note else ''}"
                       f"\n🤖 הבוט הושתק לשיחה הזו — ענה/י ידנית בקונסולה.{closed_tag}")
    except Exception:  # noqa: BLE001
        pass


def _agent_handoff_active(sess, hours=12) -> bool:
    """האם הלקוח עדיין מועבר לנציג אנושי (הבוט צריך לשתוק). פג אחרי `hours` שעות —
    אז הבוט חוזר לענות. **חותמת חסרה → מושתק** (handoff ידני/ישן — עדיף שקט)."""
    from datetime import datetime
    ts = (sess.get("data") or {}).get("ts")
    if not ts:
        return True
    try:
        t = datetime.fromisoformat(str(ts))
        return (datetime.now(t.tzinfo) - t).total_seconds() < hours * 3600
    except Exception:  # noqa: BLE001
        return True


_REPAIR_EMOJI = {"ממתין": "⏳", "תוקן": "✅", "נמסר ללקוח": "📦",
                 "נשלח למעבדה חיצונית": "🔧", "נישלח לאחריות": "🛡️", "מושבת": "🚫"}


def _repair_status(phone, fix_id=None):
    """סטטוס תיקון מ-NewOrder (/api/Fixes) — לפי טלפון הלקוח, או מספר תיקון שהוקלד.
    לא נמצא לפי הטלפון → מבקש מספר תיקון; לא נמצא לפי מספר → מאפשר לנסות שוב או נציג."""
    import main
    results = main.bot_repair_status(phone=("" if fix_id else phone), fix_id=fix_id)
    if not results:
        if fix_id:                            # חיפשו לפי מספר ולא נמצא → לנסות שוב / נציג
            wa.send_text(phone, f"לא מצאתי את תיקון מספר {fix_id} במערכת המקוונת 🤔\n"
                                "ייתכן שזה תיקון ישן יותר, או מספר שגוי. בדוק/י ונסה/י שוב, "
                                "או כתוב/י *נציג* — נבדוק עבורך ישירות מול המעבדה.")
            db.bot_session_set(phone, "await_repair_id", {})
        else:                                 # לא נמצא לפי הטלפון → לבקש מספר תיקון
            wa.send_text(phone, "לא מצאתי תיקון על מספר הטלפון שלך 🤔\n"
                                "אם יש *מספר תיקון* (מהקבלה) — כתוב/י אותו, או *נציג* לבירור.")
            db.bot_session_set(phone, "await_repair_id", {})
        return
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
        dev = cands[0]
        # אם הלקוח כבר ציין מהות תיקון ('מסך אייפון 14') — קופצים ישר למחיר, בלי לשאול שוב
        if main.bot_repair_match_part(dev, text):
            db.bot_session_set(phone, "await_repair_part", {"device": dev})
            return _repair_part_result(phone, text)
        return _repair_ask_part(phone, dev)
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
        wa.send_text(phone, "לא מצאתי הזמנה על מספר הטלפון הזה 🤔\n"
                            "אם יש לך *מספר הזמנה* — שלח/י אותו (ספרות בלבד) ואבדוק מיד.\n"
                            "אם אין — כתוב/י *נציג* ונשמח לעזור.")
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
# ⚠️ pending = ממתין לתשלום (טרם שולם), לא 'בטיפול'! בלי זה הבוט אמר ללקוח 'נקלטה
# ונמצאת בטיפול' על הזמנה שלא שולמה (שיחה 539644424, הזמנה 47323).
_ST_PENDING = {"pending", "checkout-draft"}


def _status_msg(o, meta, name, num):
    """טקסט סטטוס מותאם לפי שלב ההזמנה + שיטת המשלוח (קרגו/אקספרס/איסוף סניף/נק׳ ת״א)."""
    import main
    head = f"היי {name or 'לקוח/ה יקר/ה'},\nעדכון בנוגע להזמנה מס' *{num}*\n"
    st = o.get("status")
    titles = " ".join((sl.get("method_title") or "") for sl in (o.get("shipping_lines") or []))
    is_tlv = st == "tlv-pickup" or "נקודת מסירה" in titles
    is_pickup = (not is_tlv) and "איסוף" in titles
    is_express = "אקספרס" in titles or "אותו היום" in titles
    if st in _ST_PENDING:
        msg = head + ("סטטוס: ההזמנה *ממתינה להשלמת התשלום* 💳\n"
                      "ברגע שהתשלום יתקבל נתחיל בהכנה ונעדכן אותך 🙏")
        link = meta.get("greenos_payplus_link") or ""
        if link:
            msg += f"\n\n🔗 להשלמת התשלום:\n{link}"
        return msg
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
