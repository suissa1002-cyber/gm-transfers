"""
pos_driver.py — הנעת קופת NewOrder/Morning (VB6) דרך pywinauto (backend win32).
נקרא ע"י agent.py. ממופה מ-inspect_dialog.py (27/07/2026), עמדה 5, גרסה 9.10.85.

⚙️ כיול חי: כל הפרמטרים הרגישים (נתיב תפריט, קואורדינטות כפתורים owner-drawn)
   נטענים מ-GreenOS (agent-config → tuning) עם ברירות-מחדל כאן. כך מכיילים
   מהשרת בלי להוריד את הקוד מחדש למכונה.
"""
import time

from pywinauto import Application, Desktop, mouse

DRIVER_VERSION = "2026-08-07.40"          # מודפס ע"י הסוכן — לוודא איזו גרסה רצה
POS_TITLE_RE = ".*אורדר.*"
FORM_TITLE = "הורדה מהמלאי"
ITEM_TITLE = "הורדה מהמלאי - פעולה חדשה"
UI_MAP_READY = True

# ── ברירות-מחדל (ניתנות לדריסה מ-tuning בשרת) ────────────────────────
DEFAULT_TUNING = {
    # נתיב התפריט אל טופס ההורדה (הורדה מהמלאי תחת תת-תפריט "תנועות מלאי"):
    "menu_path": "מלאי->תנועות מלאי->הורדה מהמלאי/החזרה לספק",
    "branch_city": "מחסן\\מרלוג",
    # קואורדינטות מסך של כפתורים owner-drawn (מרכז מלבן) — לכיול:
    "emp_set_rect":  [35, 321, 96, 354],    # "הצב" בטופס
    "start_rect":    [491, 417, 596, 458],  # "התחל פעולה" בטופס
    "cancel_rect":   [363, 417, 468, 458],  # "ביטול" בטופס
    "add_rect":      [623, 328, 904, 353],  # "הורד מהמלאי" במסך פריטים
    "finish_rect":   None,                  # "סיים פעולה" — יימדד בכיול
    # ── פופאפ "בחר שם עובד" ──
    # חלון **ללא כותרת** שכל כפתוריו owner-drawn **בלי טקסט** → חייבים מיקום.
    # נמדד 07/08/2026; העוגן הוא ה-Frame הפנימי, וכל המיקומים מוסטים אוטומטית
    # אם הפופאפ ייפתח במקום אחר (ראה _click_emp_button).
    # אינדקס הכפתור ברשת (סדר קריאה RTL: 0 = ימני-עליון). משמש רק אם הפופאפ
    # מסרב להיסגר. ⚠️ מוסיפים/מסירים עובד בקופה → האינדקסים זזים; מעדכנים כאן
    # (או מהשרת ב-tuning.emp_index) לפי מה שהאימות העצמי מדווח.
    "emp_index": {"אודי": 0, "אורי": 1, "אירה": 2, "אסי": 3,
                  "אתי": 4, "יעקב.ע": 5, "קטיה": 6, "שמואל": 7},
    "popup_frame_origin": [533, 320],
    "emp_buttons": {
        "אודי":   [1241, 324, 1378, 462],
        "אורי":   [1099, 324, 1236, 462],
        "אירה":   [958, 324, 1095, 462],
        "אסי":    [816, 324, 953, 462],
        "אתי":    [675, 324, 812, 462],
        "יעקב.ע": [533, 324, 670, 462],
        "קטיה":   [1241, 466, 1378, 604],
        "שמואל":  [1099, 466, 1236, 604],
    },
}

# control-ids קבועים (מהמיפוי, לא משתנים):
F_OPTYPE_UPDATE = 5     # OptionButton "עדכון מלאי" (חובה)
F_BRANCH_RADIO  = 15    # OptionButton "בחר סניף:"
F_BRANCH_COMBO  = 14    # ComboBox הסניפים
F_DESC          = 8     # TextBox תיאור פעולה (הסכום)
F_EMP_NO        = 3     # TextBox מס' עובד
F_EMP_NAME      = 2     # TextBox שם עובד
# מסך הפריטים ("הורדה מהמלאי - פעולה חדשה"):
I_CODE          = 15    # TextBox "קוד פריט/סריאלי" (הוורוד, למעלה)
I_STOCK_NOW     = 21    # TextBox "מלאי נוכחי" — לקריאה בלבד! ⛔ לא הכמות
I_QTY           = 22    # TextBox "כמות" ← זה השדה שממלאים
I_NOTE          = 14    # TextBox "הערה" (הרחב)


_APP = None          # האפליקציה המחוברת — כדי שאיתור דיאלוגים לא יהיה תלוי בקורא

DESKTOP_LOCKED_MSG = (
    "מסך המחשב נעול — אי אפשר להזין לקופה. כפתורי הקופה מצוירים ודורשים עכבר "
    "אמיתי, שלא פועל על מסך נעול. הפעולה נשארה בתור ותתבצע כשהמסך ייפתח. "
    "לתפעול 24/7: להשאיר את המחשב מחובר ולא נעול.")


def _desktop_active():
    """האם יש שולחן עבודה פעיל (המסך לא נעול / הסשן לא מנותק).

    ⚠️ קריטי: ההזנה לקופה מבוססת על **קליק פיזי** (הכפתורים owner-drawn ואינם
    מגיבים ללחיצה בהודעה — ניסיון כזה שבר את 'התחל פעולה' בעבר). על מסך נעול
    כל הזזת עכבר נכשלת, ולכן עדיף לזהות מראש ולומר זאת בבירור מאשר להיכשל
    באמצע הזנה עם הודעה באנגלית (נצפה 07/08: הסשן ננעל ושתי הרצות נפלו)."""
    try:
        import win32api
        x, y = win32api.GetCursorPos()
        win32api.SetCursorPos((x, y))        # נכשל בדיוק כשאין שולחן עבודה פעיל
        return True
    except Exception:                        # noqa: BLE001
        return False


def connect():
    global _APP
    app = Application(backend="win32").connect(title_re=POS_TITLE_RE, timeout=15)
    _APP = app
    return app, app.window(title_re=POS_TITLE_RE)


def _wait_until(pred, timeout=2.0, step=0.1):
    """ממתין לתנאי במקום sleep קבוע. מחזיר True אם התקיים.
    ⏱️ זה מה שהופך את ההרצה למהירה: רוב ההמתנות הקבועות היו 'מספיק לגרוע ביותר',
    ובפועל הקופה מגיבה הרבה יותר מהר."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            if pred():
                return True
        except Exception:                # noqa: BLE001
            pass
        time.sleep(step)
    return False


# הסוכן מציב כאן את פונקציית הלוג שלו, כדי שהודעות הדרייבר (כולל שורת הזמנים)
# ייכנסו גם ל-agent.log ולא ייעלמו עם החלון.
LOG = None


def _log(msg):
    if LOG:
        try:
            LOG(msg)
            return
        except Exception:                # noqa: BLE001
            pass
    print(msg, flush=True)


# ⏱️ מדידת זמנים — מודפס בסוף כל הרצה כדי לראות בדיוק איפה הזמן נשרף
_TIMING = []


def _lap(tag, t0):
    # ⚠️ תוויות באנגלית בכוונה: שורת הזמנים נקראת במסוף Windows, ובתצוגת RTL
    # תוויות עבריות ומספרים מתערבבים עד שאי אפשר לדעת איזה מספר שייך למי.
    dt = time.time() - t0
    _TIMING.append("%s=%.1f" % (tag, dt))
    return time.time()


def _spec(app, title, timeout=8):
    """WindowSpecification לחלון של הקופה לפי כותרת מדויקת (יש לו child_window,
    בניגוד ל-wrapper מ-Desktop().windows()). None אם לא הופיע בזמן."""
    w = app.window(title=title)
    try:
        return w if w.exists(timeout=timeout) else None
    except Exception:                    # noqa: BLE001
        return None


def _click_rect(win, rect):
    try:
        win.set_focus()
    except Exception:                    # noqa: BLE001
        pass
    cx = (rect[0] + rect[2]) // 2
    cy = (rect[1] + rect[3]) // 2
    mouse.click(coords=(cx, cy))


def _child(win, cid):
    """מאתר פקד לפי control_id ע"י מעבר על descendants (עובד גם עם 32-bit app
    ו-64-bit Python, בניגוד ל-child_window(control_id=) שנכשל שם)."""
    for c in win.descendants():
        try:
            if c.control_id() == cid:
                return c
        except Exception:                # noqa: BLE001
            continue
    raise RuntimeError("לא נמצא control_id=%s בטופס" % cid)


def _all_ctrls(app):
    """כל הפקדים בכל חלונות הקופה — **כולל חלונות ללא כותרת**. פופאפ 'בחר שם עובד'
    הוא חלון כזה (כותרת ריקה), ולכן חיפוש לפי כותרת פספס אותו לגמרי."""
    out = []
    for w in app.windows(visible_only=True, enabled_only=False):
        try:
            out.append(w)
            out.extend(w.descendants())
        except Exception:                # noqa: BLE001
            continue
    return out


def _find_by_text(app, text):
    """מאתר פקד לפי טקסט: התאמה מדויקת, ואז 'מתחיל ב-', ואז מילה ראשונה
    (שם בקופה עשוי להיות 'יוסי ספגה' מול 'יוסי' אצלנו)."""
    text = (text or "").strip()
    if not text:
        return None
    ctrls = _all_ctrls(app)
    first = text.split()[0]
    for match in (lambda t: t == text,
                  lambda t: t.startswith(text) or text.startswith(t),
                  lambda t: t == first or t.startswith(first)):
        for c in ctrls:
            try:
                t = (c.window_text() or "").strip()
                if t and match(t):
                    return c
            except Exception:            # noqa: BLE001
                continue
    return None


def _find_popup(app):
    """פופאפ 'בחר שם עובד' — חלון ThunderRT6FormDC **ללא כותרת**. מודאלי: כל עוד
    הוא פתוח הוא חוסם כל לחיצה על הטופס שמאחוריו (וזה נראה ככשל שקט)."""
    for w in app.windows(visible_only=True, enabled_only=False):
        try:
            if w.class_name() == "ThunderRT6FormDC" and not (w.window_text() or "").strip():
                return w
        except Exception:                # noqa: BLE001
            continue
    return None


def _click_emp_button(popup, emp_name, T):
    """לוחץ על כפתור העובד בפופאפ. הכפתורים owner-drawn ללא טקסט → לפי מיקום,
    עם היסט אוטומטי לפי מיקום ה-Frame בפועל (כך שהזזת הפופאפ לא שוברת)."""
    rect = (T.get("emp_buttons") or {}).get(emp_name)
    if not rect:
        # התאמה גמישה: 'יוסי' מול 'יוסי ספגה' וכד'
        first = emp_name.split()[0] if emp_name else ""
        for k, v in (T.get("emp_buttons") or {}).items():
            if k == first or k.startswith(first) or first.startswith(k):
                rect = v
                break
    if not rect:
        raise RuntimeError("אין מיקום מוגדר לכפתור העובד '%s' בפופאפ "
                           "(הוסף ל-tuning.emp_buttons)" % emp_name)
    dx = dy = 0
    try:                                 # היסט לפי ה-Frame הפנימי (עוגן המדידה)
        for c in popup.descendants():
            if c.class_name() == "ThunderRT6Frame":
                r = c.rectangle()
                o = T.get("popup_frame_origin") or [r.left, r.top]
                dx, dy = r.left - o[0], r.top - o[1]
                break
    except Exception:                    # noqa: BLE001
        pass
    _click_rect(popup, [rect[0] + dx, rect[1] + dy, rect[2] + dx, rect[3] + dy])


_YES = ("כן", "&כן", "Yes", "&Yes", "אישור", "&אישור", "OK", "&OK")
_NO = ("ביטול", "&ביטול", "לא", "&לא", "Cancel", "&Cancel", "No", "&No")


def _norm(t):
    return (t or "").replace("&", "").strip()


def _dialog_candidates():
    """כל החלונות שעשויים להיות תיבת אישור.

    ⚠️ לא רק #32770! שאלת היציאה של הקופה ("בבקשה אשר" · כן/ביטול) היא **טופס
    VB6 מותאם**, לא MsgBox תקני — הצירוף כן+ביטול בכלל לא קיים ב-MsgBox. לכן
    חיפוש לפי מחלקה בלבד פשוט לא מצא אותה, ה"כן" מעולם לא נלחץ, ומסך הפריטים
    נשאר פתוח (אסי, 07/08). כאן סורקים גם את חלונות האפליקציה עצמה."""
    out, seen = [], set()
    try:
        for w in Desktop(backend="win32").windows():
            try:
                if w.class_name() == "#32770" and w.is_visible():
                    out.append(w)
                    seen.add(w.handle)
            except Exception:            # noqa: BLE001
                continue
    except Exception:                    # noqa: BLE001
        pass
    if _APP is not None:
        try:
            for w in _APP.windows(visible_only=True, enabled_only=False):
                try:
                    if w.handle in seen:
                        continue
                    title = (w.window_text() or "").strip()
                    if "אורדר" in title or title in (ITEM_TITLE, FORM_TITLE):
                        continue         # החלון הראשי / מסכי העבודה — לא דיאלוג
                    out.append(w)
                except Exception:        # noqa: BLE001
                    continue
        except Exception:                # noqa: BLE001
            pass
    return out


def _click_button(c):
    """לחיצה על כפתור — קלט עכבר אמיתי, ובנפילה גם הודעת BM_CLICK ישירה
    (עובדת גם כשהחלון אינו בחזית או שמשהו מסתיר אותו)."""
    try:
        c.click_input()
        return True
    except Exception:                    # noqa: BLE001
        pass
    try:
        c.click()
        return True
    except Exception:                    # noqa: BLE001
        return False


def _confirm_dialog(yes=True, timeout=6):
    """עונה לשאלת אישור של הקופה בלחיצה על 'כן' / 'ביטול'.
    ⚠️ לא ENTER: בשאלת היציאה ('הפעולה לא הושלמה, האם ברצונך לצאת?') כפתור
    ברירת המחדל הוא **ביטול**, ולכן ENTER היה משאיר אותנו תקועים בפנים."""
    wanted, other = (_YES, _NO) if yes else (_NO, _YES)
    end = time.time() + timeout
    _first = True
    while time.time() < end:
        if not _first:
            time.sleep(0.12)      # סריקת כל חלונות ה-Desktop יקרה — לא בלולאה צמודה
        _first = False
        for w in _dialog_candidates():
            # ⏱️ children() ולא descendants(): כפתורי "כן"/"ביטול" הם ילדים
            # ישירים של הדיאלוג, וסריקת כל הצאצאים של כל חלון עלתה שניות
            # (5.7ש' בשלב "מחק הכל" בלבד). נופלים ל-descendants רק אם לא נמצא.
            try:
                ctrls = list(w.children())
            except Exception:            # noqa: BLE001
                continue
            if not any(_norm(getattr(c, "window_text", lambda: "")()) in
                       [_norm(x) for x in (_YES + _NO)] for c in ctrls):
                try:
                    ctrls = list(w.descendants())
                except Exception:        # noqa: BLE001
                    continue
            texts = []
            target = None
            for c in ctrls:
                try:
                    t = _norm(c.window_text())
                except Exception:        # noqa: BLE001
                    continue
                if t:
                    texts.append(t)
                if target is None and t in [_norm(x) for x in wanted]:
                    target = c
            if target is None:
                # אין התאמת טקסט, אבל יש כאן כפתור "ביטול" → זו תיבת אישור:
                # לוקחים את הכפתור **השני** (זה שאינו הביטול).
                if any(t in [_norm(x) for x in other] for t in texts):
                    btns = [c for c in ctrls
                            if (c.class_name() or "").lower().startswith("button")]
                    alt = [c for c in btns
                           if _norm(c.window_text()) not in [_norm(x) for x in other]]
                    target = alt[0] if alt else None
                    if target is not None:
                        _log("  (דיאלוג ללא טקסט תואם — נלחץ הכפתור שאינו ביטול: '%s')"
                             % _norm(target.window_text()))
            if target is not None and _click_button(target):
                time.sleep(0.5)
                return True
        time.sleep(0.3)
    return False


# שאלות שהתשובה להן היא **לא**. כל השאר (שמירה/אישור) → כן.
# ⚠️ עד ההרצה החיה הראשונה כל השאלות אחרי "סיים פעולה" היו שאלות שמירה, ולכן
# הקוד ענה "כן" לכולן. "האם להדפיס בהדפסה רחבה?" היא הראשונה שבה כן = מדפסת
# מיותרת (אסי, 07/08). מוסיפים כאן מילת מפתח כשמתגלה שאלה חדשה מסוגה.
# ⚠️ שורש ולא מילה מלאה: השאלה בפועל היא "האם ל**הדפיס** בהדפסה רחבה?" —
# ו-"הדפס" **אינו** תת-מחרוזת של "להדפיס" (יש י' בין פ' לס'). לכן "הדפ".
_ANSWER_NO_KEYWORDS = ("הדפ", "דפיס", "print", "מדפס", "מדבק", "פקס", "מייל",
                       "אימייל", "email", "שלח")


_OK_ONLY = ("OK", "&OK", "אישור", "&אישור", "סגור", "המשך", "Close")


def _dialog_with_buttons():
    """מאתר חלון שממתין לתשובה. מחזיר (חלון, טקסט, סוג):
       'confirm' — יש כן/לא (שאלה)      ·  'ack' — יש רק OK (הודעה)
    ⚠️ שני הסוגים חוסמים באותה מידה: חלון שנשאר פתוח משבית את תפריט הקופה
    ומפיל את ההרצה הבאה. לכן שניהם מטופלים באותה שרשרת."""
    yn = [_norm(x) for x in (_YES + _NO)]
    ok = [_norm(x) for x in _OK_ONLY]
    for w in _dialog_candidates():
        try:
            kids = list(w.children())
        except Exception:                # noqa: BLE001
            continue
        texts = [_norm(c.window_text()) for c in kids]
        has_yn = any(t in yn for t in texts)
        has_ok = any(t in ok for t in texts)
        if not (has_yn or has_ok):
            # ⚠️ נפילה ל-descendants: בטופס VB6 מותאם הכפתורים יושבים בתוך
            # Frame ואינם ילדים ישירים. _confirm_dialog כבר עשה את הנפילה הזאת,
            # וכאן היא נשכחה — כלומר חלון כזה לא זוהה בכלל כשאלה פתוחה.
            try:
                kids = list(w.descendants())
            except Exception:            # noqa: BLE001
                continue
            texts = [_norm(c.window_text()) for c in kids]
            has_yn = any(t in yn for t in texts)
            has_ok = any(t in ok for t in texts)
            if not (has_yn or has_ok):
                continue
        # ⚠️ הטקסט **תמיד** מכל הצאצאים, גם כשהכפתורים נמצאו בילדים הישירים:
        # ההודעה עצמה עשויה לשבת עמוק יותר, ואז body הכיל רק את הכותרת
        # ("הודעת מערכת") — בלי המילה "להדפיס". התוצאה: הוכרע "כן", והקופה
        # ניסתה להדפיס (אסי, 07/08). ההכרעה חייבת להתבסס על הטקסט המלא.
        try:
            texts = [_norm(c.window_text()) for c in w.descendants()]
        except Exception:                # noqa: BLE001
            pass
        msg = " ".join(t for t in texts if t and t not in yn and t not in ok)
        try:
            title = _norm(w.window_text())
        except Exception:                # noqa: BLE001
            title = ""
        body = (title + " " + msg).strip()
        # informative = יש טקסט אמיתי מעבר לכותרת. בקופה הזאת נוסח השאלה מצויר
        # ואינו חשוף כפקד, ולכן לרוב מגיעה רק הכותרת — ואז אסור להכריע לפי תוכן.
        return w, body, ("confirm" if has_yn else "ack"), bool(msg.strip()), title
    return None, "", "", False, ""


# כותרות של שאלות שהתשובה להן "כן" גם כשנוסח השאלה אינו קריא.
# ⚠️ בקופה הזאת נוסח השאלה מצויר ואינו חשוף כפקד — הקוד מקבל רק את הכותרת
# ("הודעת מערכת"), ולכן הכרעה לפי מילות מפתח בנוסח פשוט לא אפשרית (אסי, 07/08).
_TITLE_YES = ("אשר",)          # "בבקשה אשר" = אישור שמירה/יציאה


def _decide_yes(body, informative, title):
    """האם לענות "כן". ⛔ ברירת המחדל לשאלה שלא הצלחנו לקרוא היא **לא**:
    סירוב לפעולה לא מוכרת הוא שחזיר (הפעולה חוזרת לתור ומנוסה שוב), בעוד
    הסכמה יכולה להדפיס/למחוק ולתקוע את הקופה — וזה מה שקרה שוב ושוב."""
    if any(k in body for k in _ANSWER_NO_KEYWORDS):
        return False
    if not informative:
        return any(t in title for t in _TITLE_YES)
    return True


def _click_ok(w):
    """סוגר הודעת OK. לחיצה על הכפתור לפי טקסט, ובנפילה ENTER."""
    ok = [_norm(x) for x in _OK_ONLY]
    try:
        for c in w.children():
            if _norm(c.window_text()) in ok and _click_button(c):
                return True
    except Exception:                    # noqa: BLE001
        pass
    try:
        w.set_focus()
        w.type_keys("{ENTER}")
        return True
    except Exception:                    # noqa: BLE001
        return False


def _answer_smart(timeout=3):
    """עונה לחלון שעל המסך **לפי תוכנו**: הדפסה→לא, הודעה→OK, השאר→כן.

    ⛔ זו הפונקציה היחידה שמותר לה לענות על חלון לא-ידוע. שלושה מקומות שונים
    לחצו "כן" בעיוורון (סיום, יציאה, ניקוי כפוי), וכל אחד מהם הספיק בפני עצמו
    כדי לגרום לקופה להדפיס ולהיתקע — גם אחרי שהמקומות האחרים תוקנו (אסי, 07/08).
    """
    w, body, kind, informative, title = _dialog_with_buttons()
    if w is None:
        return False
    if kind == "ack":
        return _click_ok(w)
    return _confirm_dialog(yes=_decide_yes(body, informative, title), timeout=timeout)


def _items_screen_gone():
    """מסך הפריטים נסגר → הפעולה נסגרה ולא יגיעו עוד חלונות."""
    if _APP is None:
        return False
    try:
        return _spec(_APP, ITEM_TITLE, timeout=0.1) is None
    except Exception:                    # noqa: BLE001
        return False


def _answer_dialog_chain(rounds=10, first_wait=6.0, next_wait=6.0):
    """עונה לשרשרת החלונות שאחרי "סיים פעולה": שמירה→כן, הדפסה→לא, הודעה→OK.

    ⚠️ נבנה מהריצה החיה הראשונה (אסי, 07/08): אחרי השמירה מגיעה שאלת הדפסה,
    ואחרי "לא" מגיעה הודעת "בעיה בהגדרת מדפסת קופה" עם OK. שום שלב מזה לא הופיע
    בהרצות היבשות, כי שם לא לוחצים "סיים פעולה" בכלל.
    מחזיר רשימת (שאלה, תשובה) — **נרשמת ביומן**, כדי שכל חלון חדש שיופיע בשטח
    יזוהה מיד ולא ידרוש ניחוש."""
    answered = []
    for _i in range(rounds):
        # ⏱️ החלונות מופיעים בזה אחר זה, כל אחד רק אחרי שקודמו נסגר — ולכן
        # ממתינים לכל אחד במקום לבדוק פעם אחת ולפספס את השרשרת.
        # ⚠️ החלון הבא מופיע רק אחרי שהקופה סיימה לשמור, וזה לוקח לה כמה שניות.
        # המתנה של 2ש' הספיקה לשאלת השמירה ופספסה את שאלת ההדפסה שאחריה
        # (אסי, 07/08). ממתינים ארוך — אבל יוצאים מיד כשמסך הפריטים נסגר,
        # כי אז בטוח שאין עוד חלונות בדרך.
        wait = first_wait if _i == 0 else next_wait
        if not _wait_until(lambda: _dialog_with_buttons()[0] is not None
                           or _items_screen_gone(), wait, 0.15):
            break
        if _dialog_with_buttons()[0] is None:
            break
        w, body, kind, informative, title = _dialog_with_buttons()
        if w is None:
            break
        if kind == "ack":
            if not _click_ok(w):
                break
            answered.append((body[:70], "OK"))
        else:
            yes = _decide_yes(body, informative, title)
            if not informative:
                _log("  ⚠️ נוסח השאלה לא נקרא ('%s') — הוכרע לפי כותרת: %s"
                     % (title, "כן" if yes else "לא"))
            if not _confirm_dialog(yes=yes, timeout=2):
                break
            answered.append((body[:70], "כן" if yes else "לא"))
        time.sleep(0.4)
    if answered:
        _log("  חלונות הקופה: " + " | ".join("%s → %s" % a for a in answered))
    # ⚠️ אם נשאר חלון פתוח — מדפיסים בדיוק מה יש שם. חלון שנשאר חוסם את ההרצה
    # הבאה, ובלי התיעוד הזה כל אבחון הוא ניחוש (בזבזנו על כך שני סבבים, 07/08).
    if _dialog_with_buttons()[0] is not None:
        _log("  ⚠️ נשאר חלון פתוח אחרי שרשרת הסיום:")
        _dump_dialogs()
    return answered


def _doc_no_from(answered):
    """שולף את מספר התעודה מהודעת הסיום ("פעולה עודכנה בהצלחה. מספר פעולה: 15088").
    זה המזהה שמאפשר להצליב הורדה ב-GreenOS מול התעודה בקופה."""
    import re
    for body, _ans in answered or []:
        m = re.search(r"מספר\s*פעולה\s*[:：]?\s*(\d+)", body)
        if m:
            return m.group(1)
    return ""


def _dialog_open():
    """האם מוצגת כרגע שאלת אישור — כלומר חלון שיש בו כפתור 'כן'/'ביטול'.
    ⚠️ לא לפי מחלקת החלון: שאלת היציאה של הקופה אינה MsgBox תקני."""
    keys = [_norm(x) for x in (_YES + _NO)]
    for w in _dialog_candidates():
        try:
            for c in w.children():       # ⏱️ ילדים ישירים — זול; ראה _confirm_dialog
                if _norm(c.window_text()) in keys:
                    return True
        except Exception:                # noqa: BLE001
            continue
    return False


def _dump_dialogs(tag=""):
    """מדפיס מה באמת פתוח על המסך — כדי שכשל ייתן לנו עובדות, לא ניחוש."""
    for w in _dialog_candidates():
        try:
            title = (w.window_text() or "").strip()
            cls = w.class_name()
            texts = []
            for c in w.descendants():
                t = _norm(c.window_text())
                if t and t not in texts:
                    texts.append(t)
            _log("  🔍 %sחלון '%s' [%s]: %s" % (tag, title, cls, " | ".join(texts[:10])))
        except Exception:                # noqa: BLE001
            continue


def _exit_item_screen(app, items_win, tries=4):
    """יוצא ממסך הפריטים **ומוודא שהוא באמת נסגר**.

    ⚠️ למה זה קריטי: מסך פריטים שנשאר פתוח משבית את תפריט הקופה, וכל הרצה
    הבאה נופלת ב-ElementNotEnabled (זה מה שהפיל את #27 עשרות פעמים). בנוסף,
    בשאלת היציאה ברירת המחדל היא **ביטול** — ENTER או ESC רק ישאירו אותנו בפנים.
    לכן: ESC → ממתינים שהשאלה תופיע → לוחצים "כן" מפורשות → מאמתים.
    מחזיר True אם המסך נסגר."""
    for i in range(tries):
        if _spec(app, ITEM_TITLE, timeout=0.3) is None:
            return True
        try:
            items_win.set_focus()
            items_win.type_keys("{ESC}")
        except Exception:                # noqa: BLE001
            pass
        if not _wait_until(_dialog_open, 2.0, 0.15):
            # ESC לא הרים את השאלה → לוחצים "יציאה" (הכפתור התחתון בעמודה הימנית)
            try:
                cands = [(c, c.rectangle()) for c in items_win.descendants()
                         if c.class_name() == "ThunderRT6UserControlDC"]
                if cands:
                    _click_ctrl(max(cands, key=lambda cr: (cr[1].top, cr[1].left))[0])
            except Exception:            # noqa: BLE001
                pass
            _wait_until(_dialog_open, 2.5, 0.15)
        if not _answer_smart(timeout=3):
            _dump_dialogs("יציאה %d: " % (i + 1))
            # נפילה אחרונה: מזיזים פוקוס לכפתור השני ומקישים רווח. ברירת המחדל
            # היא 'ביטול', ולכן TAB מעביר אל 'כן'.
            try:
                from pywinauto.keyboard import send_keys as _sk3
                _sk3("{TAB}")
                time.sleep(0.2)
                _sk3("{SPACE}")
            except Exception:            # noqa: BLE001
                pass
        # ⛔ רק כשאין שאלת אישור פתוחה: _dismiss_message_box מקיש ENTER, ובשאלת
        # היציאה ENTER = "ביטול" — כלומר היינו סוגרים לעצמנו את הדלת.
        if not _dialog_open():
            _dismiss_message_box()
        time.sleep(0.4)
    return _spec(app, ITEM_TITLE, timeout=0.5) is None


NEW_ITEM_TITLE = "פריט חדש"


def _guard_new_item(app, sku):
    """⛔ הגנה: אם הקופה פתחה 'פריט חדש' — המק"ט לא נמצא אצלה. סוגרים ב'ביטול'
    ועוצרים. **אסור בשום מצב ליצור מוצר בקופה** (קרה 07/08: הקוד נכנס חלקי,
    הקופה הציעה מוצר חדש, והסוכן הקליד לתוכו את הכמות כשם המוצר)."""
    # 0.5ש' מספיק: החלון נפתח מיד אחרי Enter, וכבר המתנּו אחריו. timeout ארוך
    # כאן היה עולה 2 שניות **לכל פריט** בלי שום תועלת.
    w = _spec(app, NEW_ITEM_TITLE, timeout=0.5)
    if w is None:
        return
    try:
        for c in w.descendants():
            if (c.window_text() or "").strip() in ("ביטול", "Cancel"):
                c.click_input()
                time.sleep(0.8)
                break
    except Exception:                    # noqa: BLE001
        pass
    _answer_smart(timeout=3)
    raise RuntimeError("הקופה לא מזהה את המק\"ט '%s' ופתחה 'פריט חדש' — בוטל. "
                       "בדוק שהמק\"ט קיים בקופה." % sku)


def _clear_field(c):
    """מרוקן שדה טקסט של VB6. ⚠️ לא Ctrl+A — הוא לא תמיד בוחר-הכל שם, ואז
    הערך החדש נדבק לישן והקוד נכנס חלקי (ברקוד '6520' במקום '516520')."""
    from pywinauto.keyboard import send_keys
    try:
        c.set_edit_text("")
    except Exception:                    # noqa: BLE001
        pass
    c.click_input()
    time.sleep(0.08)
    # ⏱️ pause מפורש: ברירת המחדל של send_keys היא 0.05ש' **לכל תו**, כלומר
    # 50 תווי מחיקה = 2.5ש' מבוזבזים בכל ניקוי שדה. 24 מספיקים (סריאל=15 תווים).
    send_keys("{END}" + "{BACKSPACE}" * 24 + "{DELETE}" * 6, pause=0.005)
    time.sleep(0.08)


def _type_code_verified(c, code, tries=4, start_pace=0):
    """מקליד קוד/סריאל לשדה **ומוודא שהוא נקלט במלואו** לפני Enter.

    ⚠️ למה: אחרי שפריט נוסף לרשימה הקופה עסוקה רגע, והתווים הראשונים של הקוד
    הבא נבלעים — סריאל 863631087396667 נקלט כ-087396667 ואז נפתח "פריט חדש"
    (אסי, 07/08). קוראים בחזרה ומנסים שוב עד שזהה.

    ⏱️ מהיר-קודם: מתחילים בהקלדה מהירה, ורק אם היא לא נקלטה במלואה מאטים.
    ברוב הפעמים המהירה עובדת — וזה חוסך ~1ש' לכל פריט מול pause קבוע של 0.06.
    """
    from pywinauto.keyboard import send_keys
    code = str(code)
    paces = [0.015, 0.045, 0.08, 0.12]
    got = ""
    for i in range(tries):
        _clear_field(c)
        send_keys(code, pause=paces[min(i + start_pace, len(paces) - 1)])
        time.sleep(0.15 + 0.1 * i)           # הקלדה מהירה → בדיקה מהירה
        # ⏱️ 6 בדיקות (~0.5ש') ולא יותר: נמדד (07/08) שהארכה ל-14 רק **הוסיפה**
        # זמן — כשההקלדה הראשונה נבלעת היא לא "מופיעה מאוחר", היא פשוט אבדה,
        # ועדיף להיכשל מהר ולהקליד שוב. השורש טופל בהמתנה לקופה פנויה, למטה.
        for _ in range(6):                   # קריאה-חוזרת עד שהערך מתייצב
            try:
                got = (c.window_text() or "").strip()
            except Exception:                # noqa: BLE001
                got = ""
            if got == code:
                return
            time.sleep(0.08)
    raise RuntimeError("הקוד '%s' לא נקלט במלואו בשדה (התקבל '%s') — "
                       "נעצר לפני Enter כדי לא לפתוח 'פריט חדש'" % (code, got))


def _type_text(c, text):
    """מקליד ערך לשדה **בהקלדה אמיתית**.

    ⚠️ למה לא set_edit_text: VB6 מעדכן את הערך הפנימי של הפקד רק מאירועי
    מקלדת. set_edit_text משנה רק את מה שמוצג — הכמות נראתה '1' על המסך
    והקופה בכל זאת ענתה 'ערך לא חוקי בשדה כמות' (אסי, 07/08).
    TAB בסוף מאלץ את הקופה לאמת ולקבע את הערך."""
    from pywinauto.keyboard import send_keys
    _clear_field(c)
    send_keys(str(text), pause=0.03)
    time.sleep(0.12)
    send_keys("{TAB}")
    time.sleep(0.2)


def _item_action_buttons(win):
    """כפתורי הפעולה במסך הפריטים, מימין לשמאל:
    [0]=סיים פעולה · [1]=הורד מהמלאי · [2]=עדכן · [3]=מחק · [4]=מחק הכל.
    מאותרים כשורה עם הכי הרבה כפתורים מצוירים — יציב גם אם החלון זז."""
    try:
        rects = [(c, c.rectangle()) for c in win.descendants()
                 if c.class_name() == "ThunderRT6UserControlDC"]
        rows = {}
        for c, r in rects:
            rows.setdefault(round(r.top / 12) * 12, []).append((c, r))
        if not rows:
            return []
        row = max(rows.values(), key=len)
        row.sort(key=lambda cr: -cr[1].left)
        return [c for c, _ in row]
    except Exception:                    # noqa: BLE001
        return []


def _is_selected(c):
    """האם כפתור-הרדיו מסומן. VB6 OptionButton עונה ל-BM_GETCHECK (0xF0)."""
    try:
        import win32gui
        import win32con
        return bool(win32gui.SendMessage(c.handle, 0x00F0, 0, 0))   # BM_GETCHECK
    except Exception:                    # noqa: BLE001
        try:
            return bool(c.get_check_state())
        except Exception:                # noqa: BLE001
            return False


def _select_option(c):
    """מסמן OptionButton של VB6 — **בלי לחיצות עכבר לפי מיקום**.

    ⛔ למה: גרסה קודמת ניסתה גם קליקים על קואורדינטות אחרי הלחיצה ההודעתית,
    והם נחתו על כפתור-הרדיו השכן — כלומר *ביטלו* את הבחירה שכבר הצליחה
    ('נבחר עדכון מלאי ואז חזר להחזרה לספק', אסי 07/08). שתי השיטות כאן
    חסינות-מיקום: הודעת click, ואז פוקוס + רווח."""
    # ⚠️ **קואורדינטות יחסיות בלבד.** rectangle() של פקדי הטופס הזה אינו במרחב
    # המסך (המיפוי דיווח על הטופס בפינה השמאלית-עליונה בעוד שהוא מוצג ממורכז),
    # ולכן mouse.click על מיקום מוחלט נחת במקום שגוי — הרדיו "נבחר ונעלם"
    # (אסי, 07/08). click_input עם coords יחסיים מבצע את ההמרה בעצמו.
    for rel in ("center", "circle"):
        try:
            r = c.rectangle()
            w, h = r.right - r.left, r.bottom - r.top
            if rel == "center":
                c.click_input()
            else:                        # RTL: העיגול בקצה הימני של הפקד
                c.click_input(coords=(max(w - 8, 1), max(h // 2, 1)))
            time.sleep(0.3)
            return
        except Exception:                # noqa: BLE001
            continue
    try:                                 # נפילה אחרונה: מקלדת
        from pywinauto.keyboard import send_keys
        c.set_focus()
        send_keys(" ")
    except Exception:                    # noqa: BLE001
        pass
    time.sleep(0.2)


def _dismiss_message_box():
    """סוגר **הודעה** (חלון עם OK בלבד). מחזיר את הטקסט שהוצג.

    ⛔ לא נוגע בשאלה עם כן/לא. הגרסה הקודמת הקישה ENTER על כל חלון #32770,
    ו-ENTER בשאלת "האם להדפיס בהדפסה רחבה?" הוא **כן** (כפתור ברירת המחדל) —
    כלומר גם כששרשרת הסיום ענתה "לא" כמו שצריך, השורה הזאת לחצה "כן" אחריה
    והקופה נתקעה על "ממתין לחיבור מדפסת" (אסי, 07/08). שאלות שייכות אך ורק
    ל-_answer_dialog_chain, שמכריע לפי תוכן."""
    w, body, kind, informative, title = _dialog_with_buttons()
    if w is None:
        return _read_message_box()
    if kind == "confirm":
        return body                      # שאלה — משאירים לשרשרת
    _click_ok(w)
    time.sleep(0.3)
    return body


def _read_message_box():
    """קורא טקסט מתיבת הודעה של Windows (#32770) אם מוצגת — כך שהשגיאה שלנו
    תכיל את מה שהקופה בעצם אמרה, במקום ניחוש."""
    try:
        for w in Desktop(backend="win32").windows():
            try:
                if w.class_name() != "#32770" or not w.is_visible():
                    continue
                parts = []
                for c in w.descendants():
                    t = (c.window_text() or "").strip()
                    if t and t not in parts:
                        parts.append(t)
                if parts:
                    return " | ".join(parts[:6])
            except Exception:            # noqa: BLE001
                continue
    except Exception:                    # noqa: BLE001
        pass
    return ""


def _emp_grid(popup):
    """רשת כפתורי העובדים **כפי שהיא כרגע** (נקראת חיה מהפופאפ, לא ממיקומים
    שנמדדו בעבר — הפופאפ עשוי להיפתח במקום אחר או להשתנות כשמוסיפים עובד).
    מחזיר את הכפתורים בסדר קריאה RTL: שורה אחר שורה, בכל שורה מימין לשמאל.
    ⛔ שורת [חדש/ביטול] התחתונה מוחרגת, וכך גם פקדים זעירים/דקורטיביים."""
    try:
        rects = [(c, c.rectangle()) for c in popup.descendants()
                 if c.class_name() == "ThunderRT6UserControlDC"]
        if not rects:
            return []
        bottom = max(r.top for _, r in rects)
        grid = [(c, r) for c, r in rects if r.top < bottom - 40]
        if grid:                          # מסננים פקדים נמוכים מהכפתורים האמיתיים
            h = max(r.bottom - r.top for _, r in grid)
            grid = [(c, r) for c, r in grid if (r.bottom - r.top) >= h * 0.8]
        grid.sort(key=lambda cr: (cr[1].top, -cr[1].left))
        return [c for c, _ in grid]
    except Exception:                    # noqa: BLE001
        return []


def _bottom_row(win):
    """הפקדים בשורה התחתונה של החלון, ממוינים **מימין לשמאל** (סדר RTL)."""
    try:
        rects = [(c, c.rectangle()) for c in win.descendants()
                 if c.class_name() == "ThunderRT6UserControlDC"]
        if not rects:
            return []
        bottom = max(r.top for _, r in rects)
        row = [(c, r) for c, r in rects if abs(r.top - bottom) <= 12]
        row.sort(key=lambda cr: -cr[1].left)
        return [c for c, _ in row]
    except Exception:                    # noqa: BLE001
        return []


def _dismiss_popup(app, popup):
    """סוגר את פופאפ בחירת העובד בלי לבחור עובד. שלוש שכבות, מהבטוח לפחות:
    ESC → סגירת חלון (WM_CLOSE, כמו ה-X) → 'ביטול'.
    ⚠️ 'ביטול' מאותר לפי מיקום **יחסי**: בשורה התחתונה הסדר מימין הוא
    [חדש, ביטול, ...] — ולכן השני מימין. ⛔ לעולם לא הראשון מימין: זה 'חדש',
    ולחיצה עליו פותחת יצירת עובד חדש (קרה 07/08)."""
    # ⏱️ בדיקה צפופה במקום sleep קבוע: בפועל ה-ESC סוגר תוך פחות מחצי שנייה,
    # וההמתנות הקבועות (3.6ש' במקרה הגרוע) נשרפו כמעט תמיד לחינם.
    for attempt in range(3):
        try:
            popup.set_focus()
            popup.type_keys("{ESC}")
        except Exception:                # noqa: BLE001
            pass
        if _wait_until(lambda: _find_popup(app) is None, 1.0, 0.1):
            return
        try:
            popup.close()                # WM_CLOSE — שקול ללחיצה על ה-X
        except Exception:                # noqa: BLE001
            pass
        if _wait_until(lambda: _find_popup(app) is None, 1.0, 0.1):
            return
        # ⛔ **אין ללחוץ על שורת הכפתורים התחתונה.** "חדש" יושב שם צמוד ל"ביטול",
        # ולחיצה שמפספסת פותחת יצירת עובד חדש ומקלידה לתוכה (קרה פעמיים, 07/08).
        # אם ESC/סגירה לא הצליחו — הקורא בוחר עובד מהרשת, וזו התוצאה הרצויה ממילא.


def _bottom_right_button(win):
    """הכפתור הימני-ביותר בשורה התחתונה של חלון — ב-RTL זה כפתור האישור
    ('התחל פעולה' מול 'ביטול'). owner-drawn: בלי טקסט ובלי id, ולכן מאתרים לפי
    מיקום יחסי בתוך החלון — יציב גם אם החלון זז."""
    try:
        cands = [c for c in win.descendants()
                 if c.class_name() == "ThunderRT6UserControlDC"]
        if not cands:
            return None
        rects = [(c, c.rectangle()) for c in cands]
        bottom = max(r.top for _, r in rects)
        row = [(c, r) for c, r in rects if abs(r.top - bottom) <= 12]
        return max(row, key=lambda cr: cr[1].left)[0] if row else None
    except Exception:                    # noqa: BLE001
        return None


def _err(e):
    """טקסט שגיאה קריא — pywinauto זורק לעיתים חריגות עם הודעה ריקה."""
    s = str(e).strip()
    return "%s: %s" % (type(e).__name__, s) if s else type(e).__name__


def _click_ctrl(c):
    """לחיצה על **כפתור מצויר** (ThunderRT6UserControlDC וכד') — קליק פיזי.

    ⚠️ שני סוגי פקדים, שתי שיטות הפוכות, ואסור לערבב:
    • כפתור-רדיו (OptionButton) → הודעת click עובדת; קליק פיזי פוגע בשכן
      ומבטל את הבחירה. ← ראה _select_option.
    • כפתור מצויר (UserControl) → הודעת click "מצליחה" אך **לא עושה כלום**
      (אין לו טיפול בהודעה), ולכן חייבים קליק פיזי. ניסיון להעדיף הודעה כאן
      שבר את 'התחל פעולה' בגרסה .7."""
    try:
        c.click_input()
        return
    except Exception as e1:              # noqa: BLE001
        try:
            r = c.rectangle()
            mouse.click(coords=((r.left + r.right) // 2, (r.top + r.bottom) // 2))
            return
        except Exception:                # noqa: BLE001
            raise RuntimeError(_err(e1))


def _set_text(c, text):
    """מילוי שדה טקסט: set_edit_text (מהיר) ואם נכשל — קליק + הקלדה אמיתית."""
    try:
        c.set_edit_text(text)
        return
    except Exception:                    # noqa: BLE001
        pass
    _click_ctrl(c)
    time.sleep(0.15)
    try:
        c.type_keys("^a{DELETE}", set_foreground=False)
    except Exception:                    # noqa: BLE001
        pass
    from pywinauto.keyboard import send_keys
    send_keys(str(text).replace("(", "{(}").replace(")", "{)}").replace("+", "{+}")
              .replace("^", "{^}").replace("%", "{%}").replace("~", "{~}"),
              with_spaces=True, pause=0.02)


def _force_close_extra(app, pos):
    """סוגר **כל** חלון של הקופה שאינו החלון הראשי.

    ⚠️ למה זה קיים: _cleanup סוגר רק חלונות ששמם ידוע לנו. חלון שלא הכרנו
    ('פריט חדש', דיאלוג עובד חדש, כל טופס אחר שנשאר פתוח) נשאר על המסך, מבטל
    את תפריט הקופה, וכל ניסיון הזנה נופל ב-ElementNotEnabled — שוב ושוב, בלי
    שהמערכת מצליחה להיחלץ לבד (פעולה #27 נכשלה כך 26 פעמים, אסי 07/08).
    מחזיר כמה חלונות נסגרו."""
    try:
        main = pos.handle
    except Exception:                    # noqa: BLE001
        main = None
    closed = 0
    for _ in range(4):
        extras = []
        try:
            for w in app.windows(visible_only=True, enabled_only=False):
                try:
                    if main is not None and w.handle == main:
                        continue
                    extras.append(w)
                except Exception:        # noqa: BLE001
                    continue
        except Exception:                # noqa: BLE001
            break
        if not extras:
            break
        for w in extras:
            try:
                w.set_focus()
                w.type_keys("{ESC}")
                time.sleep(0.3)
            except Exception:            # noqa: BLE001
                pass
            try:
                if w.exists():
                    w.close()
                    time.sleep(0.3)
            except Exception:            # noqa: BLE001
                pass
            # עונים לפי תוכן — לא "כן" עיוור (ראה _answer_smart)
            _answer_smart(timeout=1.5)
            _dismiss_message_box()
            closed += 1
        time.sleep(0.3)
    return closed


def _cleanup(app, T):
    """סוגר שאריות דיאלוגים מהרצה קודמת שנכשלה (טופס/מסך פריטים/פופאפ עובד),
    כדי שהתפריט יהיה פנוי. ריצה שנכשלה משאירה חלון פתוח שחוסם את הבא."""
    _dismiss_message_box()          # תיבת הודעה תקועה חוסמת הכל
    # מסך הפריטים אינו נסגר ב-ESC — לוחצים "יציאה" (הכפתור התחתון בעמודה הימנית).
    # בלעדיו הוא נשאר פתוח וחוסם את תפריט הקופה בריצה הבאה (ElementNotEnabled).
    try:
        iw = _spec(app, ITEM_TITLE, timeout=0.4)
        if iw is not None:
            # ⚠️ "כן" מפורש ואימות סגירה — ברירת המחדל בשאלת היציאה היא **ביטול**,
            # ומסך שנשאר פתוח משבית את תפריט הקופה בהרצה הבאה.
            _exit_item_screen(app, iw)
    except Exception:                    # noqa: BLE001
        pass
    # ⏱️ timeout קצר: בקופה נקייה החלונות לא קיימים, וכל בדיקה עם timeout=1
    # הייתה עולה שנייה שלמה — ~4ש' קבועות בכל הרצה בלי שום סיבה.
    for _ in range(3):
        closed = False
        for title in (ITEM_TITLE, FORM_TITLE, "בחר שם עובד"):
            w = _spec(app, title, timeout=0.3)
            if w is None:
                continue
            closed = True
            try:
                w.set_focus()
                w.type_keys("{ESC}")
                time.sleep(0.4)
            except Exception:            # noqa: BLE001
                pass
            # אם ESC לא סגר את הטופס — לחיצה על "ביטול"
            if title == FORM_TITLE and _spec(app, FORM_TITLE, timeout=0.4):
                _click_rect(w, T["cancel_rect"])
                time.sleep(0.4)
        if not closed:
            break


def recover(tuning=None):
    """ניקוי מצב אחרי כשל — סוגר כל שארית מסך/דיאלוג בקופה.
    נקרא ע"י הסוכן מיד אחרי שגיאה, כדי שהניסיון החוזר (שתמיד יגיע) יתחיל נקי
    ולא ייתקל ב-ElementNotEnabled בגלל חלון שנשאר פתוח."""
    T = dict(DEFAULT_TUNING)
    if tuning:
        T.update({k: v for k, v in tuning.items() if v is not None})
    app, pos = connect()
    try:
        pos.set_focus()
    except Exception:                    # noqa: BLE001
        pass
    _cleanup(app, T)
    _force_close_extra(app, pos)         # גם חלונות שלא מוכרים לנו בשם
    return True


def apply_removal(removal, dry_run=True, screenshot_path=None, tuning=None):
    """מבצע פעולת הורדה אחת. dry_run=True → ממלא אבל לא שומר (יוצא ב-ESC).
    tuning: dict מהשרת שדורס את DEFAULT_TUNING. מחזיר מס' תעודה ('' ב-dry)."""
    T = dict(DEFAULT_TUNING)
    if tuning:
        T.update({k: v for k, v in tuning.items() if v is not None})

    del _TIMING[:]
    _run0 = time.time()
    _t = _run0
    # ⚠️ הגרסה בכל הרצה, לא רק בהפעלה: פעמיים נותחו כשלים על סמך ההנחה שרצה
    # הגרסה החדשה, בזמן שהסוכן לא הופעל מחדש (07/08). שורה אחת שמסיימת ויכוח.
    _log("  דרייבר %s" % DRIVER_VERSION)

    # ⛔ בודקים **לפני** שנוגעים בקופה: על מסך נעול ההזנה תיכשל באמצע ותשאיר
    # טופס פתוח שיחסום גם את ההרצה הבאה. עדיף לא להתחיל.
    if not _desktop_active():
        raise RuntimeError(DESKTOP_LOCKED_MSG)

    app, pos = connect()
    pos.set_focus()

    # 0) ניקוי שאריות מהרצה קודמת (חלון פתוח חוסם את התפריט)
    _cleanup(app, T)
    pos.set_focus()
    _t = _lap("cleanup", _t)

    # 1) תפריט → טופס הורדה
    # ⚠️ ElementNotEnabled כאן = חלון אחר של הקופה נשאר פתוח ומשבית את התפריט.
    # במקום להיכשל (ולחזור על אותו כשל בכל ניסיון חוזר) — סוגרים הכל ומנסים שוב.
    try:
        pos.menu_select(T["menu_path"])
    except Exception as e:               # noqa: BLE001
        n = _force_close_extra(app, pos)
        try:
            pos.set_focus()
            time.sleep(0.5)
            pos.menu_select(T["menu_path"])
        except Exception as e2:          # noqa: BLE001
            raise RuntimeError(
                "פתיחת תפריט מלאי נכשלה: %s (%s). נסגרו %d חלונות שנשארו פתוחים "
                "בקופה ועדיין לא ניתן — בדוק שאין מסך פתוח בקופה." %
                (e2, type(e2).__name__, n))
    time.sleep(0.25)
    _t = _lap("menu", _t)

    # 2) פופאפ "בחר שם עובד" — ⚠️ חלון **ללא כותרת**, ולכן לא ניתן לאתר לפי שם.
    #    לוחצים על כפתור העובד לפי הטקסט שלו, בדיוק כמו משתמש. הפופאפ מודאלי:
    #    כל עוד הוא פתוח, הטופס שמאחוריו קיים אבל **חוסם כל לחיצה** (כשל שקט).
    # ⛔ לא לוחצים על כפתורי הפופאפ! הם owner-drawn בלי טקסט, וקליק-לפי-מיקום
    #    פספס ונחת על "חדש" (נפתחה יצירת עובד חדש, 07/08). במקום זה: סוגרים את
    #    הפופאפ ב-ESC — עכשיו מוצאים אותו כחלון-ללא-כותרת ולכן ה-ESC באמת מגיע —
    #    וממלאים שם+מספר עובד ישירות בשדות הטופס (פקדים אמיתיים עם id).
    emp_name = (removal.get("employee_name") or "").strip()
    popup = None
    # ⏱️ הפופאפ מצויר מיד עם פתיחת הטופס. המתנה של 6ש' "ליתר ביטחון" נשרפה
    # במלואה בכל הרצה שבה הוא לא מופיע. 2.5ש' בדגימה צפופה מספיקים בהרבה.
    for _ in range(25):
        popup = _find_popup(app)
        if popup is not None:
            break
        time.sleep(0.1)
    if popup is not None:
        # קודם מנסים לסגור (ESC/X/ביטול) — אז נמלא עובד בשדות הטופס.
        _dismiss_popup(app, popup)
        if _find_popup(app) is not None:
            # לא נסגר → הפופאפ דורש בחירה. לוחצים על הכפתור לפי **רשת חיה**
            # (נקראת מהפופאפ עכשיו, לא מיקומים שנמדדו פעם) + אימות עצמי בהמשך.
            grid = _emp_grid(popup)
            idx = (T.get("emp_index") or {}).get(emp_name)
            if idx is None:
                first = emp_name.split()[0] if emp_name else ""
                for k, v in (T.get("emp_index") or {}).items():
                    if k == first or k.startswith(first) or first.startswith(k):
                        idx = v
                        break
            if idx is None or idx >= len(grid):
                raise RuntimeError(
                    "הפופאפ לא נסגר ואין אינדקס תקין ל'%s' (נמצאו %d כפתורים). "
                    "הגדר tuning.emp_index" % (emp_name, len(grid)))
            _click_ctrl(grid[idx])
            if not _wait_until(lambda: _find_popup(app) is None, 2.0, 0.1):
                raise RuntimeError("פופאפ בחירת העובד עדיין פתוח אחרי לחיצה על אינדקס %d" % idx)
    _t = _lap("emp_popup", _t)

    form = _spec(app, FORM_TITLE, timeout=8)
    if form is None:
        raise RuntimeError("טופס 'הורדה מהמלאי' לא נפתח")
    _t = _lap("form_open", _t)

    # 📸 צילום מצב הטופס בכל כשל — כדי לראות בדיוק איפה נעצר
    def _shot(tag):
        if not screenshot_path:
            return
        try:
            form.capture_as_image().save(screenshot_path.replace(".png", "_%s.png" % tag))
        except Exception:                # noqa: BLE001
            pass

    # בחר סניף → מחסן\מרלוג
    try:
        _select_option(_child(form, F_BRANCH_RADIO))
        time.sleep(0.2)
        combo = _child(form, F_BRANCH_COMBO)
        try:
            combo.select(T["branch_city"])
        except Exception:                # noqa: BLE001
            _click_ctrl(combo)           # פתיחת הרשימה + בחירה בהקלדה
            time.sleep(0.3)
            from pywinauto.keyboard import send_keys
            send_keys(T["branch_city"], with_spaces=True, pause=0.03)
            send_keys("{ENTER}")
    except Exception as e:               # noqa: BLE001
        _shot("branch")
        raise RuntimeError("בחירת סניף נכשלה: %s" % _err(e))

    # תיאור פעולה = ברירת מחדל + סכום (+ הערה)
    amount = removal.get("amount") or 0
    note = (removal.get("note") or "").strip()
    desc = "הורדה מהמלאי"
    if amount:
        a = int(amount) if float(amount).is_integer() else amount
        desc += " - סכום: %s ש\"ח" % a
    if note:
        desc += " · " + note
    try:
        _set_text(_child(form, F_DESC), desc)
    except Exception as e:               # noqa: BLE001
        _shot("desc")
        raise RuntimeError("מילוי תיאור פעולה נכשל: %s" % _err(e))

    # עובד: ממלאים **ישירות** את שני השדות של הטופס (id=3 מספר, id=2 שם) —
    # בלי כפתור "הצב" ובלי קואורדינטות. אלה פקדי TextBox אמיתיים עם id.
    emp_no = str(removal.get("employee_no") or "").strip()
    try:
        # 🔍 אימות עצמי: אם הפופאפ בחר עובד, הטופס כבר מכיל את שמו. אם זה **לא**
        # העובד שביקשנו — עצור ודווח מי כן נבחר, כדי לתקן את האינדקס בלי לנחש.
        cur_name = ""
        try:
            cur_name = (_child(form, F_EMP_NAME).window_text() or "").strip()
        except Exception:                # noqa: BLE001
            pass
        if cur_name and emp_name and emp_name.split()[0] not in cur_name:
            raise RuntimeError("הפופאפ בחר '%s' במקום '%s' — אינדקס שגוי ב-emp_index"
                               % (cur_name, emp_name))
        if not cur_name:                 # הפופאפ נסגר בלי בחירה → ממלאים ידנית
            if emp_no:
                _set_text(_child(form, F_EMP_NO), emp_no)
                time.sleep(0.2)
            if emp_name:
                _set_text(_child(form, F_EMP_NAME), emp_name)
                time.sleep(0.2)
    except RuntimeError:
        _shot("emp")
        raise
    except Exception as e:               # noqa: BLE001
        _shot("emp")
        raise RuntimeError("הזנת עובד נכשלה: %s" % _err(e))

    if screenshot_path:
        try: form.capture_as_image().save(screenshot_path)
        except Exception: pass           # noqa: BLE001

    # סוג פעולה = "עדכון מלאי" — ⚠️ **חובה**, ו**אחרון**: אם נשאר "החזרה לספק"
    # (ברירת המחדל) הקופה עוצרת עם "חסר שדה חובה! ... (שם ספק)". מסמנים בסוף כדי
    # ששום מילוי אחר אחריו לא יאפס אותו (נצפה 07/08: נבחר ואז חזר).
    try:
        opt = _child(form, F_OPTYPE_UPDATE)
        for _ in range(3):               # מנסים עד שהסימון "נתפס" בפועל
            _select_option(opt)
            if _is_selected(opt):
                break
        if not _is_selected(opt):
            _shot("optype")
            raise RuntimeError("'עדכון מלאי' לא נתפס אחרי 3 ניסיונות — "
                               "הטופס נשאר על 'החזרה לספק'")
    except RuntimeError:
        raise
    except Exception as e:               # noqa: BLE001
        _shot("optype")
        raise RuntimeError("סימון 'עדכון מלאי' נכשל: %s" % _err(e))

    _t = _lap("form_fill", _t)

    # 3) התחל פעולה → מסך פריטים
    # ⏱️ ⛔ אין צילום כאן. capture_as_image על הטופס עולה ~5 שניות — שישית מזמן
    # ההרצה — והוא תיעוד בלבד. _shot נשאר בכל נתיבי הכשל, שם הוא באמת שווה משהו.
    # הכפתור owner-drawn (בלי טקסט/id) — מאתרים אותו **דינמית**: השורה התחתונה
    # של הטופס, והכפתור הימני ביותר בה (RTL: ימין = "התחל פעולה", שמאל = "ביטול").
    # ⚠️ "התחל פעולה" owner-drawn: מנסים בסדר עולה של סיכון, ובודקים אחרי כל צעד.
    #    (1) Enter — כפתור ברירת המחדל של הטופס, בלי תלות במיקום בכלל.
    #    (2) לחיצה על הפקד הימני בשורה התחתונה.
    #    (3) הפקד הבא בשורה — רק אם הטופס עדיין פתוח (כלומר לא לחצנו "ביטול").
    #    ⛔ אם הטופס נסגר בלי שנפתח מסך פריטים — לחצנו "ביטול", ומדווחים במפורש.
    # ⏱️ **לחיצה קודם.** המדידה הראתה start[CLICK] בכל הרצה — כלומר ה-Enter
    # מעולם לא פתח את המסך, ורק שרפנו עליו המתנה. Enter נשאר כרשת ביטחון אחרי.
    items_win = None
    how = "CLICK"
    row = _bottom_row(form)              # ממוין מימין לשמאל
    for cand in row[:2]:
        if _spec(app, FORM_TITLE, timeout=1) is None:
            raise RuntimeError("הטופס נסגר בלי לפתוח מסך פריטים — ככל הנראה "
                               "נלחץ 'ביטול' במקום 'התחל פעולה'")
        try:
            _click_ctrl(cand)
        except Exception:                # noqa: BLE001
            continue
        # ⏱️ בלי sleep קבוע לפני הבדיקה — exists() כבר ממתין עד שיופיע
        items_win = _spec(app, ITEM_TITLE, timeout=4)
        if items_win is not None:
            break
        if _read_message_box():          # הקופה התלוננה — לא ננסה כפתור נוסף
            break

    if items_win is None and _spec(app, FORM_TITLE, timeout=1) is not None:
        how = "ENTER"                    # רשת ביטחון: אם מיקום הכפתור השתנה
        try:
            form.set_focus()
            from pywinauto.keyboard import send_keys as _sk
            _sk("{ENTER}")
        except Exception:                # noqa: BLE001
            pass
        items_win = _spec(app, ITEM_TITLE, timeout=3)
    if items_win is None:
        # 🔍 אבחון: הקופה כנראה הציגה הודעה (שדה חסר וכו'). קוראים את הטקסט שלה
        # ומחזירים אותו בשגיאה — במקום לנחש מה הפריע לה.
        msg = _dismiss_message_box()
        _shot("start_failed")
        raise RuntimeError("מסך הזנת הפריטים לא נפתח%s"
                           % (" — הקופה אמרה: %s" % msg if msg else
                              " (לא הוצגה הודעה — ייתכן שהלחיצה פספסה)"))
    _t = _lap("start[%s]" % how, _t)

    # 🛡️ ניקוי הרשימה לפני הזנה — **קריטי לבטיחות.** הקופה משחזרת פעולה שלא
    # הושלמה, ולכן פריטים מריצה קודמת עלולים להישאר ברשימה (נצפה 07/08: SmartTag
    # מבדיקה קודמת הופיע יחד עם הפריט החדש). בלי זה, הורדה חיה הייתה מורידה גם
    # אותם. "מחק הכל" = הכפתור החמישי מימין בשורת הפעולות.
    # לוחצים תמיד — ניקוי רשימה ריקה הוא no-op, ולכן זה זול ובטוח יותר מלנסות
    # לספור שורות (MSFlexGrid לא חושף ספירה אמינה).
    try:
        btns = _item_action_buttons(items_win)
        if len(btns) >= 5:
            _click_ctrl(btns[4])             # מחק הכל
            time.sleep(0.4)
            # ⏱️ timeout קצר: ברשימה ריקה הקופה **לא** שואלת כלום, וההמתנה
            # המלאה (6ש') הייתה נשרפת בכל הרצה על דיאלוג שלא יגיע.
            _answer_smart(timeout=1.5)
            time.sleep(0.3)
            _dismiss_message_box()
    except Exception:                        # noqa: BLE001
        pass
    # ℹ️ הפריט הראשון עולה ~4.9ש' מול ~2.2ש' לשאר — ההקלדה הראשונה אחרי פתיחת
    # המסך נבלעת ומוקלדת שוב. נוסו שלושה תיקונים (07/08) וכולם **החמירו**:
    # האטת הקצב (+1.0), הארכת הקריאה-החוזרת (+0.6), והמתנה ל-CPU פנוי (+0.5 בלי
    # תועלת). ⛔ לא לנסות שוב בלי השערה חדשה — זה 2.5ש' פעם אחת בפעולה.
    _t = _lap("clear_all", _t)

    # 4) הזנת פריטים — לכל פריט: קוד → Enter → **כמות (תמיד)** → "הורד מהמלאי".
    #    ⚠️ הכמות היא id=22; id=21 הוא "מלאי נוכחי" (תצוגה). בלי מילוי כמות
    #    הפריט לא נכנס לרשימה שבתחתית (אסי, 07/08).
    for _idx, it in enumerate(removal.get("items") or []):
        sku = str(it.get("sku") or "").strip()
        serial = str(it.get("serial") or "").strip()
        qty = float(it.get("qty") or 1)
        if not sku and not serial:
            continue
        # ⚠️ בפריט **סידורי** מקלידים את ה**סריאל**, לא את מק"ט המוצר. המסך נקרא
        # "קוד פריט/סריאלי" ומצפה ליחידה הספציפית; הקלדת המק"ט גרמה לקופה לא לזהות
        # ולהציע "פריט חדש" (07/08). המק"ט נשמר לאימות המלאי בלבד.
        code = serial or sku
        items_win.set_focus()
        from pywinauto.keyboard import send_keys as _sk2
        code_ctrl = _child(items_win, I_CODE)
        # ⏱️ נוסה (07/08) להאיט את הפריט הראשון — זה רק **הוסיף** שנייה (4.9→5.9).
        # כלומר העיכוב אינו בקצב ההקלדה אלא בקופה עצמה מיד אחרי פתיחת המסך.
        _type_code_verified(code_ctrl, code)
        _sk2("{ENTER}")
        # ⏱️ במקום להמתין 1.2ש' קבועות: מחכים לאות שהקופה סיימה לעבד — היא
        # מרוקנת את שדה הקוד לפריט הבא. בפועל זה ~0.3ש', לא 1.2.
        _wait_until(lambda: (code_ctrl.window_text() or "").strip() != code, 2.0)
        _guard_new_item(app, code)        # ⛔ מק"ט לא מוכר → ביטול ועצירה

        # ⚠️ שני מסלולים שונים (אסי, 07/08):
        #  • פריט **סידורי** (הוקלד סריאל) → הקופה מוסיפה לרשימה **אוטומטית**
        #    אחרי Enter. אין כמות ואין ללחוץ "הורד מהמלאי".
        #  • פריט **לא-סידורי** → חייבים למלא כמות ואז "הורד מהמלאי", אחרת
        #    הפריט לא נכנס לרשימה שבתחתית.
        if serial:
            time.sleep(0.25)
            _t = _lap("item_%s" % code, _t)
            continue

        qty_s = str(int(qty) if float(qty).is_integer() else qty)
        try:
            _type_text(_child(items_win, I_QTY), qty_s)   # הקלדה אמיתית — ראה _type_text
        except Exception as e:           # noqa: BLE001
            _shot("qty")
            raise RuntimeError("מילוי כמות נכשל עבור %s: %s" % (sku, _err(e)))
        time.sleep(0.3)
        _dismiss_message_box()           # אם בכל זאת התלוננה — סוגרים ולא נתקעים

        btns = _item_action_buttons(items_win)   # [0]=סיים פעולה, [1]=הורד מהמלאי
        if len(btns) >= 2:
            _click_ctrl(btns[1])
        else:
            _click_rect(items_win, T["add_rect"])
        time.sleep(0.45)
        _t = _lap("item_%s" % code, _t)

    if screenshot_path:
        try: items_win.capture_as_image().save(screenshot_path.replace(".png", "_items.png"))
        except Exception: pass           # noqa: BLE001

    # 5) סיום
    if dry_run:
        # יציאה בלי לשמור — ומוודאים שהמסך אכן נסגר, אחרת הוא יחסום את הריצה הבאה
        ok = _exit_item_screen(app, items_win)
        if not ok:
            _force_close_extra(app, pos)
            ok = _spec(app, ITEM_TITLE, timeout=0.5) is None
        _lap("exit_dry", _t)
        _log("TIMING total=%.1fs | %s" % (time.time() - _run0, " ".join(_TIMING)))
        if not ok:
            raise RuntimeError("מסך הפריטים לא נסגר אחרי היציאה — הוא יחסום את "
                               "ההרצה הבאה. סגור אותו בקופה ('יציאה' → 'כן').")
        return ""

    btns = _item_action_buttons(items_win)       # [0] = "סיים פעולה"
    if btns:
        _click_ctrl(btns[0])
    elif T.get("finish_rect"):
        _click_rect(items_win, T["finish_rect"])
    else:
        raise RuntimeError("לא אותר כפתור 'סיים פעולה'")
    # ⚠️ לא "כן לכל דבר": אחרי השמירה הקופה שואלת גם על **הדפסה**, ושם התשובה
    # היא לא, ואז מגיעות שתי הודעות OK. השרשרת (07/08, ההרצה החיה הראשונה):
    #   "האם להדפיס בהדפסה רחבה?" → לא
    #   "בעיה בהגדרת מדפסת קופה"  → OK
    #   "פעולה עודכנה בהצלחה. מספר פעולה: NNNNN" → OK  ← ממנה נשלף מס' התעודה
    answered = _answer_dialog_chain()
    # סבב שני: חלון שהופיע באיחור אחרי שהשרשרת כבר סיימה
    answered += _answer_dialog_chain(first_wait=2.0, next_wait=4.0)
    doc_no = _doc_no_from(answered)
    _dismiss_message_box()
    # אחרי שמירה המסך אמור להיסגר לבד; אם נשאר — סוגרים, אחרת הוא יחסום את הבא
    if _spec(app, ITEM_TITLE, timeout=0.5) is not None:
        _exit_item_screen(app, items_win)
    _lap("finish", _t)
    _log("TIMING total=%.1fs | %s" % (time.time() - _run0, " ".join(_TIMING)))
    if doc_no:
        _log("  📄 תעודה בקופה: %s" % doc_no)
    return doc_no
