"""
pos_driver.py — הנעת קופת NewOrder/Morning (VB6) דרך pywinauto (backend win32).
נקרא ע"י agent.py. ממופה מ-inspect_dialog.py (27/07/2026) על עמדה 5, גרסה 9.10.85.

זרימת "הורדה מהמלאי":
  קופה → תפריט מלאי → "הורדה מהמלאי/החזרה לספק"
    → (פופאפ "בחר שם עובד" אם מופיע) → טופס "הורדה מהמלאי"
      · סוג פעולה = "עדכון מלאי" (OptionButton id=5) — חובה
      · בחר סניף  = OptionButton id=15 + ComboBox id=14 → "מחסן\\מרלוג"
      · תיאור פעולה = TextBox id=8 (כאן נכנס הסכום)
      · מס' עובד = TextBox id=3 + כפתור "הצב"
      · "התחל פעולה" (כפתור ימני-תחתון)
    → מסך "הורדה מהמלאי - פעולה חדשה":
      · שדה קוד/סריאלי (ממוקד) → הקלד מק"ט + Enter
      · כמות = TextBox id=21 ; הערה = TextBox id=14
      · "הורד מהמלאי" מוסיף שורה ; "סיים פעולה" שומר תעודה

⚠️ כפתורים owner-drawn (id=0) — מזוהים לפי מלבן; קואורדינטות מכוילות ב-dry-run.
"""
import time

from pywinauto import Application, Desktop

POS_TITLE_RE = ".*אורדר.*"
FORM_TITLE = "הורדה מהמלאי"
ITEM_TITLE = "הורדה מהמלאי - פעולה חדשה"
UI_MAP_READY = True                      # המיפוי הושלם; דורש כיול dry-run לקואורדינטות

# ── מפת פקדים (מ-inspect_dialog) ─────────────────────────────────────
# טופס "הורדה מהמלאי":
F_OPTYPE_UPDATE = 5     # OptionButton "עדכון מלאי"  (חובה — הורדה מהמלאי)
F_BRANCH_RADIO  = 15    # OptionButton "בחר סניף:"
F_BRANCH_COMBO  = 14    # ComboBox (ערכים: אתר/גן העיר/סטאר/מחסן\מרלוג/עד הלום)
F_DESC          = 8     # TextBox "תיאור פעולה" (כאן הסכום)
F_EMP_NO        = 3     # TextBox מס' עובד
F_EMP_SET_RECT  = (35, 321, 96, 354)     # כפתור "הצב" (owner-drawn)
F_START_RECT    = (491, 417, 596, 458)   # "התחל פעולה" (ימני-תחתון)
F_CANCEL_RECT   = (363, 417, 468, 458)   # "ביטול"
BRANCH_CITY     = "מחסן\\מרלוג"

# מסך פריטים "הורדה מהמלאי - פעולה חדשה":
I_QTY           = 21    # TextBox כמות
I_NOTE          = 14    # TextBox הערה
# כפתורים owner-drawn — מלבנים מהמיפוי, לכיול ב-dry-run:
I_ADD_RECT      = (623, 328, 904, 353)   # ⚠️ אזור "הורד מהמלאי" — לכיול
I_FINISH_RECT   = None                   # ⚠️ "סיים פעולה" — יימדד בכיול dry-run
I_EXIT_RECT     = None                   # ⚠️ "יציאה" — יימדד בכיול dry-run


def connect():
    app = Application(backend="win32").connect(title_re=POS_TITLE_RE, timeout=15)
    dlg = app.window(title_re=POS_TITLE_RE)
    return app, dlg


def _win(title, timeout=8):
    """מחזיר חלון פתוח שכותרתו == title (מדויק). None אם לא נמצא."""
    end = time.time() + timeout
    while time.time() < end:
        for w in Desktop(backend="win32").windows():
            try:
                if (w.window_text() or "") == title:
                    return w
            except Exception:            # noqa: BLE001
                pass
        time.sleep(0.3)
    return None


def _click_rect(win, rect):
    """לוחץ במרכז מלבן (קואורדינטות מסך מהמיפוי). win לצורך פוקוס."""
    from pywinauto import mouse
    try:
        win.set_focus()
    except Exception:                    # noqa: BLE001
        pass
    cx = (rect[0] + rect[2]) // 2
    cy = (rect[1] + rect[3]) // 2
    mouse.click(coords=(cx, cy))


def _child(win, ctrl_id):
    return win.child_window(control_id=ctrl_id)


def apply_removal(removal, dry_run=True, screenshot_path=None):
    """מבצע פעולת הורדה אחת. dry_run=True → ממלא הכל אבל **לא שומר** (יוצא בלי לשמור).
    מחזיר מספר תעודה ('' ב-dry-run). זורק חריגה על כשל."""
    if not UI_MAP_READY:
        raise RuntimeError("UI map not configured")

    app, pos = connect()
    pos.set_focus()

    # 1) פתיחת "הורדה מהמלאי" מהתפריט
    try:
        pos.menu_select("מלאי->הורדה מהמלאי/החזרה לספק")
    except Exception as e:               # noqa: BLE001
        raise RuntimeError("פתיחת תפריט מלאי נכשלה: %s" % e)
    time.sleep(1.0)

    # 2) פופאפ "בחר שם עובד" — אם מופיע, סוגרים (נזין עובד בטופס)
    emp_popup = _win("בחר שם עובד", timeout=3)
    if emp_popup is not None:
        try:
            emp_popup.type_keys("{ESC}")
        except Exception:                # noqa: BLE001
            pass
        time.sleep(0.5)

    # 3) הטופס
    form = _win(FORM_TITLE, timeout=8)
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
        try:
            combo.select(BRANCH_CITY)
        except Exception:                # noqa: BLE001
            combo.type_keys(BRANCH_CITY, with_spaces=True)
    except Exception as e:               # noqa: BLE001
        raise RuntimeError("בחירת סניף נכשלה: %s" % e)

    # תיאור פעולה = ברירת מחדל + סכום (+ הערה)
    amount = removal.get("amount") or 0
    note = (removal.get("note") or "").strip()
    desc = "הורדה מהמלאי"
    if amount:
        desc += " - סכום: %s ש\"ח" % (int(amount) if float(amount).is_integer() else amount)
    if note:
        desc += " · " + note
    try:
        d = _child(form, F_DESC)
        d.set_edit_text(desc)
    except Exception as e:               # noqa: BLE001
        raise RuntimeError("מילוי תיאור פעולה נכשל: %s" % e)

    # עובד: מס' עובד + "הצב"
    emp_no = str(removal.get("employee_no") or "").strip()
    if emp_no:
        try:
            _child(form, F_EMP_NO).set_edit_text(emp_no)
            _click_rect(form, F_EMP_SET_RECT)
            time.sleep(0.4)
        except Exception as e:           # noqa: BLE001
            raise RuntimeError("הזנת עובד נכשלה: %s" % e)

    if screenshot_path:
        try: form.capture_as_image().save(screenshot_path)
        except Exception: pass           # noqa: BLE001

    # 4) התחל פעולה → מסך פריטים
    _click_rect(form, F_START_RECT)
    time.sleep(1.2)
    items_win = _win(ITEM_TITLE, timeout=8)
    if items_win is None:
        raise RuntimeError("מסך הזנת הפריטים לא נפתח")

    # 5) הזנת פריטים — שדה הקוד ממוקד; מקלידים מק"ט + Enter לכל פריט
    for it in (removal.get("items") or []):
        sku = str(it.get("sku") or "").strip()
        qty = float(it.get("qty") or 1)
        if not sku:
            continue
        items_win.set_focus()
        items_win.type_keys("{DELETE 20}%s{ENTER}" % sku, with_spaces=True)
        time.sleep(0.8)
        if qty and qty != 1:
            try:
                _child(items_win, I_QTY).set_edit_text(str(int(qty) if float(qty).is_integer() else qty))
            except Exception:            # noqa: BLE001
                pass
        _click_rect(items_win, I_ADD_RECT)      # "הורד מהמלאי" — מוסיף שורה (כיול dry-run)
        time.sleep(0.6)

    if screenshot_path:
        try: items_win.capture_as_image().save(screenshot_path.replace(".png", "_items.png"))
        except Exception: pass           # noqa: BLE001

    # 6) סיום
    if dry_run:
        # לא שומרים — יוצאים. ESC/יציאה סוגר בלי לשמור תעודה.
        try:
            items_win.type_keys("{ESC}")
        except Exception:                # noqa: BLE001
            pass
        return ""

    if I_FINISH_RECT is None:
        raise RuntimeError("קואורדינטת 'סיים פעולה' טרם כוילה — הרץ dry-run קודם")
    _click_rect(items_win, I_FINISH_RECT)        # "סיים פעולה" — שומר
    time.sleep(1.5)
    # TODO: קליטת מספר התעודה מדיאלוג האישור (יימופה בכיול)
    return ""
