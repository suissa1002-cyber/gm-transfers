"""
pos_driver.py — הנעת קופת NewOrder/Morning (VB6) דרך pywinauto (backend win32).
נקרא ע"י agent.py. ממופה מ-inspect_dialog.py (27/07/2026), עמדה 5, גרסה 9.10.85.

⚙️ כיול חי: כל הפרמטרים הרגישים (נתיב תפריט, קואורדינטות כפתורים owner-drawn)
   נטענים מ-GreenOS (agent-config → tuning) עם ברירות-מחדל כאן. כך מכיילים
   מהשרת בלי להוריד את הקוד מחדש למכונה.
"""
import time

from pywinauto import Application, Desktop, mouse

DRIVER_VERSION = "2026-08-07.9"          # מודפס ע"י הסוכן — לוודא איזו גרסה רצה
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
I_QTY           = 21    # TextBox כמות (מסך פריטים)


def connect():
    app = Application(backend="win32").connect(title_re=POS_TITLE_RE, timeout=15)
    return app, app.window(title_re=POS_TITLE_RE)


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
    """סוגר תיבת הודעה של Windows (#32770) בלחיצה על OK — כדי שהטופס לא יישאר
    תקוע מאחוריה ויחסום את הריצה הבאה. מחזיר את הטקסט שהוצג."""
    msg = _read_message_box()
    try:
        for w in Desktop(backend="win32").windows():
            try:
                if w.class_name() == "#32770" and w.is_visible():
                    try:
                        w.set_focus()
                        w.type_keys("{ENTER}")
                    except Exception:    # noqa: BLE001
                        pass
                    time.sleep(0.4)
            except Exception:            # noqa: BLE001
                continue
    except Exception:                    # noqa: BLE001
        pass
    return msg


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
    for attempt in range(3):
        try:
            popup.set_focus()
            popup.type_keys("{ESC}")
        except Exception:                # noqa: BLE001
            pass
        time.sleep(0.6)
        if _find_popup(app) is None:
            return
        try:
            popup.close()                # WM_CLOSE — שקול ללחיצה על ה-X
        except Exception:                # noqa: BLE001
            pass
        time.sleep(0.6)
        if _find_popup(app) is None:
            return
        if attempt == 0:                 # רק אחרי ש-ESC/X נכשלו
            row = _bottom_row(popup)
            if len(row) >= 2:            # [0]=חדש (אסור!), [1]=ביטול
                try:
                    _click_ctrl(row[1])
                except Exception:        # noqa: BLE001
                    pass
                time.sleep(0.8)
                if _find_popup(app) is None:
                    return


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


def _cleanup(app, T):
    """סוגר שאריות דיאלוגים מהרצה קודמת שנכשלה (טופס/מסך פריטים/פופאפ עובד),
    כדי שהתפריט יהיה פנוי. ריצה שנכשלה משאירה חלון פתוח שחוסם את הבא."""
    _dismiss_message_box()          # תיבת הודעה תקועה חוסמת הכל
    for _ in range(3):
        closed = False
        for title in (ITEM_TITLE, FORM_TITLE, "בחר שם עובד"):
            w = _spec(app, title, timeout=1)
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
            if title == FORM_TITLE and _spec(app, FORM_TITLE, timeout=1):
                _click_rect(w, T["cancel_rect"])
                time.sleep(0.4)
        if not closed:
            break


def apply_removal(removal, dry_run=True, screenshot_path=None, tuning=None):
    """מבצע פעולת הורדה אחת. dry_run=True → ממלא אבל לא שומר (יוצא ב-ESC).
    tuning: dict מהשרת שדורס את DEFAULT_TUNING. מחזיר מס' תעודה ('' ב-dry)."""
    T = dict(DEFAULT_TUNING)
    if tuning:
        T.update({k: v for k, v in tuning.items() if v is not None})

    app, pos = connect()
    pos.set_focus()

    # 0) ניקוי שאריות מהרצה קודמת (חלון פתוח חוסם את התפריט)
    _cleanup(app, T)
    pos.set_focus()

    # 1) תפריט → טופס הורדה
    try:
        pos.menu_select(T["menu_path"])
    except Exception as e:               # noqa: BLE001
        raise RuntimeError("פתיחת תפריט מלאי נכשלה: %s (%s)" % (e, type(e).__name__))
    time.sleep(1.0)

    # 2) פופאפ "בחר שם עובד" — ⚠️ חלון **ללא כותרת**, ולכן לא ניתן לאתר לפי שם.
    #    לוחצים על כפתור העובד לפי הטקסט שלו, בדיוק כמו משתמש. הפופאפ מודאלי:
    #    כל עוד הוא פתוח, הטופס שמאחוריו קיים אבל **חוסם כל לחיצה** (כשל שקט).
    # ⛔ לא לוחצים על כפתורי הפופאפ! הם owner-drawn בלי טקסט, וקליק-לפי-מיקום
    #    פספס ונחת על "חדש" (נפתחה יצירת עובד חדש, 07/08). במקום זה: סוגרים את
    #    הפופאפ ב-ESC — עכשיו מוצאים אותו כחלון-ללא-כותרת ולכן ה-ESC באמת מגיע —
    #    וממלאים שם+מספר עובד ישירות בשדות הטופס (פקדים אמיתיים עם id).
    emp_name = (removal.get("employee_name") or "").strip()
    popup = None
    for _ in range(12):                  # עד ~6 שניות עד שהפופאפ מצויר
        popup = _find_popup(app)
        if popup is not None:
            break
        time.sleep(0.5)
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
            time.sleep(1.0)
            if _find_popup(app) is not None:
                raise RuntimeError("פופאפ בחירת העובד עדיין פתוח אחרי לחיצה על אינדקס %d" % idx)

    form = _spec(app, FORM_TITLE, timeout=8)
    if form is None:
        raise RuntimeError("טופס 'הורדה מהמלאי' לא נפתח")

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

    # 3) התחל פעולה → מסך פריטים
    _shot("filled")                      # צילום הטופס המלא לפני ההמשך
    # הכפתור owner-drawn (בלי טקסט/id) — מאתרים אותו **דינמית**: השורה התחתונה
    # של הטופס, והכפתור הימני ביותר בה (RTL: ימין = "התחל פעולה", שמאל = "ביטול").
    # ⚠️ "התחל פעולה" owner-drawn: מנסים בסדר עולה של סיכון, ובודקים אחרי כל צעד.
    #    (1) Enter — כפתור ברירת המחדל של הטופס, בלי תלות במיקום בכלל.
    #    (2) לחיצה על הפקד הימני בשורה התחתונה.
    #    (3) הפקד הבא בשורה — רק אם הטופס עדיין פתוח (כלומר לא לחצנו "ביטול").
    #    ⛔ אם הטופס נסגר בלי שנפתח מסך פריטים — לחצנו "ביטול", ומדווחים במפורש.
    items_win = None
    try:
        form.set_focus()
        from pywinauto.keyboard import send_keys as _sk
        _sk("{ENTER}")
    except Exception:                    # noqa: BLE001
        pass
    time.sleep(1.5)
    items_win = _spec(app, ITEM_TITLE, timeout=3)

    if items_win is None:
        row = _bottom_row(form)          # ממוין מימין לשמאל
        for cand in row[:2]:
            if _spec(app, FORM_TITLE, timeout=1) is None:
                raise RuntimeError("הטופס נסגר בלי לפתוח מסך פריטים — ככל הנראה "
                                   "נלחץ 'ביטול' במקום 'התחל פעולה'")
            try:
                _click_ctrl(cand)
            except Exception:            # noqa: BLE001
                continue
            time.sleep(1.5)
            items_win = _spec(app, ITEM_TITLE, timeout=4)
            if items_win is not None:
                break
            if _read_message_box():      # הקופה התלוננה — לא ננסה כפתור נוסף
                break
    if items_win is None:
        # 🔍 אבחון: הקופה כנראה הציגה הודעה (שדה חסר וכו'). קוראים את הטקסט שלה
        # ומחזירים אותו בשגיאה — במקום לנחש מה הפריע לה.
        msg = _dismiss_message_box()
        _shot("start_failed")
        raise RuntimeError("מסך הזנת הפריטים לא נפתח%s"
                           % (" — הקופה אמרה: %s" % msg if msg else
                              " (לא הוצגה הודעה — ייתכן שהלחיצה פספסה)"))

    # 4) הזנת פריטים
    for it in (removal.get("items") or []):
        sku = str(it.get("sku") or "").strip()
        qty = float(it.get("qty") or 1)
        if not sku:
            continue
        items_win.set_focus()
        items_win.type_keys("{DELETE 20}%s{ENTER}" % sku, with_spaces=True)
        time.sleep(0.8)
        if qty and qty != 1:
            try: _child(items_win, I_QTY).set_edit_text(str(int(qty) if float(qty).is_integer() else qty))
            except Exception: pass       # noqa: BLE001
        _click_rect(items_win, T["add_rect"])
        time.sleep(0.6)

    if screenshot_path:
        try: items_win.capture_as_image().save(screenshot_path.replace(".png", "_items.png"))
        except Exception: pass           # noqa: BLE001

    # 5) סיום
    if dry_run:
        try: items_win.type_keys("{ESC}")
        except Exception: pass           # noqa: BLE001
        return ""

    if not T.get("finish_rect"):
        raise RuntimeError("קואורדינטת 'סיים פעולה' טרם כוילה")
    _click_rect(items_win, T["finish_rect"])
    time.sleep(1.5)
    return ""
