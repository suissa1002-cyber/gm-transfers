"""
pos_driver.py — הנעת קופת NewOrder/Morning (VB6) דרך pywinauto (backend win32).
נקרא ע"י agent.py. ממופה מ-inspect_dialog.py (27/07/2026), עמדה 5, גרסה 9.10.85.

⚙️ כיול חי: כל הפרמטרים הרגישים (נתיב תפריט, קואורדינטות כפתורים owner-drawn)
   נטענים מ-GreenOS (agent-config → tuning) עם ברירות-מחדל כאן. כך מכיילים
   מהשרת בלי להוריד את הקוד מחדש למכונה.
"""
import time

from pywinauto import Application, Desktop, mouse

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
}

# control-ids קבועים (מהמיפוי, לא משתנים):
F_OPTYPE_UPDATE = 5     # OptionButton "עדכון מלאי" (חובה)
F_BRANCH_RADIO  = 15    # OptionButton "בחר סניף:"
F_BRANCH_COMBO  = 14    # ComboBox הסניפים
F_DESC          = 8     # TextBox תיאור פעולה (הסכום)
F_EMP_NO        = 3     # TextBox מס' עובד
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
    return win.child_window(control_id=cid)


def _cleanup(app, T):
    """סוגר שאריות דיאלוגים מהרצה קודמת שנכשלה (טופס/מסך פריטים/פופאפ עובד),
    כדי שהתפריט יהיה פנוי. ריצה שנכשלה משאירה חלון פתוח שחוסם את הבא."""
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

    # 2) פופאפ "בחר שם עובד" — סוגרים (נזין בטופס)
    emp_popup = _spec(app, "בחר שם עובד", timeout=3)
    if emp_popup is not None:
        try: emp_popup.type_keys("{ESC}")
        except Exception: pass           # noqa: BLE001
        time.sleep(0.5)

    form = _spec(app, FORM_TITLE, timeout=8)
    if form is None:
        raise RuntimeError("טופס 'הורדה מהמלאי' לא נפתח")

    # סוג פעולה = עדכון מלאי (חובה)
    try:
        _child(form, F_OPTYPE_UPDATE).click()
    except Exception as e:               # noqa: BLE001
        raise RuntimeError("סימון 'עדכון מלאי' נכשל: %s" % e)

    # בחר סניף → מחסן\מרלוג
    try:
        _child(form, F_BRANCH_RADIO).click()
        combo = _child(form, F_BRANCH_COMBO)
        try: combo.select(T["branch_city"])
        except Exception: combo.type_keys(T["branch_city"], with_spaces=True)  # noqa: BLE001
    except Exception as e:               # noqa: BLE001
        raise RuntimeError("בחירת סניף נכשלה: %s" % e)

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
        _child(form, F_DESC).set_edit_text(desc)
    except Exception as e:               # noqa: BLE001
        raise RuntimeError("מילוי תיאור פעולה נכשל: %s" % e)

    # עובד: מס' + "הצב"
    emp_no = str(removal.get("employee_no") or "").strip()
    if emp_no:
        try:
            _child(form, F_EMP_NO).set_edit_text(emp_no)
            _click_rect(form, T["emp_set_rect"])
            time.sleep(0.4)
        except Exception as e:           # noqa: BLE001
            raise RuntimeError("הזנת עובד נכשלה: %s" % e)

    if screenshot_path:
        try: form.capture_as_image().save(screenshot_path)
        except Exception: pass           # noqa: BLE001

    # 3) התחל פעולה → מסך פריטים
    _click_rect(form, T["start_rect"])
    time.sleep(1.2)
    items_win = _spec(app, ITEM_TITLE, timeout=8)
    if items_win is None:
        raise RuntimeError("מסך הזנת הפריטים לא נפתח")

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
