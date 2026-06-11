"""
wa.py — WhatsApp (ConnectOp/ChatRace) backend לטאב 💬 ב-GreenOS.

שני קליינטים vendored ב-shared/:
  • chatrace_dashboard_client — ה-API הפנימי של הדשבורד (קריאת inbox/שיחות,
    שליחת template, ארכוב, מתג אנושי). טוקן cookie שפג ~10-22 ימים (token_watch מנטר).
  • connectop_client — ה-API הציבורי (שליחת טקסט חופשי). טוקן יציב.

כללי ברזל של אורי, נאכפים כאן בצד שרת (ה-UI לא יכול לעקוף):
  • חלון 24 שעות נקבע אך ורק לפי הודעה נכנסת (direction=in) — לעולם לא לפי
    last_active (כולל יוצאות!). מחוץ לחלון → טקסט חופשי נכשל בשקט אצל Meta,
    לכן השרת מסרב ומציע template `new_message`.
  • template נשלח רק דרך הדשבורד (ה-API הציבורי מחזיר success אבל לא מוסר).
  • body של new_message חייב שורה אחת — מקפלים שורות ל-" — ".
  • שליחה כ"אנושי" בלי toggle_human_mode (ה-toggle שובר את ה-UI של ConnectOp).
"""
import logging
import re
import sys
import time
from pathlib import Path
from threading import Lock

sys.path.insert(0, str(Path(__file__).parent / "shared"))

import config as cfg

logger = logging.getLogger("wa")

WINDOW_HOURS = 24
_INBOX_CACHE_TTL = 10  # שניות — מגן על ConnectOp מ-polling צפוף של כמה לשוניות

_lock = Lock()
_dash_client = None
_pub_client = None
_inbox_cache = {"at": 0.0, "rows": None}


class WaError(Exception):
    """שגיאה עם הודעה ידידותית להצגה ב-UI."""


def _dash():
    global _dash_client
    if _dash_client is None:
        from chatrace_dashboard_client import ChatRaceDashboardClient
        _dash_client = ChatRaceDashboardClient.from_env()
    return _dash_client


def _pub():
    global _pub_client
    if _pub_client is None:
        from connectop_client import ConnectOpClient
        _pub_client = ConnectOpClient.from_env()
    return _pub_client


def _dash_call(fn, *args, **kwargs):
    """עוטף קריאת דשבורד ומתרגם כשל טוקן להודעה ברורה."""
    from chatrace_dashboard_client import ChatRaceDashboardError
    try:
        return fn(*args, **kwargs)
    except ChatRaceDashboardError as e:
        if "code': 1" in str(e) or '"code": 1' in str(e):
            raise WaError("טוקן הדשבורד של ConnectOp פג/נדחה — יש לחדש (sync_dashboard_token)") from e
        raise WaError(f"שגיאת ConnectOp: {e}") from e


# ── Inbox ────────────────────────────────────────────────────────────

def list_conversations(limit: int = 200, include_archived: bool = False):
    """רשימת שיחות מה-inbox (עם micro-cache קצר)."""
    now = time.time()
    with _lock:
        cached = _inbox_cache["rows"]
        if cached is not None and now - _inbox_cache["at"] < _INBOX_CACHE_TTL:
            rows = cached
        else:
            resp = _dash_call(_dash()._post_user_php,
                              {"op": "conversations", "op1": "get",
                               "offset": 0, "limit": limit})
            rows = resp.get("data", []) if isinstance(resp, dict) else []
            _inbox_cache.update(at=now, rows=rows)
    out = []
    for r in rows:
        if str(r.get("channel")) != "5":  # WhatsApp בלבד
            continue
        if str(r.get("blocked", "0")) == "1":
            continue
        archived = str(r.get("archived", "0")) == "1"
        if archived and not include_archived:
            continue
        ts_ms = int(r.get("timestamp") or 0)
        last_read = int(r.get("last_read_page") or 0)
        out.append({
            "phone": r.get("ms_id"),
            "name": r.get("full_name") or r.get("first_name") or r.get("ms_id"),
            "last_msg": (r.get("last_msg") or "")[:120],
            "ts": ts_ms // 1000,
            "archived": archived,
            "live_chat": str(r.get("live_chat", "0")) == "1",
            "unread": bool(ts_ms and last_read and ts_ms / 1000 > last_read + 2),
            "pic": r.get("profile_pic") or "",
        })
    out.sort(key=lambda c: c["ts"], reverse=True)
    return out


# ── Thread + חלון 24 שעות ───────────────────────────────────────────

def _window_state(msgs):
    """מצב חלון ה-24ש לפי ההודעה הנכנסת (direction=in) האחרונה בלבד."""
    last_in = max((m.get("ts") or 0 for m in msgs if m.get("direction") == "in"),
                  default=0)
    if not last_in:
        return {"in_window": False, "hours_left": 0, "last_inbound_ts": 0}
    hours = WINDOW_HOURS - (time.time() - last_in) / 3600
    return {"in_window": hours > 0,
            "hours_left": round(max(0, hours), 1),
            "last_inbound_ts": last_in}


def get_thread(phone: str, limit: int = 60):
    """שיחה מפוענחת (ישן→חדש) + מצב חלון 24ש."""
    msgs = _dash_call(_dash().get_conversation, phone, limit=limit)
    msgs = list(reversed(msgs))  # הדשבורד מחזיר חדש→ישן
    slim = [{
        "id": m.get("id"),
        "direction": m.get("direction"),
        "text": m.get("text") or "",
        "ts": m.get("ts") or 0,
        "sent_by": m.get("sent_by"),
    } for m in msgs]
    return {"phone": phone, "messages": slim, "window": _window_state(slim)}


# ── שליחה ────────────────────────────────────────────────────────────

def send_reply(phone: str, text: str):
    """
    מענה אנושי. אוכף חלון 24ש בצד שרת:
    בתוך החלון → טקסט חופשי; מחוץ לחלון → 409 לוגי (needs_template) —
    ה-UI מציע לשלוח כ-template new_message.
    """
    text = (text or "").strip()
    if not text:
        raise WaError("הודעה ריקה")
    if re.search(r"\btest\b|\bping\b", text, re.IGNORECASE):
        raise WaError("ההודעה מכילה test/ping — חסום (כלל ברזל: בלי ניסויים על לקוחות)")
    win = _window_state(get_thread(phone, limit=60)["messages"])
    if not win["in_window"]:
        return {"sent": False, "needs_template": True, "window": win}
    resp = _pub().send_text_as_human(phone, text)
    logger.info("wa send text -> %s (%d chars)", phone, len(text))
    return {"sent": True, "via": "text", "window": win, "resp": resp}


def send_template(phone: str, name: str, body: str):
    """
    שליחה מחוץ לחלון: template `new_message` (מאושר מטא) עם [שם, גוף].
    הגוף חייב שורה אחת — מקפלים שורות/טאבים ל-" — ".
    """
    body = re.sub(r"\s*\n+\s*", " — ", (body or "").strip())
    body = re.sub(r"\s{4,}|\t+", " ", body)
    if not body:
        raise WaError("גוף הודעה ריק")
    resp = _dash_call(_dash().send_whatsapp_template,
                      phone, "new_message", [name or "לקוח/ה יקר/ה", body])
    logger.info("wa send template new_message -> %s", phone)
    return {"sent": True, "via": "template", "resp": resp}


# ── פעולות שיחה ─────────────────────────────────────────────────────

def archive(phone: str, archived: bool = True):
    _dash_call(_dash().archive_conversation, phone, archive=archived)
    with _lock:
        _inbox_cache["rows"] = None  # שהשינוי ייראה מיד
    return {"ok": True}


def set_human(phone: str, enable: bool = True):
    """
    מתג אנושי/בוט. ⚠️ ידוע: שולח עדכון WebSocket שעלול להקריס את ה-UI של
    ConnectOp אם הוא פתוח בדפדפן במקביל (לקח 03/06/2026) — לכן ב-UI שלנו
    זה כפתור מפורש עם אזהרה, לא אוטומטי.
    """
    _dash_call(_dash().set_human_mode, phone, enable=enable)
    with _lock:
        _inbox_cache["rows"] = None
    return {"ok": True}
