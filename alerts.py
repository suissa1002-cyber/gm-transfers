"""
Alerts — "אכיפה רכה" של הקליטה דרך טלגרם (כי ה-API של הקופה קריאה-בלבד ואי אפשר לחסום).

שלושה מנגנונים:
  1. on_new_transfers  — פוש מיידי לסניף היעד כשנוצרת העברה חדשה.
  2. check_aging       — תזכורת לסניף אחרי RECEIVE_REMINDER_HOURS; הסלמה למנהלים אחרי
                         RECEIVE_ESCALATE_HOURS. כל דגל נשלח פעם אחת (notified/reminded/escalated).
  3. daily_digest      — דוח יומי (09:00) של כל ההעברות הפתוחות, מקובץ לפי סניף יעד.

אם אין TELEGRAM_BOT_TOKEN — הכל מדלג בשקט (כמו monday_tasks).
"""

import logging
from datetime import datetime, timezone

import requests

import config as cfg
import db

logger = logging.getLogger("transfers.alerts")


def _send(chat_id, text: str) -> bool:
    if not cfg.TELEGRAM_BOT_TOKEN or not chat_id:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{cfg.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": str(chat_id), "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=15)
        if r.status_code != 200:
            logger.warning("telegram send failed %s: %s", r.status_code, r.text[:200])
        return r.status_code == 200
    except requests.RequestException as e:
        logger.warning("telegram send error: %s", e)
        return False


def _branch_chat(branch_id) -> str:
    return cfg.TELEGRAM_BRANCH_CHATS.get(str(branch_id))


def _link(op_id: str) -> str:
    if cfg.APP_BASE_URL:
        return f"\n👉 {cfg.APP_BASE_URL}/?op={op_id}"
    return ""


def _age_hours(t: dict) -> float:
    stamp = t.get("created_at") or t.get("first_seen")
    if not stamp:
        return 0.0
    try:
        d = datetime.fromisoformat(stamp.split(".")[0].replace("Z", ""))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc).astimezone()
        return (datetime.now().astimezone() - d).total_seconds() / 3600.0
    except (ValueError, AttributeError):
        return 0.0


# ──────────────────────────────────────────────────────────────
def on_new_transfers(op_ids: list[str]):
    """נקרא ע"י ה-poller כשנכנסו העברות חדשות ל-DB."""
    for op_id in op_ids:
        t = db.get_transfer(op_id)
        if not t or t.get("notified_new"):
            continue
        frm = cfg.branch_name(t.get("from_branch_id"))
        to = cfg.branch_name(t.get("to_branch_id"))
        msg = (f"📦 <b>העברה חדשה בדרך אליך</b>\n"
               f"מ<b>{frm}</b> → <b>{to}</b>\n"
               f"פעולה {op_id} · {t.get('total_units')} פריטים\n"
               f"נא לסרוק את המכשירים בקבלה.{_link(op_id)}")
        chat = _branch_chat(t.get("to_branch_id"))
        sent = _send(chat, msg) if chat else False
        # אם אין chat לסניף — מודיעים למנהלים שיש העברה ללא ערוץ סניף
        if not chat:
            _send(cfg.TELEGRAM_MANAGERS_CHAT_ID,
                  f"📦 העברה {op_id} {frm}→{to} ({t.get('total_units')} פריטים) — "
                  f"אין ערוץ טלגרם לסניף היעד.")
        db.mark_transfer_flag(op_id, "notified_new")


def check_aging():
    """תזכורות והסלמות לפי גיל ההעברה הפתוחה."""
    for t in db.open_transfers_for_alerts():
        age = _age_hours(t)
        op_id = t["op_id"]
        frm = cfg.branch_name(t.get("from_branch_id"))
        to = cfg.branch_name(t.get("to_branch_id"))
        remaining = t["total_units"] - t["received_units"]

        # הסלמה למנהלים
        if age >= cfg.RECEIVE_ESCALATE_HOURS and not t.get("escalated"):
            _send(cfg.TELEGRAM_MANAGERS_CHAT_ID,
                  f"🔴 <b>העברה לא נקלטה מעל {cfg.RECEIVE_ESCALATE_HOURS}ש'</b>\n"
                  f"פעולה {op_id} · {frm} → {to}\n"
                  f"חסרים {remaining}/{t['total_units']} · גיל {int(age)}ש'{_link(op_id)}")
            db.mark_transfer_flag(op_id, "escalated")
            continue

        # תזכורת לסניף
        if age >= cfg.RECEIVE_REMINDER_HOURS and not t.get("reminded"):
            chat = _branch_chat(t.get("to_branch_id"))
            _send(chat or cfg.TELEGRAM_MANAGERS_CHAT_ID,
                  f"⏰ <b>תזכורת קליטה</b>\n"
                  f"פעולה {op_id} מ{frm} ממתינה — חסרים {remaining}/{t['total_units']}."
                  f"{_link(op_id)}")
            db.mark_transfer_flag(op_id, "reminded")


def daily_digest():
    """דוח יומי של כל ההעברות הפתוחות, מקובץ לפי סניף יעד."""
    rows = db.open_transfers_for_alerts()
    if not rows:
        _send(cfg.TELEGRAM_MANAGERS_CHAT_ID, "✅ אין העברות פתוחות. כל הכבוד!")
        return
    by_to = {}
    for t in rows:
        by_to.setdefault(t.get("to_branch_id"), []).append(t)
    lines = [f"📋 <b>העברות פתוחות לקליטה</b> ({len(rows)})", ""]
    for to_id, items in by_to.items():
        lines.append(f"<b>{cfg.branch_name(to_id)}</b>:")
        for t in items:
            rem = t["total_units"] - t["received_units"]
            lines.append(f"  • פעולה {t['op_id']} מ{cfg.branch_name(t.get('from_branch_id'))} "
                         f"— חסרים {rem}/{t['total_units']} (גיל {int(_age_hours(t))}ש')")
        lines.append("")
    _send(cfg.TELEGRAM_MANAGERS_CHAT_ID, "\n".join(lines))
