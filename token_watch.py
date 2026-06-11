"""
token_watch — ניטור תפוגת טוקן הדשבורד של ConnectOp (ChatRace).

הטוקן (cookie מהתחברות Google ל-newapp.connectop.co.il) הוא JWT עם שדה
`expire`, והוא משרת את: uri-stock-watcher (thread view), ה-CF Worker
uri-webhook (bot-escape), וטאב הוואטסאפ העתידי ב-GreenOS. כשהוא פג —
קריאת שיחות נשברת בשקט (status=ERROR code=1).

בדיקה יומית: פענוח ה-expire מה-JWT + אימות חי קל מול ConnectOp.
התראת טלגרם למנהלים כשנשארו ≤3 ימים, וקריטית כשפג/נפסל.
חידוש: התחברות בדפדפן → cookie `token` → הרצת
agents/uri/cli/sync_dashboard_token.py (מסנכרן את כל הצרכנים).
"""
import base64
import json
import logging
import time

import requests

import config as cfg
from alerts import _send

logger = logging.getLogger("token_watch")

ALERT_DAYS = 3  # מתריעים מ-3 ימים לפני תפוגה

RENEW_HOWTO = (
    "🔧 חידוש: להתחבר ל-newapp.connectop.co.il בדפדפן → "
    "DevTools → Application → Cookies → להעתיק את `token` → "
    "להריץ sync_dashboard_token.py (או להדביק לסוכן אורי/רון)."
)


def _decode_expire(token: str):
    """expire (epoch seconds) מתוך ה-JWT, או None אם לא ניתן לפענוח."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("expire") or data.get("exp")
        return int(exp) if exp else None
    except Exception:  # noqa: BLE001
        return None


def _live_check(token: str) -> bool:
    """אימות חי קל (limit=1) — תופס טוקן שנפסל לפני מועד התפוגה."""
    account = (cfg.CHATRACE_ACCOUNT_ID or "").strip()
    if not account:
        return True  # אין מזהה חשבון — מדלגים על האימות החי
    body = {"param": json.dumps([{
        "op": "conversations", "op1": "get", "offset": 0, "limit": 1,
        "page_id": account, "pageName": "inbox",
    }])}
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Cookie": f"token={token}; last_page_id={account}; lang=he",
        "Origin": "https://newapp.connectop.co.il",
        "Referer": f"https://newapp.connectop.co.il/en/inbox?acc={account}",
        "X-Requested-With": "XMLHttpRequest",
    }
    try:
        r = requests.post("https://newapp.connectop.co.il/php/user.php",
                          data=body, headers=headers, timeout=15)
        if r.status_code != 200:
            return False
        resp = r.json()
        first = resp[0] if isinstance(resp, list) and resp else resp
        return isinstance(first, dict) and "data" in first
    except Exception as e:  # noqa: BLE001
        logger.warning("live check failed (network?): %s", e)
        return True  # תקלת רשת רגעית — לא מתריעים על סמך זה


def _alert_once_a_day(text: str):
    """שולח התראה לכל היותר פעם ביום — cold starts של Render מריצים את
    בדיקת ה-boot בכל התעוררות, ובלי dedup זה היה מציף."""
    import db
    today = time.strftime("%Y-%m-%d")
    if db.sales_state_get("token_watch_last_alert") == today:
        logger.info("token alert already sent today — skipping")
        return
    if _send(cfg.TELEGRAM_MANAGERS_CHAT_ID, text):
        db.sales_state_set("token_watch_last_alert", today)


def check():
    """ריצה יומית מה-scheduler (+ בדיקת boot)."""
    token = (cfg.CHATRACE_DASHBOARD_TOKEN or "").strip()
    if not token:
        logger.warning("CHATRACE_DASHBOARD_TOKEN not set — skipping watch")
        return
    exp = _decode_expire(token)
    now = time.time()
    if exp is None:
        _alert_once_a_day("⚠️ ConnectOp: טוקן הדשבורד לא ניתן לפענוח (לא JWT?). " + RENEW_HOWTO)
        return
    days_left = (exp - now) / 86400
    expires_str = time.strftime("%d/%m/%Y %H:%M", time.localtime(exp))

    if days_left <= 0:
        _alert_once_a_day(
            f"🚨 ConnectOp: טוקן הדשבורד פג ({expires_str})! "
            f"קריאת שיחות וואטסאפ שבורה עכשיו (bot-escape, thread view).\n{RENEW_HOWTO}")
        return
    if not _live_check(token):
        _alert_once_a_day(
            f"🚨 ConnectOp: טוקן הדשבורד נדחה ע\"י השרת למרות שתוקפו עד {expires_str} "
            f"(נפסל/הוחלף?).\n{RENEW_HOWTO}")
        return
    if days_left <= ALERT_DAYS:
        _alert_once_a_day(
            f"⏳ ConnectOp: טוקן הדשבורד יפוג בעוד {days_left:.1f} ימים "
            f"({expires_str}). כדאי לחדש עכשיו.\n{RENEW_HOWTO}")
    else:
        logger.info("ConnectOp dashboard token OK — %.1f days left (%s)",
                    days_left, expires_str)
