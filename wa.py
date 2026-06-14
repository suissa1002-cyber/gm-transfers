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
    """חילוץ מדיה מבלוקי תוכן: [{'type','url','caption'}]. שני מבנים אמיתיים (אומתו):
    יוצא:  {"type":"image","image":{"link":"https://cdnj1.com/..."}}
    נכנס:  {"attachment":{"type":"image","payload":{"url":"https://cdnj1.com/..."}}}
    ה-CDN פומבי בשני המקרים."""
    out = []
    if not isinstance(content, list):
        return out
    for b in content:
        if not isinstance(b, dict):
            continue
        # מבנה נכנס (מהלקוח): attachment.payload.url
        att = b.get("attachment")
        if isinstance(att, dict) and att.get("type") in _MEDIA_TYPES:
            payload = att.get("payload") or {}
            url = payload.get("url") or payload.get("link") or ""
            if url:
                out.append({"type": att["type"], "url": url,
                            "caption": payload.get("caption") or ""})
            continue
        # מבנה יוצא: type + <type>.link
        if b.get("type") not in _MEDIA_TYPES:
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


# ── זיהוי "פנה מעמוד מוצר" מתוך השורה הגלויה שכפתור ה-WhatsApp (Chaty) ממלא מראש ──
# פורמט הסניפט: "שלום, אני מתעניין/ת לגבי המוצר: <שם המוצר>" (גלוי, נקי, בלי קישור).
# מחלצים את המוצר האחרון שעליו פנה הלקוח להצגה בכרטיסיית הפרטים (דינמי).
_RE_INTEREST = re.compile(r"לגבי המוצר:\s*([^\n\r]+)")


def _parse_entry_product(text: str):
    if not text or "לגבי המוצר:" not in text:
        return None
    m = _RE_INTEREST.search(text)
    if not m:
        return None
    name = m.group(1).strip()
    return {"name": name[:140], "url": ""} if name else None


def get_thread(phone: str, limit: int = 60):
    """שיחה מפוענחת (ישן→חדש) + מצב חלון 24ש."""
    msgs = _dash_call(_dash().get_conversation, phone, limit=limit)
    msgs = list(reversed(msgs))  # הדשבורד מחזיר חדש→ישן
    slim = []
    entry_product = None
    for m in msgs:
        text = m.get("text") or ""
        if m.get("direction") == "in":
            ep = _parse_entry_product(text)       # פנייה מעמוד מוצר — שומרים את האחרונה (הכי עדכנית)
            if ep:
                entry_product = ep
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
        # reply של הלקוח להודעה שלנו: בלוקים נכנסים נושאים context עם ה-wamid המצוטט
        reply_to = None
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict):
                    c = b.get("context")
                    if isinstance(c, dict) and (c.get("id") or c.get("message_id")):
                        reply_to = c.get("id") or c.get("message_id")
                        break
        slim.append({
            "id": m.get("id"),
            "direction": m.get("direction"),
            "text": text,
            "kind": kind,
            "tpl": tpl_name,
            "media": _extract_media(content),
            "ts": m.get("ts") or 0,
            "sent_by": m.get("sent_by"),
            "reply_to": reply_to,
        })
    # מיזוג יומן הצל: הודעות שנשלחו ישירות דרך Meta (תבניות כפתור, replies) —
    # ConnectOp לא מציג אותן, אז הן נשמרות אצלנו וממוזגות לציר הזמן
    import db
    seen = {m["id"] for m in slim if m.get("id")}
    for sh in db.wa_shadow_list(phone):
        if sh.get("wamid") in seen:
            continue
        slim.append({
            "id": sh.get("wamid"), "direction": "out", "text": sh.get("text") or "",
            "kind": "shadow", "tpl": None, "media": [], "ts": sh.get("ts") or 0,
            "sent_by": "greenos", "reply_to": sh.get("reply_to") or None,
            "reply_preview": sh.get("reply_preview") or "",
        })
    slim.sort(key=lambda m: m.get("ts") or 0)
    # תצוגת הציטוט: ממפים wamid → קטע טקסט מההודעה המצוטטת
    by_id = {m["id"]: (m.get("text") or "") for m in slim if m.get("id")}
    for m in slim:
        if m.get("reply_to") and not m.get("reply_preview"):
            m["reply_preview"] = (by_id.get(m["reply_to"]) or "")[:90]
    return {"phone": phone, "messages": slim, "window": _window_state(slim),
            "entry_product": entry_product}


# ── קריאה מהחנות העצמאית שלנו (שלב 3 — ניתוק קונקטופ) ──
def get_thread_native(phone: str, limit: int = 80):
    """שיחה מ-wa_msg (החנות שלנו) — בלי לקרוא מקונקטופ. אותו פורמט כמו get_thread."""
    import db
    rows = db.wa_msg_thread(phone, limit=limit)
    slim = []
    for m in rows:
        media = []
        murl = m.get("media_url")
        if murl:
            mt = (m.get("media_mime") or "").split("/")[0] or m.get("type") or "file"
            media = [{"type": mt, "url": murl, "caption": ""}]
        text = _strip_entry_marker(m.get("text") or "") if "_strip_entry_marker" in globals() else (m.get("text") or "")
        slim.append({
            "id": m.get("wamid"), "direction": m.get("direction"),
            "text": text, "kind": m.get("type"), "tpl": None, "media": media,
            "ts": m.get("ts") or 0,
            "sent_by": "greenos" if m.get("direction") == "out" else None,
            "reply_to": m.get("reply_to") or None, "status": m.get("status"),
        })
    by_id = {x["id"]: (x.get("text") or "") for x in slim if x.get("id")}
    for x in slim:
        if x.get("reply_to") and not x.get("reply_preview"):
            x["reply_preview"] = (by_id.get(x["reply_to"]) or "")[:90]
    entry = None
    for x in slim:
        if x.get("direction") == "in":
            ep = _parse_entry_product(x.get("text") or "")
            if ep:
                entry = ep
    return {"phone": phone, "messages": slim, "window": _window_state(slim),
            "entry_product": entry, "source": "native"}


def list_conversations_native(limit: int = 300):
    """רשימת שיחות מ-wa_contact (החנות שלנו) — אותו פורמט כמו list_conversations."""
    import db
    stars = db.wa_stars()
    out = []
    for r in db.wa_conversations(limit):
        ph = r.get("phone")
        out.append({
            "phone": ph, "name": r.get("name") or ph,
            "last_msg": (r.get("last_msg") or "")[:120],
            "ts": int(r.get("last_msg_ts") or 0),
            "archived": bool(r.get("archived")), "live_chat": bool(r.get("live_chat")),
            "unread": False, "star": ph in stars, "pic": "",
        })
    return out


# ── שליחה ────────────────────────────────────────────────────────────

_human_auto_cache: dict = {}


def _auto_human(phone: str):
    """אחרי מענה אנושי (שלנו או טיוטה של אורי שנשלחה) — מסמנים את השיחה כ'אנושי'
    כדי שהבוט יפסיק לענות ללקוח (בקשת אסי 12/06: "אחרי שאני עונה ממשיך לקבל בוט").
    cache 30 דק' — לא חוזרים על ה-toggle בכל הודעה. כשל כאן לעולם לא מפיל שליחה.
    ⚠️ ה-toggle שולח עדכון WebSocket שעלול לשבור UI של ConnectOp אם פתוח במקביל
    (לקח 03/06) — התקבל במודע: GreenOS הוא ממשק העבודה; רענון מתקן אצלם."""
    now = time.time()
    if now - _human_auto_cache.get(phone, 0) < 1800:
        return
    try:
        _dash_call(_dash().set_human_mode, phone, enable=True)
        _human_auto_cache[phone] = now
        with _lock:
            _inbox_cache["rows"] = None
        logger.info("wa auto human-mode -> %s", phone)
    except Exception as e:  # noqa: BLE001
        logger.warning("auto human-mode failed for %s: %s", phone, e)


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
    _auto_human(phone)   # קודם מסמנים אנושי — שהבוט לא יקפוץ על ההודעה הבאה של הלקוח
    # שלב 4 (ניתוק קונקטופ): מעדיפים שליחה ישירה דרך Meta אם מופעל; אחרת ConnectOp.
    via = "text"
    wamid = ""
    if os.getenv("WA_SEND_VIA_META", "0") == "1" and meta_direct_ready():
        try:
            wamid = _meta_send_text(phone, text)
            via = "text-meta"
        except Exception as e:  # noqa: BLE001
            logger.warning("meta text send failed (%s) — ConnectOp fallback", e)
    if not wamid:
        _pub().send_text_as_human(phone, text)
    _store_outbound(phone, text, wamid=wamid, mtype="text")
    logger.info("wa send text -> %s via %s (%d chars)", phone, via, len(text))
    return {"sent": True, "via": via, "window": win}


def fetch_meta_media(media_id: str):
    """שלב 2 (מדיה): מוריד מדיה ממטא לפי media_id — קבלת URL זמני ואז הורדת הבייטים
    (מאומת בטוקן). מחזיר (bytes, mime)."""
    if not meta_direct_ready():
        raise WaError("Meta ישיר לא מוגדר")
    import os as _os
    import requests as _rq
    h = {"Authorization": f"Bearer {_os.getenv('META_WA_TOKEN').strip()}"}
    r1 = _rq.get(f"{META_GRAPH}/{media_id}", headers=h, timeout=30)
    if not r1.ok:
        raise WaError(f"media lookup failed ({r1.status_code})")
    info = r1.json()
    url = info.get("url")
    mime = info.get("mime_type") or "application/octet-stream"
    if not url:
        raise WaError("no media url")
    r2 = _rq.get(url, headers=h, timeout=60)
    if not r2.ok:
        raise WaError(f"media download failed ({r2.status_code})")
    return r2.content, mime


def _meta_send_text(phone: str, text: str) -> str:
    """שליחת טקסט חופשי ישירות דרך WhatsApp Cloud API של מטא. מחזיר wamid."""
    if not meta_direct_ready():
        raise WaError("Meta ישיר לא מוגדר")
    import os as _os
    import requests as _rq
    r = _rq.post(f"{META_GRAPH}/{_os.getenv('META_WA_PHONE_ID').strip()}/messages",
                 headers={"Authorization": f"Bearer {_os.getenv('META_WA_TOKEN').strip()}",
                          "Content-Type": "application/json"},
                 json={"messaging_product": "whatsapp", "to": phone, "type": "text",
                       "text": {"body": text}}, timeout=30)
    if r.status_code not in (200, 201):
        raise WaError(f"Meta text send failed ({r.status_code}): {r.text[:200]}")
    return ((r.json().get("messages") or [{}])[0]).get("id", "")


def _store_outbound(phone: str, text: str, wamid: str = "", mtype: str = "text",
                    media_url: str = ""):
    """שומר הודעה יוצאת בחנות העצמאית (wa_msg) — חובה כי webhook של מטא לא מחזיר
    את ההודעות שלנו. גם wa_shadow (לתצוגה ב-get_thread בזמן מעבר)."""
    import db
    ts = int(time.time())
    wid = wamid or f"gm-out-{ts}-{abs(hash(text)) % 100000}"
    try:
        db.wa_msg_upsert(wamid=wid, phone=str(phone), direction="out", mtype=mtype,
                         text=text or "", media_url=media_url or "", ts=ts, status="sent")
        db.wa_contact_upsert(str(phone), out_ts=ts)
    except Exception as e:  # noqa: BLE001
        logger.warning("store outbound failed: %s", e)
    if wamid:
        try:
            db.wa_shadow_add(str(phone), wamid, text or "", ts=ts)
        except Exception:  # noqa: BLE001
            pass


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
    _auto_human(phone)
    logger.info("wa send template new_message -> %s", phone)
    return {"sent": True, "via": "template", "resp": resp}


def send_media(phone: str, filename: str, content: bytes, mime: str, caption: str = ""):
    """
    שליחת קובץ ללקוח: מעלים ל-file manager של ConnectOp (multipart ל-user.php,
    op=inbox_file_manager/upload — אותו מנגנון של הדשבורד שלהם, הקובץ יושב על
    cdnj1) ואז שולחים דרך ה-API הציבורי. גם מדיה כפופה לחלון 24ש.
    הערה: ההעלאה הקודמת ל-WordPress נחסמה ע"י Cloudflare מ-IP של Render.
    """
    import json as _json
    if len(content) > 15 * 1024 * 1024:
        raise WaError("קובץ גדול מדי (מקס׳ 15MB)")
    win = _window_state(get_thread(phone, limit=60)["messages"])
    if not win["in_window"]:
        raise WaError("מחוץ לחלון 24ש׳ — אי אפשר לשלוח מדיה (רק template טקסט)")
    m = (mime or "").lower()
    kind = ("image" if m.startswith("image/") else
            "audio" if m.startswith("audio/") else
            "video" if m.startswith("video/") else "file")
    safe = re.sub(r"[^\w.\-]+", "_", filename or "file", flags=re.UNICODE) or "file"
    d = _dash()
    param = {"page_id": d.account_id, "op": "inbox_file_manager", "op1": "upload",
             "op2": kind, "file_name": safe, "uploadFB": False, "inbox": True,
             "ms_id": str(phone)}
    try:
        r = d._session.post(f"{d.base_url}/php/user.php",
                            data={"param": _json.dumps(param)},
                            files={"file": (safe, content, mime or "application/octet-stream")},
                            headers={"X-Requested-With": "XMLHttpRequest",
                                     "Referer": f"{d.base_url}/en/inbox?acc={d.account_id}"},
                            timeout=90)
        j = r.json() if r.ok else {}
    except Exception as e:  # noqa: BLE001
        raise WaError(f"העלאת קובץ ל-ConnectOp נכשלה: {e}") from e
    if not (isinstance(j, dict) and j.get("status") == "OK"):
        raise WaError(f"העלאה נדחתה: {str(j)[:120] or r.status_code} (טוקן דשבורד?)")
    url = (j.get("data") or {}).get("url", "")
    if not url:
        raise WaError("ConnectOp לא החזיר כתובת קובץ")
    send_kind = "document" if kind == "file" else kind
    _auto_human(phone)
    resp = _pub().send_file(phone, url, caption=caption, file_type=send_kind)
    logger.info("wa send %s -> %s (%s)", send_kind, phone, safe)
    return {"sent": True, "via": send_kind, "url": url, "resp": resp}


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
    _auto_human(phone)
    logger.info("wa send template %s -> %s", template_id, phone)
    return {"sent": True, "via": f"template:{template_id}", "resp": resp}


# ── שליחת תבנית תשלום ──
# המסלול המועדף: Meta Cloud API ישיר (תבנית payment_link עם כפתור URL דינמי —
# הקישור האישי של הלקוח בכפתור). דורש env: META_WA_TOKEN + META_WA_PHONE_ID.
# ⚠️ דרך ConnectOp זה בלתי אפשרי — נבדק אמפירית 12/06/2026: הם בולעים בשקט
# תבניות עם כפתור URL *דינמי* בכל צורת payload (קבוע/Quick-Reply כן עוברים).
# Fallback: תבנית payment_request (כפתור קבוע → /pay) דרך ConnectOp, אם תיווצר.
META_GRAPH = "https://graph.facebook.com/v23.0"
META_PAY_TEMPLATE = "payment_link"
PAY_TEMPLATE_ID = "payment_request"


def meta_direct_ready() -> bool:
    import os
    return bool(os.getenv("META_WA_TOKEN", "").strip() and os.getenv("META_WA_PHONE_ID", "").strip())


def _meta_send_template(phone: str, template: str, body_params: list, button_params: list):
    """שליחת תבנית ישירות דרך Meta Cloud API — עוקף את ConnectOp (שלא מסוגל
    להעביר כפתורי URL דינמיים). מחזיר message_id."""
    import os
    import requests as rq
    if not meta_direct_ready():
        raise WaError("חיבור Meta ישיר לא מוגדר (META_WA_TOKEN / META_WA_PHONE_ID)")
    components = [{"type": "body",
                   "parameters": [{"type": "text", "text": str(p)[:120]} for p in body_params]}]
    if button_params:
        components.append({"type": "button", "sub_type": "url", "index": "0",
                           "parameters": [{"type": "text", "text": str(button_params[0])}]})
    payload = {"messaging_product": "whatsapp", "to": str(phone), "type": "template",
               "template": {"name": template, "language": {"code": "he"},
                            "components": components}}
    r = rq.post(f"{META_GRAPH}/{os.getenv('META_WA_PHONE_ID').strip()}/messages",
                headers={"Authorization": f"Bearer {os.getenv('META_WA_TOKEN').strip()}",
                         "Content-Type": "application/json"},
                json=payload, timeout=40)
    try:
        j = r.json()
    except Exception:  # noqa: BLE001
        j = {}
    if r.status_code != 200 or not (j.get("messages") or []):
        err = ((j.get("error") or {}).get("message")) or str(j)[:150]
        logger.warning("meta direct send failed %s (%s): %s", r.status_code, template, str(j)[:300])
        raise WaError(f"Meta דחתה את השליחה: {err}")
    mid = (j["messages"][0] or {}).get("id", "")
    _auto_human(phone)
    return mid


# תבנית לתשלום כללי (ללא הזמנה): "קישור לתשלום על סך {{2}} עבור {{3}}" + כפתור
META_GENERAL_TEMPLATE = "payment_general"


def _tpl_body_filled(template_id: str, params: list) -> str:
    """גוף התבנית עם הפרמטרים — לרישום ביומן הצל (תצוגת השיחה)."""
    try:
        t = next((t for t in wa_templates() if t["id"] == template_id), None)
        body = (t or {}).get("body") or ""
        for i, p in enumerate(params, 1):
            body = body.replace("{{%d}}" % i, str(p))
        return body or f"[תבנית {template_id}]"
    except Exception:  # noqa: BLE001
        return f"[תבנית {template_id}]"


def _shadow(phone: str, wamid: str, text: str, reply_to: str = "", reply_preview: str = ""):
    try:
        import db
        db.wa_shadow_add(phone, wamid, text, reply_to, reply_preview, ts=int(time.time()))
    except Exception as e:  # noqa: BLE001
        logger.warning("shadow log failed: %s", e)


def send_pay_template_direct(phone: str, name: str, order_number: str, total: str, pru: str):
    """payment_link דרך Meta: גוף [שם, מס׳ הזמנה, סכום] + הקישור האישי בכפתור."""
    if not pru:
        raise WaError("חסר מזהה דף תשלום (pru)")
    params = [(name or "לקוח/ה יקר/ה")[:60], str(order_number), str(total)]
    mid = _meta_send_template(phone, META_PAY_TEMPLATE, params, [pru])
    _shadow(phone, mid, _tpl_body_filled(META_PAY_TEMPLATE, params) + "\n[🔘 לתשלום מאובטח]")
    logger.info("wa meta-direct pay-template -> %s (order %s, mid %s)", phone, order_number, mid)
    return {"sent": True, "via": "meta-direct", "message_id": mid}


def send_reply_quoted(phone: str, text: str, reply_to: str, reply_preview: str = ""):
    """
    מענה עם ציטוט (reply) להודעה ספציפית — דרך Meta Cloud API עם context.
    ConnectOp לא תומך בזה; ההודעה נרשמת ביומן הצל כדי להופיע בשיחה.
    """
    import os
    import requests as rq
    text = (text or "").strip()
    if not text:
        raise WaError("הודעה ריקה")
    if re.search(r"\btest\b|\bping\b", text, re.IGNORECASE):
        raise WaError("ההודעה מכילה test/ping — חסום")
    if not meta_direct_ready():
        raise WaError("חיבור Meta ישיר לא מוגדר")
    win = _window_state(get_thread(phone, limit=60)["messages"])
    if not win["in_window"]:
        return {"sent": False, "needs_template": True, "window": win}
    payload = {"messaging_product": "whatsapp", "to": str(phone),
               "context": {"message_id": reply_to},
               "type": "text", "text": {"body": text}}
    r = rq.post(f"{META_GRAPH}/{os.getenv('META_WA_PHONE_ID').strip()}/messages",
                headers={"Authorization": f"Bearer {os.getenv('META_WA_TOKEN').strip()}",
                         "Content-Type": "application/json"},
                json=payload, timeout=40)
    try:
        j = r.json()
    except Exception:  # noqa: BLE001
        j = {}
    if r.status_code != 200 or not (j.get("messages") or []):
        err = ((j.get("error") or {}).get("message")) or str(j)[:150]
        logger.warning("meta reply send failed %s: %s", r.status_code, str(j)[:300])
        raise WaError(f"שליחת ה-reply נכשלה: {err}")
    mid = (j["messages"][0] or {}).get("id", "")
    _auto_human(phone)
    _shadow(phone, mid, text, reply_to=reply_to, reply_preview=reply_preview)
    logger.info("wa meta-direct reply -> %s (quoting %s)", phone, (reply_to or "")[:30])
    return {"sent": True, "via": "meta-reply", "window": win, "message_id": mid}


def send_general_pay_direct(phone: str, name: str, total: str, desc: str, pru: str):
    """payment_general דרך Meta — תשלום כללי בלי הזמנה: [שם, סכום, תיאור] + כפתור."""
    if not pru:
        raise WaError("חסר מזהה דף תשלום (pru)")
    params = [(name or "לקוח/ה יקר/ה")[:60], str(total), (desc or "התשלום")[:60]]
    mid = _meta_send_template(phone, META_GENERAL_TEMPLATE, params, [pru])
    _shadow(phone, mid, _tpl_body_filled(META_GENERAL_TEMPLATE, params) + "\n[🔘 לתשלום מאובטח]")
    logger.info("wa meta-direct general-pay -> %s (%s, mid %s)", phone, desc, mid)
    return {"sent": True, "via": "meta-direct", "message_id": mid}


def pay_template_ready() -> bool:
    """האם תבנית התשלום כבר מאושרת ומסונכרנת ב-ConnectOp?"""
    try:
        return any(t["id"] == PAY_TEMPLATE_ID for t in wa_templates())
    except Exception:  # noqa: BLE001
        return False


def send_pay_template(phone: str, name: str, order_number: str, total: str, pru: str = ""):
    """
    שליחת בקשת תשלום כתבנית payment_request: גוף [שם, מס׳ הזמנה, סכום] +
    כפתור קבוע "לתשלום מאובטח" → /pay, שם הלקוח מקליד את מס׳ ההזמנה ומועבר
    ל-PayPlus. הכפתור לחיץ אצל כל לקוח, תמיד — גם בלי אינטראקציה קודמת.
    """
    tpl = next((t for t in wa_templates() if t["id"] == PAY_TEMPLATE_ID), None)
    if tpl is None:
        raise WaError(f"תבנית {PAY_TEMPLATE_ID} עוד לא סונכרנה מ-Meta")
    resp = _dash_call(_dash().send_whatsapp_template,
                      phone, PAY_TEMPLATE_ID,
                      [name or "לקוח/ה יקר/ה", str(order_number), str(total)],
                      language=tpl.get("language") or "he")
    _auto_human(phone)
    logger.info("wa send pay-template -> %s (order %s)", phone, order_number)
    return {"sent": True, "via": f"template:{PAY_TEMPLATE_ID}", "resp": resp}


def search_conversations(q: str, limit: int = 50):
    """חיפוש צד-שרת בכל אנשי הקשר (לא רק 200 השיחות האחרונות) — לפי שם/טלפון/אימייל,
    באותו מנגנון cdts של ה-UI של ConnectOp. ⚠️ בלי saveFilter — שליחתו שוברת את הדשבורד!"""
    q = (q or "").strip()
    if not q:
        return []
    digits = re.sub(r"[\s\-+()]", "", q)
    if digits.isdigit():
        cdt = {"atrb_name": "phone", "oprt": "6", "value1": [digits], "value2": None, "order": 1}
        if len(digits) > 8:
            cdt["checkContactId"] = True
    elif "@" in q:
        cdt = {"atrb_name": "email", "oprt": "6", "value1": [q], "value2": None, "order": 1}
    else:
        cdt = {"atrb_name": "user_name", "oprt": "6", "value1": [q], "value2": None, "order": 1}
    r = _dash_call(_dash()._post_user_php,
                   {"op": "conversations", "op1": "get", "offset": 0,
                    "limit": limit, "cdts": [cdt]})
    rows = r.get("data", []) if isinstance(r, dict) else []
    import db
    stars = db.wa_stars()
    out = []
    for c in rows:
        if str(c.get("channel")) != "5" or str(c.get("blocked", "0")) == "1":
            continue
        ts_ms = int(c.get("timestamp") or 0)
        phone = c.get("ms_id")
        out.append({
            "phone": phone,
            "name": c.get("full_name") or c.get("first_name") or phone,
            "last_msg": (c.get("last_msg") or "")[:120],
            "ts": ts_ms // 1000,
            "archived": str(c.get("archived", "0")) == "1",
            "live_chat": str(c.get("live_chat", "0")) == "1",
            "unread": False,
            "star": phone in stars,
            "pic": c.get("profile_pic") or "",
        })
    out.sort(key=lambda c: c["ts"], reverse=True)
    return out


def media_list(phone: str, limit: int = 200):
    """כל המדיה מהשיחה (תמונות/וידאו/קבצים), חדש→ישן — לגלריה בפאנל הפרטים."""
    msgs = _dash_call(_dash().get_conversation, phone, limit=limit)
    out = []
    for m in msgs:  # הדשבורד מחזיר חדש→ישן — נשארים בסדר הזה
        for item in _extract_media(m.get("content")):
            out.append({**item, "ts": m.get("ts") or 0,
                        "direction": m.get("direction")})
    return out


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
    """הזמנות WooCommerce לפי טלפון. ⚠️ הזמנות מהאתר נשמרות בפורמט מקומי (05X)
    והוואטסאפ בבינלאומי (972X) — לכן מחפשים לפי הליבה בלי קידומת ("546290097"),
    שתופסת את שני הפורמטים (חיפוש WC הוא substring). אומת 12/06/2026 על הזמנה 46883."""
    import os
    import requests as rq
    base = os.getenv("WC_STORE_URL", "").rstrip("/")
    auth = (os.getenv("WC_CONSUMER_KEY", ""), os.getenv("WC_CONSUMER_SECRET", ""))
    if not base or not auth[0]:
        return None  # WC לא מוגדר — הפאנל פשוט לא יציג הזמנות
    core = re.sub(r"^(?:972|0)", "", re.sub(r"\D", "", str(phone)))
    try:
        r = rq.get(f"{base}/wp-json/wc/v3/orders",
                   params={"search": core or str(phone), "per_page": limit},
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
