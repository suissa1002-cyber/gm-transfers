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
    import db
    stars = db.wa_stars()
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
        phone = r.get("ms_id")
        out.append({
            "phone": phone,
            "name": r.get("full_name") or r.get("first_name") or phone,
            "last_msg": (r.get("last_msg") or "")[:120],
            "ts": ts_ms // 1000,
            "archived": archived,
            "live_chat": str(r.get("live_chat", "0")) == "1",
            "unread": bool(ts_ms and last_read and ts_ms / 1000 > last_read + 2),
            "star": phone in stars,
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


_MEDIA_TYPES = ("image", "video", "audio", "document", "file", "sticker")


def _extract_media(content):
    """חילוץ מדיה מבלוקי תוכן: [{'type','url','caption'}]. מבנה אמיתי (אומת):
    {"type":"image","image":{"link":"https://cdnj1.com/..."}} — ה-CDN פומבי."""
    out = []
    if not isinstance(content, list):
        return out
    for b in content:
        if not isinstance(b, dict) or b.get("type") not in _MEDIA_TYPES:
            continue
        inner = b.get(b["type"]) or {}
        if not isinstance(inner, dict):
            continue
        url = inner.get("link") or inner.get("url") or ""
        if url:
            out.append({"type": b["type"], "url": url,
                        "caption": inner.get("caption") or ""})
    return out


def _render_template_block(b):
    """מרנדר בלוק template להודעה שהלקוח באמת קיבל: גוף התבנית המאושרת
    עם הפרמטרים שמולאו (שניהם זמינים — הפרמטרים בבלוק, הגוף ב-wa_templates)."""
    tpl = (b.get("template") or {})
    name = tpl.get("name") or ""
    params = []
    for comp in tpl.get("components", []):
        if isinstance(comp, dict) and comp.get("type") == "body":
            params = [p.get("text", "") for p in comp.get("parameters", [])
                      if isinstance(p, dict) and p.get("type") == "text"]
    body = ""
    try:
        t = next((t for t in wa_templates() if t["id"] == name), None)
        body = (t or {}).get("body") or ""
    except Exception:  # noqa: BLE001
        body = ""
    if not body:
        return f"[template:{name}]", name
    for i, p in enumerate(params, 1):
        body = body.replace("{{%d}}" % i, str(p))
    return body, name


def get_thread(phone: str, limit: int = 60):
    """שיחה מפוענחת (ישן→חדש) + מצב חלון 24ש."""
    msgs = _dash_call(_dash().get_conversation, phone, limit=limit)
    msgs = list(reversed(msgs))  # הדשבורד מחזיר חדש→ישן
    slim = []
    for m in msgs:
        text = m.get("text") or ""
        kind = tpl_name = None
        content = m.get("content")
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "template":
                    text, tpl_name = _render_template_block(b)
                    kind = "template"
                    break
                if b.get("type") == "interactive":
                    kind = "interactive"
        if kind == "interactive":
            text = text.replace("[interactive]", "", 1).strip()
        slim.append({
            "id": m.get("id"),
            "direction": m.get("direction"),
            "text": text,
            "kind": kind,
            "tpl": tpl_name,
            "media": _extract_media(content),
            "ts": m.get("ts") or 0,
            "sent_by": m.get("sent_by"),
        })
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


def send_media(phone: str, filename: str, content: bytes, mime: str, caption: str = ""):
    """
    שליחת קובץ ללקוח: מעלים ל-WordPress media (URL ציבורי) ואז שולחים
    דרך ה-API הציבורי (`send_file` דורש URL). גם מדיה כפופה לחלון 24ש.
    """
    import os
    import requests as rq
    if len(content) > 15 * 1024 * 1024:
        raise WaError("קובץ גדול מדי (מקס׳ 15MB)")
    win = _window_state(get_thread(phone, limit=60)["messages"])
    if not win["in_window"]:
        raise WaError("מחוץ לחלון 24ש׳ — אי אפשר לשלוח מדיה (רק template טקסט)")
    base = os.getenv("WC_STORE_URL", "").rstrip("/")
    user = os.getenv("WP_USERNAME", "")
    pw = os.getenv("WP_APP_PASSWORD", "")
    if not (base and user and pw):
        raise WaError("חסרים פרטי WordPress להעלאת הקובץ (WP_USERNAME/WP_APP_PASSWORD)")
    safe = re.sub(r"[^\w.\-]+", "_", filename or "file")
    try:
        r = rq.post(f"{base}/wp-json/wp/v2/media",
                    headers={"Content-Disposition": f'attachment; filename="{safe}"',
                             "Content-Type": mime or "application/octet-stream"},
                    data=content, auth=(user, pw), timeout=60)
        if r.status_code not in (200, 201):
            raise WaError(f"העלאה ל-WordPress נכשלה ({r.status_code})")
        url = r.json().get("source_url", "")
    except WaError:
        raise
    except Exception as e:  # noqa: BLE001
        raise WaError(f"העלאת קובץ נכשלה: {e}") from e
    if not url:
        raise WaError("WordPress לא החזיר כתובת קובץ")
    kind = ("image" if (mime or "").startswith("image/") else
            "video" if (mime or "").startswith("video/") else
            "audio" if (mime or "").startswith("audio/") else "document")
    resp = _pub().send_file(phone, url, caption=caption, file_type=kind)
    logger.info("wa send %s -> %s (%s)", kind, phone, safe)
    return {"sent": True, "via": kind, "url": url, "resp": resp}


# ── פעולות שיחה ─────────────────────────────────────────────────────

# ── תשובות שמורות + תבניות מאושרות מ-ConnectOp ─────────────────────
# התגלית (11/06, מתוך inbox.js של הדשבורד): התשובות חוזרות בשדה `results`
# (לא `data`!): op=inbox_saved_reply/get; תבניות: op=whatsapp/templates/get.

_sr_cache = {"at": 0.0, "items": None}
_tpl_cache = {"at": 0.0, "items": None}


def saved_replies():
    """התשובות השמורות של ConnectOp (read-only אצלנו), cache 5 דקות."""
    now = time.time()
    if _sr_cache["items"] is None or now - _sr_cache["at"] > 300:
        r = _dash_call(_dash()._post_user_php, {"op": "inbox_saved_reply", "op1": "get"})
        items = r.get("results", []) if isinstance(r, dict) else []
        _sr_cache.update(at=now, items=[
            {"id": i.get("id"), "title": (i.get("shortcode") or "").lstrip("/"),
             "text": i.get("value") or ""} for i in items])
    return _sr_cache["items"]


def wa_templates():
    """תבניות WhatsApp מאושרות-מטא, מפוענחות: id, שפה, טקסט, מספר פרמטרים."""
    import json as _json
    now = time.time()
    if _tpl_cache["items"] is None or now - _tpl_cache["at"] > 600:
        r = _dash_call(_dash()._post_user_php, {"op": "whatsapp", "op1": "templates", "op2": "get"})
        out = []
        for t in (r.get("results", []) if isinstance(r, dict) else []):
            try:
                jb = _json.loads(t.get("json_builder") or "{}")
            except Exception:  # noqa: BLE001
                jb = {}
            body = ((jb.get("body") or {}).get("text")) or ""
            nums = [int(m) for m in re.findall(r"\{\{(\d+)\}\}", body)]
            out.append({"id": t.get("id"), "language": t.get("language") or "he",
                        "body": body, "params": max(nums) if nums else 0})
        _tpl_cache.update(at=now, items=out)
    return _tpl_cache["items"]


def send_wa_template(phone: str, template_id: str, params=None, language: str = "he"):
    """שליחת תבנית מאושרת כלשהי (לא רק new_message) — עוקפת את חלון ה-24ש."""
    tpl = next((t for t in wa_templates() if t["id"] == template_id), None)
    if tpl is None:
        raise WaError(f"תבנית '{template_id}' לא נמצאה")
    params = [str(p) for p in (params or [])]
    if len(params) < tpl["params"]:
        raise WaError(f"התבנית דורשת {tpl['params']} פרמטרים, התקבלו {len(params)}")
    resp = _dash_call(_dash().send_whatsapp_template,
                      phone, template_id, params, language=tpl.get("language") or language)
    logger.info("wa send template %s -> %s", template_id, phone)
    return {"sent": True, "via": f"template:{template_id}", "resp": resp}


# ── כרטיס פונה: ConnectOp + הזמנות אתר + מטא שלנו ──────────────────

_tags_cache = {"at": 0.0, "tags": None}


def account_tags():
    """רשימת התגים של החשבון (81+), cache 10 דקות."""
    now = time.time()
    if _tags_cache["tags"] is None or now - _tags_cache["at"] > 600:
        _tags_cache["tags"] = _pub().get_tags()
        _tags_cache["at"] = now
    return _tags_cache["tags"]


def _wc_orders_by_phone(phone: str, limit: int = 5):
    """הזמנות WooCommerce לפי טלפון (פורמט בינלאומי תואם ישירות ל-ConnectOp)."""
    import os
    import requests as rq
    base = os.getenv("WC_STORE_URL", "").rstrip("/")
    auth = (os.getenv("WC_CONSUMER_KEY", ""), os.getenv("WC_CONSUMER_SECRET", ""))
    if not base or not auth[0]:
        return None  # WC לא מוגדר — הפאנל פשוט לא יציג הזמנות
    try:
        r = rq.get(f"{base}/wp-json/wc/v3/orders",
                   params={"search": str(phone), "per_page": limit},
                   auth=auth, timeout=25)
        if not r.ok:
            return None
        return [{
            "id": o.get("id"),
            "status": o.get("status"),
            "total": o.get("total"),
            "currency": o.get("currency_symbol") or "₪",
            "date": (o.get("date_created") or "")[:16].replace("T", " "),
            "items": [i.get("name") for i in (o.get("line_items") or [])][:4],
            "admin_url": f"{base}/wp-admin/post.php?post={o.get('id')}&action=edit",
        } for o in r.json()]
    except Exception as e:  # noqa: BLE001
        logger.warning("wc orders lookup failed: %s", e)
        return None


def contact_card(phone: str):
    """כל פרטי הפונה במקום אחד: ConnectOp + תגיות + note + הזמנות אתר + מטא שלנו."""
    import db
    card = {"phone": phone}
    try:
        c = _pub()._req("GET", f"/contacts/{phone}")
        card["contact"] = {k: c.get(k) for k in
                           ("full_name", "first_name", "last_name", "email",
                            "subscribed_date", "live_chat", "blocked", "wa_user_id")}
    except Exception as e:  # noqa: BLE001
        card["contact"] = None
        logger.warning("contact fetch failed: %s", e)
    try:
        card["tags"] = _pub()._req("GET", f"/contacts/{phone}/tags") or []
    except Exception:  # noqa: BLE001
        card["tags"] = []
    try:
        cfs = _pub()._req("GET", f"/contacts/{phone}/custom_fields") or []
        card["note_cf"] = next((f.get("value") for f in cfs if f.get("name") == "note"), "")
        card["custom_fields"] = [f for f in cfs if f.get("name") != "note"]
    except Exception:  # noqa: BLE001
        card["note_cf"] = ""
        card["custom_fields"] = []
    card["orders"] = _wc_orders_by_phone(phone)
    card["notes"] = db.wa_notes_list(phone)
    card["star"] = phone in db.wa_stars()
    return card


def set_tag(phone: str, tag_id, add: bool = True):
    if add:
        _pub().add_tag(phone, tag_id)
    else:
        _pub().remove_tag(phone, tag_id)
    return {"ok": True}


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
