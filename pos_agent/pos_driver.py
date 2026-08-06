"""
pos_driver.py — הנעת קופת NewOrder/Morning (VB6) דרך pywinauto (backend win32).
נקרא ע"י agent.py. כאן יושבת כל האינטראקציה עם חלונות הקופה.

⚠️ מפת ה-UI המדויקת (מזהי שדות/כפתורים במסכי "הורדה מהמלאי") תמולא אחרי
   הרצת inspect_dialog.py על שלושת המסכים (עובד / טופס / הזנת פריטים).
   עד שהיא מוגדרת — apply_removal מסרב לרוץ עם הודעה ברורה, כך שהסוכן לא
   עושה חצי-פעולה. הלוגיקה סביב (poll/claim/verify/report) כבר עובדת מלאה.
"""
import time

from pywinauto import Application, Desktop
from pywinauto.keyboard import send_keys

POS_TITLE_RE = ".*אורדר.*"

# ── מפת UI — יתמלא אחרי inspect_dialog.py ─────────────────────────────
# מזהי הפריטים שנצטרך (control_id / class / קואורדינטה):
UI_MAP_READY = False        # → True אחרי שממלאים את המפה למטה
MENU_PATH_REMOVAL = "מלאי->הורדה מהמלאי/החזרה לספק"   # מהמיפוי (index 3)
# EMPLOYEE_BUTTONS   = {...}   # שם→איך ללחוץ (מ-pos_emp.txt)
# FORM_DESC_ID       = ...     # שדה "תיאור פעולה" (מ-pos_form.txt)
# FORM_BRANCH_...    = ...     # בחירת סניף = מחסן\מרלוג
# FORM_START_BTN     = ...     # "התחל פעולה"
# ITEM_SKU_ID / ITEM_QTY_ID / ITEM_ADD / DOC_SAVE   (מ-pos_items.txt)


def connect():
    """מתחבר לחלון הקופה. הקופה חייבת להיות פתוחה."""
    app = Application(backend="win32").connect(title_re=POS_TITLE_RE, timeout=15)
    dlg = app.window(title_re=POS_TITLE_RE)
    dlg.set_focus()
    return app, dlg


def _find_dialog(app, title_part, timeout=8):
    """מחזיר חלון-דיאלוג פתוח שכותרתו מכילה title_part (עובד/פעולה וכו')."""
    end = time.time() + timeout
    while time.time() < end:
        for w in Desktop(backend="win32").windows():
            try:
                if title_part in (w.window_text() or ""):
                    return w
            except Exception:                # noqa: BLE001
                pass
        time.sleep(0.3)
    return None


def apply_removal(removal, dry_run=True, screenshot_path=None):
    """מבצע פעולת הורדה אחת בקופה. dry_run=True → ממלא הכל אבל **מבטל** במקום לשמור.
    מחזיר מספר תעודה (או '' ב-dry-run). זורק חריגה אם משהו לא נמצא/נכשל.

    ⚠️ ממתין להשלמת UI_MAP מ-inspect_dialog.py. עד אז — חריגה ברורה."""
    if not UI_MAP_READY:
        raise RuntimeError(
            "מפת ה-UI של מסך ההורדה עדיין לא הוגדרה. הרץ inspect_dialog.py על "
            "שלושת המסכים (emp/form/items) ושלח את הפלט כדי שאשלים את המפה.")

    app, dlg = connect()

    # 1) פתיחת "הורדה מהמלאי" מהתפריט
    dlg.menu_select(MENU_PATH_REMOVAL)

    # 2) בחירת עובד בפופאפ "בחר שם עובד"   ← יושלם מ-pos_emp.txt
    #    emp = removal["employee_name"] / removal["employee_no"]
    # 3) מילוי הטופס: סניף=מחסן\מרלוג, תיאור=note + "סכום: {amount}"  ← pos_form.txt
    # 4) "התחל פעולה"
    # 5) הזנת פריטים: לכל item — מק"ט + כמות + הוספה   ← pos_items.txt
    # 6) dry_run → לחיצה על "ביטול"; אחרת → שמירה + קליטת מס' תעודה
    raise RuntimeError("UI steps not implemented yet")   # יוחלף במימוש אחרי המיפוי


def screenshot(dlg, path):
    try:
        dlg.capture_as_image().save(path)
    except Exception:                        # noqa: BLE001
        pass
