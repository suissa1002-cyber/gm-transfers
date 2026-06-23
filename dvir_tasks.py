"""
מספור משימות Monday + סיכום יומי — הועברו מ-GitHub Actions ל-worker הקבוע של transfers.

רקע: עד 16/06/2026 שני הדברים רצו בתוך ה-GitHub Action "Dvir Sentinel" (חינמי).
ה-Action הושבת בגלל בעיית חיוב ב-GitHub → שבוע בלי מספור משימות ובלי הדייג'סט היומי.
כאן זה רץ על תשתית always-on אמינה (Render Background Worker, worker.py) — לא שורף
דקות חינם ולא תלוי בתזמון לא-אמין של GitHub.

שני job-ים, שניהם מבודדים ב-try/except (כשל אחד לא נוגע בשאר ה-worker):
  • run_numberer()  — שעתי: ממספר משימות חדשות בבורד הסוכנים (5092673295). אידמפוטנטי
                       (מדלג על מה שכבר ממוספר), אז בטוח להריץ שוב ושוב.
  • run_summary()   — יומי 10:30 IL: דייג'סט טלגרם של משימות פתוחות לצ'אט של אסי.

monday_tasks.py מוטמע (vendored) לתוך repo transfers — ה-repo המקונן ש-Render בונה
ממנו לא רואה את agents/shared/. אם נשנה את הלוגיקה ב-agents/shared/monday_tasks.py
צריך לסנכרן גם את ההעתק כאן.

⚠️ monday_tasks קורא TELEGRAM_BOT_TOKEN + TELEGRAM_TASKS_CHAT_ID ברמת המודול, לכן
מגדירים את ה-chat לפני ה-import הראשון. הבוט של transfers מוכח שמגיע ל-448181407
(כך _morning_digest_job שולח לאסי).
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("dvir_tasks")

# חובה לפני import של monday_tasks (env ברמת המודול). אם TELEGRAM_TASKS_CHAT_ID כבר
# מוגדר ב-Render — לא נוגעים; אחרת ברירת מחדל לצ'אט של אסי (כמו _morning_digest_job).
os.environ.setdefault(
    "TELEGRAM_TASKS_CHAT_ID",
    os.getenv("TELEGRAM_ADMIN_CHAT", "448181407"),
)


def _client():
    token = os.getenv("MONDAY_API_TOKEN", "")
    if not token:
        return None
    from monday_tasks import MondayTasksClient  # vendored, ייבוא עצל
    return MondayTasksClient("dvir", token)


def run_numberer() -> int:
    """ממספר משימות ללא 'מס׳ משימה'. בטוח להרצה חוזרת. מחזיר כמה מוספרו."""
    try:
        client = _client()
        if client is None:
            logger.info("dvir-numberer: MONDAY_API_TOKEN missing — skip")
            return 0
        n = client.number_unnumbered_tasks()
        logger.info("dvir-numberer: numbered %s task(s)", n)
        return n
    except Exception as e:  # noqa: BLE001
        logger.warning("dvir-numberer failed: %s", e)
        return 0


def run_summary() -> int:
    """דייג'סט יומי של משימות פתוחות → טלגרם. מחזיר כמה משימות פתוחות נכללו."""
    try:
        client = _client()
        if client is None:
            logger.info("dvir-summary: MONDAY_API_TOKEN missing — skip")
            return 0
        n = client.send_open_tasks_summary()
        logger.info("dvir-summary: sent digest of %s open task(s)", n)
        return n
    except Exception as e:  # noqa: BLE001
        logger.warning("dvir-summary failed: %s", e)
        return 0
