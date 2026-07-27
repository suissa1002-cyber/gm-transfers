"""
Transfers app — קונפיגורציה.
טוען מאותו .env של שאר הסוכנים (2 רמות מעלה), ומאפשר override ע"י משתני סביבה של Render.
"""

import os
try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # סביבת בדיקה/CI בלי python-dotenv — env מוזרק ישירות
    def load_dotenv(*a, **k):
        return False

# .env משותף ברמת השורש (agents/transfers/ -> ../../.env)
_env_path = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
load_dotenv(_env_path)

# ── NewOrder POS API (קריאה בלבד) ──
NEWORDER_BASE_URL = os.getenv("NEWORDER_BASE_URL", "https://neworderapi.azurewebsites.net")
NEWORDER_API_TOKEN = os.getenv("NEWORDER_API_TOKEN")
NEWORDER_STORE_GUID = os.getenv("NEWORDER_STORE_GUID")

# ── סניפים (זהה ל-ron/config.py) ──
BRANCHES = {
    5: "אתר",
    1: "גן העיר",
    2: "סטאר",
    3: "מחסן\\מרלוג",
    4: "עד הלום",
}
TRANSFER_OP_TYPE = 5  # operationType של "העברה בין סניפים"

# ── DB ──
# בפיתוח: SQLite מקומי. ב-Render: DATABASE_URL (Postgres) דרך משתנה סביבה.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SQLITE_PATH = os.getenv("TRANSFERS_DB_PATH",
                        os.path.join(os.path.dirname(__file__), "transfers.db"))

# ── Poller ──
# ⚠️ רצפה קשיחה — לא רק ברירת מחדל. השיעור מ-21/07: שינינו את ברירת המחדל 30→60 וזה
# לא עשה כלום, כי ב-Render מוגדר `POLL_INTERVAL_SEC=30` מפורשות והוא גובר. NewOrder
# עדיין התלוננו (רפי, 23/07). ⇒ הערך האפקטיבי לא יורד מהרצפה גם אם ה-env אומר אחרת;
# להאיץ = להוריד את הרצפה כאן, בקוד, במודע.
_POLL_FLOOR_SEC = 75
POLL_INTERVAL_SEC = max(_POLL_FLOOR_SEC, int(os.getenv("POLL_INTERVAL_SEC", "75")))

# 🕗 קצב מותאם-שעות (26/07): העברות נעשות רק כשהסניפים פתוחים — בסטאר לעיתים תוך כדי
# מכירה, ואז 3 דק' המתנה זה יותר מדי (אסי). לכן מהיר בשעות עבודה ואיטי בלילה: זה נותן
# ~75ש בשעות שבהן זה משנה, ועדיין ~80% פחות קריאות מהמצב שהציף את NewOrder — במקום
# לשרוף את אותו קצב 24/7 על שעות שבהן לא זזה שום העברה.
POLL_IDLE_INTERVAL_SEC = max(POLL_INTERVAL_SEC,
                             int(os.getenv("POLL_IDLE_INTERVAL_SEC", "900")))
POLL_ACTIVE_FROM_HOUR = int(os.getenv("POLL_ACTIVE_FROM_HOUR", "7"))    # כולל
POLL_ACTIVE_TO_HOUR = int(os.getenv("POLL_ACTIVE_TO_HOUR", "23"))       # לא כולל


def poll_interval_now() -> int:
    """הקצב האפקטיבי לרגע זה (שעון ישראל): מהיר בשעות פעילות, איטי בלילה.
    ⚠️ בספק — מחזיר את הקצב המהיר (עדיף קריאה מיותרת מהעברה שנתקעת)."""
    try:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _ZI
        h = _dt.now(_ZI(TZ)).hour
    except Exception:  # noqa: BLE001
        return POLL_INTERVAL_SEC
    return (POLL_INTERVAL_SEC if POLL_ACTIVE_FROM_HOUR <= h < POLL_ACTIVE_TO_HOUR
            else POLL_IDLE_INTERVAL_SEC)
# כמה ימים אחורה למשוך בכל סבב (חלון בטיחות; העברות פתוחות נשארות ב-DB ממילא)
POLL_LOOKBACK_DAYS = int(os.getenv("POLL_LOOKBACK_DAYS", "3"))

# ── Telegram (התראות/הסלמה) ──
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# צ'אט הסלמה למנהלים (ברירת מחדל: ה-chat של אסי, כמו monday_tasks)
TELEGRAM_MANAGERS_CHAT_ID = os.getenv("TELEGRAM_MANAGERS_CHAT_ID",
                                      os.getenv("TELEGRAM_TASKS_CHAT_ID", "448181407"))
# ── ConnectOp (ChatRace) — טוקן הדשבורד לקריאת שיחות וואטסאפ ──
# cookie מהתחברות Google ל-newapp.connectop.co.il; פג כל ~10-22 ימים.
# token_watch.py מנטר תפוגה ומתריע בטלגרם. מקומית מגיע מה-.env המשותף; ב-Render — env var.
CHATRACE_DASHBOARD_TOKEN = os.getenv("CHATRACE_DASHBOARD_TOKEN", "")
CHATRACE_ACCOUNT_ID = os.getenv("CHATRACE_DASHBOARD_ACCOUNT_ID", "1428408")

# מיפוי סניף→chat_id של קבוצת/נציג הסניף (JSON ב-env, אופציונלי). למשל: {"2": "-100..."}
import json as _json
_branch_chats = os.getenv("TELEGRAM_BRANCH_CHATS", "").strip()
TELEGRAM_BRANCH_CHATS = _json.loads(_branch_chats) if _branch_chats else {}

# ── אזמני SLA לקליטה (שעות) ──
RECEIVE_REMINDER_HOURS = int(os.getenv("RECEIVE_REMINDER_HOURS", "3"))    # תזכורת ראשונה
RECEIVE_ESCALATE_HOURS = int(os.getenv("RECEIVE_ESCALATE_HOURS", "24"))   # הסלמה למנהלים

# ── הגנת קונסולת הניהול (סניף "אתר") ──
# סיסמה לגישה ל-/api/admin ולבחירת סניף "אתר". אם ריק — הניהול פתוח (פיתוח בלבד).
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
ADMIN_BRANCH_ID = 5  # "אתר" = קונסולת הניהול

# ── כללי ──
TZ = os.getenv("TZ", "Asia/Jerusalem")
APP_TITLE = "GreenOS — Green Mobile"
# כתובת ציבורית של האפליקציה (ל-deep links בהתראות). ב-Render: ה-URL של השירות.
APP_BASE_URL = os.getenv("APP_BASE_URL", "").rstrip("/")

# דוח יומי
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", "9"))
DIGEST_DAYS = os.getenv("DIGEST_DAYS", "sun,mon,tue,wed,thu")


def branch_name(branch_id) -> str:
    try:
        return BRANCHES.get(int(branch_id), str(branch_id))
    except (TypeError, ValueError):
        return str(branch_id)
