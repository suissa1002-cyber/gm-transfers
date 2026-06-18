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
        t_sent = int(r.get("t_last_sent") or 0)   # מתי שלחנו (אנחנו/בוט) אחרון — במ"ש
        phone = r.get("ms_id")
        out.append({
            "phone": phone,
            "name": r.get("full_name") or r.get("first_name") or phone,
            "last_msg": (r.get("last_msg") or "")[:120],
            "ts": ts_ms // 1000,
            "archived": archived,
            "live_chat": str(r.get("live_chat", "0")) == "1",
            # ממתין למענה: הלקוח כתב אחרי השליחה האחרונה שלנו (גם ms). מחליף את
            # last_read_page של קונקטופ שהיה כמעט תמיד 0 (לכן הבאדג' לא נדלק).
            "unread": bool(ts_ms and ts_ms > t_sent + 2000),
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


def _window_for(phone: str):
    """מצב חלון 24ש לבדיקת שליחה — מעדיף את החנות שלנו (native), כי במצב cutover
    ההודעות הנכנסות אצלנו ולא בקונקטופ. נפילה לקונקטופ אם אצלנו סגור (זמן מעבר)."""
    try:
        wn = get_thread_native(phone, limit=30).get("window") or {}
    except Exception:  # noqa: BLE001
        wn = {}
    if wn.get("in_window"):
        return wn
    try:
        wc = get_thread(phone, limit=30).get("window") or {}
    except Exception:  # noqa: BLE001
        wc = {}
    # מחזירים את זה עם ההודעה הנכנסת האחרונה (החלון הפתוח ביותר)
    return wc if (wc.get("last_inbound_ts") or 0) >= (wn.get("last_inbound_ts") or 0) else wn


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
    try:
        msgs = _dash_call(_dash().get_conversation, phone, limit=limit)
    except WaError as e:
        # מספר שאינו איש-קשר עדיין (שיחה חדשה) — ConnectOp מחזיר 404. מחזירים
        # שיחה ריקה (לא זורקים) כדי שאפשר יהיה לפתוח שיחה חדשה ולשלוח תבנית.
        if "404" in str(e):
            return {"phone": phone, "messages": [],
                    "window": {"in_window": False, "hours_left": 0, "last_inbound_ts": 0},
                    "entry_product": None}
        raise
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
    # שכבת סטטוס מסירה/קריאה: ה-webhook של מטא שומר sent/delivered/read ב-wa_msg
    # (id ההודעה == wamid). מרכיבים על הודעות יוצאות. כרגע מתמלא רק להודעות
    # שנשלחו דרך Meta ישיר; אחרי cutover — לכל ההודעות.
    try:
        st_map = {r.get("wamid"): r.get("status")
                  for r in db.wa_msg_thread(phone, limit=200) if r.get("status")}
    except Exception:  # noqa: BLE001
        st_map = {}
    if st_map:
        for m in slim:
            if m.get("direction") == "out" and st_map.get(m.get("id")):
                m["status"] = st_map[m["id"]]
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
        wamid_m = m.get("wamid")
        if murl:
            mt = (m.get("media_mime") or "").split("/")[0] or m.get("type") or "file"
            media = [{"type": mt, "url": murl, "caption": ""}]
        elif m.get("media_id") and wamid_m:
            # מדיה נכנסת ממטא (תמונה/מסמך) — מוגשת דרך ה-endpoint שלנו (גיבוי/מטא),
            # עם טוקן חתום כדי ש-<img> יוכל לטעון בלי כותרת אימות.
            mt = (m.get("media_mime") or "").split("/")[0] or m.get("type") or "file"
            media = [{"type": mt, "caption": "",
                      "url": f"/api/wa/media?wamid={wamid_m}&t={media_token(wamid_m)}"}]
        # מנקים marker טכני בתחילת ההודעה ([interactive]/[רשימה]/[כפתורים]) מהבקאפ/בוט
        text = re.sub(r"^\s*\[(interactive|רשימה|כפתורים)\]\s*", "", m.get("text") or "")
        if "_strip_entry_marker" in globals():
            text = _strip_entry_marker(text)
        text = text.strip()
        # סטטוס מסירה: הודעות היסטוריות (מהבקאפ) מסומנות 'historic' ואין להן מידע
        # מסירה מדויק → מציגים אפור ✓✓ (נמסר). הודעות חדשות נושאות סטטוס אמיתי ממטא.
        st = m.get("status") or ""
        if m.get("direction") == "out" and st in ("", "historic"):
            st = "delivered"
        slim.append({
            "id": m.get("wamid"), "direction": m.get("direction"),
            "text": text, "kind": m.get("type"), "tpl": None, "media": media,
            "ts": m.get("ts") or 0,
            "sent_by": "greenos" if m.get("direction") == "out" else None,
            "reply_to": m.get("reply_to") or None, "status": st,
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
    has_pic = phone in db.wa_contact_pic_phones()
    pic = f"/api/admin/wa/contact-pic/{phone}?t={media_token('cpic:' + phone)}" if has_pic else ""
    return {"phone": phone, "messages": slim, "window": _window_state(slim),
            "entry_product": entry, "source": "native", "pic": pic}


def list_conversations_native(limit: int = 300):
    """רשימת שיחות מ-wa_contact (החנות שלנו) — אותו פורמט כמו list_conversations.
    handoff = הבוט מושתק לשיחה (הלקוח ביקש נציג / השתלטות ידנית)."""
    import db
    stars = db.wa_stars()
    handoff = db.bot_handoff_phones()
    pics = db.wa_contact_pic_phones()
    out = []
    for r in db.wa_conversations(limit):
        ph = r.get("phone")
        in_ts = int(r.get("last_in_ts") or 0)
        msg_ts = int(r.get("last_msg_ts") or 0)
        # לא-נקרא = ההודעה האחרונה בשיחה היא מהלקוח (טרם ענינו אחריה)
        unread = bool(in_ts and in_ts >= msg_ts)
        out.append({
            "phone": ph, "name": r.get("name") or ph,
            "last_msg": (r.get("last_msg") or "")[:120],
            "ts": msg_ts,
            "archived": bool(r.get("archived")), "live_chat": bool(r.get("live_chat")),
            "handoff": ph in handoff,
            "unread": unread, "star": ph in stars,
            "pic": (f"/api/admin/wa/contact-pic/{ph}?t={media_token('cpic:' + ph)}" if ph in pics else ""),
        })
    return out


# ── שליחה ────────────────────────────────────────────────────────────

_human_auto_cache: dict = {}


def _auto_human(phone: str):
    """No-op מאז ניתוק קונקטופ (18/06/2026). תפקידו היחיד היה לסמן 'אנושי' בקונקטופ
    כדי להשתיק את הבוט *שלהם*. הבוט שלנו מושתק נייטיב (bot_session='agent') בנתיב
    המענה האנושי (main._bot_handoff_on / _bot_handoff_on). נשאר כ-stub כדי לא לשנות
    את כל הקוראים."""
    return


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
    _auto_human(phone)   # מסמנים אנושי (cache 30ד') — שהבוט לא יקפוץ על ההודעה הבאה
    # ⚡ ביצועים: לא שולפים את כל השיחה מראש לבדיקת חלון 24ש (קריאת רשת איטית בכל
    # שליחה). שולחים ישירות דרך Meta; רק אם נכשל — בודקים חלון: מחוץ ל-24ש →
    # needs_template, אחרת ConnectOp fallback. אנחנו בעלי המספר (Meta ראשי, יציב).
    via = ""
    wamid = ""
    errs = []
    if meta_direct_ready():
        try:
            wamid = _meta_send_text(phone, text)
            via = "text-meta"
        except Exception as e:  # noqa: BLE001
            errs.append(f"Meta: {e}")
            logger.warning("meta text send failed: %s", e)
            try:   # כשל — אולי מחוץ לחלון 24ש. בודקים עכשיו בלבד (מקרה נדיר).
                win = _window_for(phone)
            except Exception:  # noqa: BLE001
                win = None
            if win is not None and not win["in_window"]:
                return {"sent": False, "needs_template": True, "window": win}
    if not via:
        try:
            _pub().send_text_as_human(phone, text)
            via = "text-connectop"
        except Exception as e:  # noqa: BLE001
            errs.append(f"ConnectOp: {e}")
            logger.warning("connectop text send failed: %s", e)
    if not via:
        raise WaError("שליחה נכשלה בכל הערוצים — " + " | ".join(errs)[:300])
    _store_outbound(phone, text, wamid=wamid, mtype="text")
    logger.info("wa send text -> %s via %s (%d chars)", phone, via, len(text))
    return {"sent": True, "via": via}


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


def media_token(wamid: str) -> str:
    """טוקן חתום ל-URL של מדיה — מאפשר ל-<img> לטעון בלי כותרת אימות (שלא ניתן
    לשלוח מתג img), בלי לחשוף את סיסמת הניהול. HMAC על ה-wamid."""
    import hashlib
    import hmac
    import os as _os
    secret = (_os.getenv("META_APP_SECRET") or _os.getenv("ADMIN_PASSWORD") or "gm-media").encode()
    return hmac.new(secret, str(wamid).encode(), hashlib.sha256).hexdigest()[:24]


def backup_media(wamid: str, media_id: str) -> bool:
    """מוריד מדיה נכנסת ממטא ושומר גיבוי קבוע (base64) — כדי שלא יאבד כשמטא ימחק
    אחרי ~30 יום. אידמפוטנטי. מחזיר True אם נשמר."""
    import db
    if not wamid or not media_id:
        return False
    if db.wa_media_blob_get(wamid):     # כבר מגובה
        return True
    try:
        content, mime = fetch_meta_media(media_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("backup_media fetch failed (%s): %s", wamid, e)
        return False
    db.wa_media_blob_set(wamid, mime, content)
    return True


def serve_media(wamid: str):
    """מחזיר (bytes, mime) להצגה — קודם מהגיבוי שלנו, אחרת מוריד ממטא ומגבה תוך כדי."""
    import db
    blob = db.wa_media_blob_get(wamid)
    if blob:
        return blob[1], blob[0]
    m = db.wa_msg_get(wamid)
    mid = (m or {}).get("media_id")
    if not mid:
        raise WaError("אין מדיה")
    content, mime = fetch_meta_media(mid)
    try:                                # cache-on-view → גם מגבה
        db.wa_media_blob_set(wamid, mime, content)
    except Exception:  # noqa: BLE001
        pass
    return content, mime


def send_typing(message_id: str) -> bool:
    """חיווי הקלדה רשמי של מטא (+ סימון 'נקרא') — מוצג ללקוח עד ~25 שניות או עד
    שנשלחת הודעה. message_id = wamid של ההודעה הנכנסת של הלקוח."""
    if not (meta_direct_ready() and message_id):
        return False
    import os as _os
    import requests as _rq
    try:
        r = _rq.post(f"{META_GRAPH}/{_os.getenv('META_WA_PHONE_ID').strip()}/messages",
                     headers={"Authorization": f"Bearer {_os.getenv('META_WA_TOKEN').strip()}",
                              "Content-Type": "application/json"},
                     json={"messaging_product": "whatsapp", "status": "read",
                           "message_id": message_id,
                           "typing_indicator": {"type": "text"}}, timeout=10)
        return r.status_code in (200, 201)
    except Exception:  # noqa: BLE001
        return False


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
                       # preview_url=true — חובה כדי שקישורים בטקסט יהיו לחיצים בוואטסאפ
                       "text": {"body": text, "preview_url": True}}, timeout=30)
    if r.status_code not in (200, 201):
        raise WaError(f"Meta text send failed ({r.status_code}): {r.text[:200]}")
    return ((r.json().get("messages") or [{}])[0]).get("id", "")


def send_document(phone: str, pdf_bytes: bytes, filename: str = "document.pdf",
                  caption: str = "") -> dict:
    """שולח קובץ PDF ללקוח דרך WhatsApp (העלאת מדיה למטא → הודעת document).
    אוכף חלון 24ש: אם השליחה נכשלת ומחוץ לחלון → needs_template."""
    if not meta_direct_ready():
        raise WaError("Meta ישיר לא מוגדר — שליחת מסמך דורשת חיבור Meta")
    if not pdf_bytes:
        raise WaError("קובץ ריק")
    import os as _os
    import requests as _rq
    tok = _os.getenv("META_WA_TOKEN").strip()
    pid = _os.getenv("META_WA_PHONE_ID").strip()
    # 1) העלאת המדיה למטא → media_id
    try:
        up = _rq.post(f"{META_GRAPH}/{pid}/media",
                      headers={"Authorization": f"Bearer {tok}"},
                      data={"messaging_product": "whatsapp"},
                      files={"file": (filename, pdf_bytes, "application/pdf")}, timeout=60)
    except Exception as e:  # noqa: BLE001
        raise WaError(f"העלאת המסמך נכשלה: {e}")
    if up.status_code not in (200, 201):
        raise WaError(f"העלאת מדיה נכשלה ({up.status_code}): {up.text[:200]}")
    media_id = up.json().get("id")
    if not media_id:
        raise WaError("לא התקבל media_id ממטא")
    # 2) שליחת הודעת document
    body = {"messaging_product": "whatsapp", "to": phone, "type": "document",
            "document": {"id": media_id, "filename": filename}}
    if caption:
        body["document"]["caption"] = caption
    r = _rq.post(f"{META_GRAPH}/{pid}/messages",
                 headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                 json=body, timeout=30)
    if r.status_code not in (200, 201):
        # ייתכן מחוץ לחלון 24ש (מטא 131047) — מאותתים needs_template
        try:
            win = _window_for(phone)
        except Exception:  # noqa: BLE001
            win = None
        if win is not None and not win["in_window"]:
            return {"sent": False, "needs_template": True, "window": win}
        raise WaError(f"שליחת המסמך נכשלה ({r.status_code}): {r.text[:200]}")
    wamid = ((r.json().get("messages") or [{}])[0]).get("id", "")
    _store_outbound(phone, caption or f"📄 {filename}", wamid=wamid, mtype="document")
    logger.info("wa send document -> %s (%s)", phone, filename)
    return {"sent": True, "wamid": wamid}


def _meta_interactive(phone: str, payload: dict, preview_text: str) -> str:
    """שולח הודעת interactive (כפתורים/רשימה) דרך Meta. מחזיר wamid, שומר outbound."""
    if not meta_direct_ready():
        raise WaError("Meta ישיר לא מוגדר")
    import os as _os
    import requests as _rq
    r = _rq.post(f"{META_GRAPH}/{_os.getenv('META_WA_PHONE_ID').strip()}/messages",
                 headers={"Authorization": f"Bearer {_os.getenv('META_WA_TOKEN').strip()}",
                          "Content-Type": "application/json"},
                 json={"messaging_product": "whatsapp", "to": phone,
                       "type": "interactive", "interactive": payload}, timeout=30)
    if r.status_code not in (200, 201):
        raise WaError(f"Meta interactive failed ({r.status_code}): {r.text[:200]}")
    wamid = ((r.json().get("messages") or [{}])[0]).get("id", "")
    _store_outbound(phone, preview_text, wamid=wamid, mtype="interactive")
    return wamid


def send_buttons(phone: str, body: str, buttons: list, header: str = "",
                 header_image: str = "") -> str:
    """עד 3 כפתורי תשובה. buttons = [(id, title), ...] (title ≤20 תווים).
    header_image = URL תמונה לכותרת (כרטיס מוצר)."""
    payload = {"type": "button", "body": {"text": body},
               "action": {"buttons": [{"type": "reply", "reply": {"id": bid, "title": t[:20]}}
                                       for bid, t in buttons[:3]]}}
    if header_image:
        payload["header"] = {"type": "image", "image": {"link": header_image}}
    elif header:
        payload["header"] = {"type": "text", "text": header[:60]}
    return _meta_interactive(phone, payload, f"[כפתורים] {body}")


def send_list(phone: str, body: str, rows: list, button_label: str = "בחר",
              header: str = "", section_title: str = "אפשרויות") -> str:
    """רשימת בחירה (עד 10 שורות). rows = [(id, title, desc), ...] (title ≤24)."""
    payload = {"type": "list", "body": {"text": body},
               "action": {"button": button_label[:20], "sections": [{
                   "title": section_title[:24],
                   "rows": [{"id": r[0], "title": r[1][:24],
                             "description": (r[2][:72] if len(r) > 2 and r[2] else "")}
                            for r in rows[:10]]}]}}
    if header:
        payload["header"] = {"type": "text", "text": header[:60]}
    return _meta_interactive(phone, payload, f"[רשימה] {body}")


def send_cta_url(phone: str, body: str, button_text: str, url: str,
                 header: str = "", header_image: str = "") -> str:
    """כפתור CTA שפותח URL בלחיצה (cta_url). button_text ≤20 תווים.
    header_image = URL תמונה לכותרת (כרטיס מוצר)."""
    payload = {"type": "cta_url", "body": {"text": body},
               "action": {"name": "cta_url",
                          "parameters": {"display_text": button_text[:20], "url": url}}}
    if header_image:
        payload["header"] = {"type": "image", "image": {"link": header_image}}
    elif header:
        payload["header"] = {"type": "text", "text": header[:60]}
    return _meta_interactive(phone, payload, f"[כפתור] {body}")


def send_review_template(phone: str, name: str, order_number, status_text: str = "נמסרה") -> str:
    """שולח את template הביקורת (order_delivered_review_request, he) ישירות דרך מטא:
    עדכון סטטוס + כפתורי חוות דעת גוגל/זאפ. {{1}}=שם, {{2}}=מס' הזמנה, {{3}}=סטטוס.
    מחליף את זרימת הביקורת של קונקטופ אחרי ה-cutover."""
    if not meta_direct_ready():
        raise WaError("Meta ישיר לא מוגדר")
    import os as _os
    import requests as _rq
    params = [{"type": "text", "text": str(name or "לקוח/ה יקר/ה")[:60]},
              {"type": "text", "text": str(order_number or "")[:20]},
              {"type": "text", "text": str(status_text or "נמסרה")[:60]}]
    payload = {"messaging_product": "whatsapp", "to": str(phone), "type": "template",
               "template": {"name": "order_delivered_review_request", "language": {"code": "he"},
                            "components": [{"type": "body", "parameters": params}]}}
    r = _rq.post(f"{META_GRAPH}/{_os.getenv('META_WA_PHONE_ID').strip()}/messages",
                 headers={"Authorization": f"Bearer {_os.getenv('META_WA_TOKEN').strip()}",
                          "Content-Type": "application/json"}, json=payload, timeout=30)
    if r.status_code not in (200, 201):
        raise WaError(f"review template failed ({r.status_code}): {r.text[:200]}")
    wamid = ((r.json().get("messages") or [{}])[0]).get("id", "")
    body = (f"שלום {name or 'לקוח/ה יקר/ה'},\n"
            f"הזמנתך מס' {order_number} נמסרה 🎉\n\n"
            f"נשמח מאוד אם תשתף/י אותנו בחוות דעת — זה עוזר לנו המון!\n"
            f"［דירוג בגוגל · המלצה］")
    _store_outbound(phone, body, wamid=wamid, mtype="template")
    return wamid


def send_order_confirm(phone: str, name: str, order_number,
                       status_text: str = "הזמנתך נקלטה ונמצאת בטיפול") -> str:
    """template 'order_update_1' (he) על קבלת הזמנה — ישירות דרך מטא. {{1}}=שם,
    {{2}}=מס' הזמנה, {{3}}=סטטוס. לכפתור 'בדיקת סטטוס הזמנה' (quick-reply, index 0)
    מצרפים **payload דינמי** עם מספר ההזמנה, כך שלחיצה תציג את סטטוס ההזמנה הזו
    אוטומטית, בלי הזנת מספר. מחליף את זרימת קבלת ההזמנה של קונקטופ."""
    if not meta_direct_ready():
        raise WaError("Meta ישיר לא מוגדר")
    import os as _os
    import requests as _rq
    params = [{"type": "text", "text": str(name or "לקוח/ה יקר/ה")[:60]},
              {"type": "text", "text": str(order_number or "")[:20]},
              {"type": "text", "text": str(status_text or "")[:60]}]
    # 3 כפתורי quick-reply (לפי סדר הטמפלייט): סטטוס (payload דינמי) · תפריט · נציג.
    # הטקסט נקבע בטמפלייט; ה-payload נקבע כאן ומגיע ל-webhook כ-reply_id לניתוב הבוט.
    payload = {"messaging_product": "whatsapp", "to": str(phone), "type": "template",
               "template": {"name": "order_update_1", "language": {"code": "he"},
                            "components": [
                                {"type": "body", "parameters": params},
                                {"type": "button", "sub_type": "quick_reply", "index": "0",
                                 "parameters": [{"type": "payload",
                                                 "payload": f"ordstatus:{order_number}"}]},
                                {"type": "button", "sub_type": "quick_reply", "index": "1",
                                 "parameters": [{"type": "payload", "payload": "menu"}]},
                                {"type": "button", "sub_type": "quick_reply", "index": "2",
                                 "parameters": [{"type": "payload", "payload": "agent"}]}]}}
    r = _rq.post(f"{META_GRAPH}/{_os.getenv('META_WA_PHONE_ID').strip()}/messages",
                 headers={"Authorization": f"Bearer {_os.getenv('META_WA_TOKEN').strip()}",
                          "Content-Type": "application/json"}, json=payload, timeout=30)
    if r.status_code not in (200, 201):
        raise WaError(f"order confirm template failed ({r.status_code}): {r.text[:200]}")
    wamid = ((r.json().get("messages") or [{}])[0]).get("id", "")
    # שומרים את המלל שהלקוח באמת רואה (לא placeholder) — כדי שהנציג יראה בקונסולה
    body = (f"שלום {name or 'לקוח/ה יקר/ה'},\n"
            f"עדכון בנוגע להזמנה מס' {order_number}\n\n"
            f"סטטוס הזמנתך: {status_text or 'הזמנתך נקלטה ונמצאת בטיפול'}\n\n"
            f"תודה, גרין מובייל.\n"
            f"［בדיקת סטטוס הזמנה · תפריט · נציג］")
    _store_outbound(phone, body, wamid=wamid, mtype="template")
    return wamid


_STATUS_TPL_DESC = {
    "order_update_distribution": "הזמנתך עברה לשלב הפצה ותימסר במסגרת ימי המשלוח שנבחרו 🚚",
    "order_ready_for_pickup": "הזמנתך מוכנה לאיסוף מהסניף 🏬",
    "messege_tlv_pickup": "הזמנתך בדרך לנקודת המסירה בתל אביב 📍",
}


def send_status_template(phone: str, name: str, order_number, template_name: str) -> str:
    """שולח template עדכון-סטטוס מאושר (2 פרמטרים: {{1}}=שם, {{2}}=מס' הזמנה) ישירות
    דרך מטא — order_update_distribution / order_ready_for_pickup / messege_tlv_pickup.
    מחליף את זרימות עדכון-הסטטוס של קונקטופ."""
    if not meta_direct_ready():
        raise WaError("Meta ישיר לא מוגדר")
    import os as _os
    import requests as _rq
    params = [{"type": "text", "text": str(name or "לקוח/ה יקר/ה")[:60]},
              {"type": "text", "text": str(order_number or "")[:20]}]
    components = [{"type": "body", "parameters": params}]
    if template_name == "order_update_distribution":   # כפתור 'קיבלתי את ההזמנה' → payload דינמי
        components.append({"type": "button", "sub_type": "quick_reply", "index": "0",
                           "parameters": [{"type": "payload",
                                           "payload": f"received:{order_number}"}]})
    payload = {"messaging_product": "whatsapp", "to": str(phone), "type": "template",
               "template": {"name": template_name, "language": {"code": "he"},
                            "components": components}}
    r = _rq.post(f"{META_GRAPH}/{_os.getenv('META_WA_PHONE_ID').strip()}/messages",
                 headers={"Authorization": f"Bearer {_os.getenv('META_WA_TOKEN').strip()}",
                          "Content-Type": "application/json"}, json=payload, timeout=30)
    if r.status_code not in (200, 201):
        raise WaError(f"status template {template_name} failed ({r.status_code}): {r.text[:200]}")
    wamid = ((r.json().get("messages") or [{}])[0]).get("id", "")
    desc = _STATUS_TPL_DESC.get(template_name, "עדכון סטטוס להזמנתך")
    tail = "\n［קיבלתי את ההזמנה］" if template_name == "order_update_distribution" else ""
    body = (f"שלום {name or 'לקוח/ה יקר/ה'},\n"
            f"עדכון בנוגע להזמנה מס' {order_number}\n\n{desc}\n\nתודה, גרין מובייל.{tail}")
    _store_outbound(phone, body, wamid=wamid, mtype="template")
    return wamid


def _render_cancel_body(template_name: str, name: str, p2) -> str:
    """משחזר את גוף התבנית המאושרת (לתצוגה בלבד בחנות שלנו — מטא לא מחזיר את הודעותינו)."""
    nm = (name or "לקוח/ה יקר/ה")
    if template_name == "order_cancelled_stock":
        return (f"הזמנה מס׳ {p2} - המוצר חסר במלאי\n\n"
                f"שלום רב {nm},\n\n"
                "עקב מחסור במלאי המוצר המוזמן בוצע זיכוי בכרטיסכם וההזמנה בוטלה.\n"
                "עמכם הסליחה.\n\nתודה,\nGreen Mobile")
    # cart_recovery (ברירת מחדל)
    return (f"שלום {nm},\n\n עדכון הזמנה\n\n"
            f"ההזמנה שלך עם המוצר *{p2}* בוטלה אוטומטית בשל אי-השלמת התשלום בפרק הזמן הנדרש.\n\n"
            "לבירור או לעזרה בהשלמת ההזמנה ניתן להשיב להודעה זו.\n\nתודה,\nGreen Mobile")


def send_cancel_template(phone: str, name: str, p2, template_name: str) -> str:
    """שולח template ביטול-הזמנה מאושר (2 פרמטרים: {{1}}=שם, {{2}}=מוצר/מס' הזמנה) ישירות
    דרך מטא — cart_recovery (אי-תשלום) / order_cancelled_stock (חוסר מלאי). מחליף את נתיב
    הביטול של קונקטופ (flow 1703671507607 + CF worker). ללא כפתורים/header."""
    if not meta_direct_ready():
        raise WaError("Meta ישיר לא מוגדר")
    if template_name not in ("cart_recovery", "order_cancelled_stock"):
        raise WaError(f"תבנית ביטול לא נתמכת: {template_name}")
    import os as _os
    import requests as _rq
    params = [{"type": "text", "text": str(name or "לקוח/ה יקר/ה")[:60]},
              {"type": "text", "text": str(p2 or "")[:200]}]
    payload = {"messaging_product": "whatsapp", "to": str(phone), "type": "template",
               "template": {"name": template_name, "language": {"code": "he"},
                            "components": [{"type": "body", "parameters": params}]}}
    r = _rq.post(f"{META_GRAPH}/{_os.getenv('META_WA_PHONE_ID').strip()}/messages",
                 headers={"Authorization": f"Bearer {_os.getenv('META_WA_TOKEN').strip()}",
                          "Content-Type": "application/json"}, json=payload, timeout=30)
    if r.status_code not in (200, 201):
        raise WaError(f"cancel template {template_name} failed ({r.status_code}): {r.text[:200]}")
    wamid = ((r.json().get("messages") or [{}])[0]).get("id", "")
    _store_outbound(phone, _render_cancel_body(template_name, name, p2), wamid=wamid, mtype="template")
    return wamid


def send_text(phone: str, text: str) -> str:
    """טקסט פשוט מהבוט (לא דרך send_reply שמסמן 'אנושי')."""
    wamid = _meta_send_text(phone, text)
    _store_outbound(phone, text, wamid=wamid, mtype="text")
    return wamid


def _store_outbound(phone: str, text: str, wamid: str = "", mtype: str = "text",
                    media_url: str = "", reply_to: str = "", reply_preview: str = "",
                    media_id: str = "", media_mime: str = "", media_name: str = ""):
    """שומר הודעה יוצאת בחנות העצמאית (wa_msg) — חובה כי webhook של מטא לא מחזיר
    את ההודעות שלנו. גם wa_shadow (לתצוגה ב-get_thread בזמן מעבר).
    reply_to = wamid המצוטט (מענה reply), כדי שהציטוט יופיע גם בתצוגה הנייטיב.
    media_id/media_mime = למדיה יוצאת (הבייטים מגובים ב-wa_media_blob ומוגשים
    דרך /api/wa/media), כך שהתמונה היוצאת תוצג גם בשיחה וגם בפאנל המדיה."""
    import db
    ts = int(time.time())
    wid = wamid or f"gm-out-{ts}-{abs(hash(text)) % 100000}"
    try:
        db.wa_msg_upsert(wamid=wid, phone=str(phone), direction="out", mtype=mtype,
                         text=text or "", media_url=media_url or "", media_id=media_id or "",
                         media_mime=media_mime or "", media_name=media_name or "",
                         reply_to=reply_to or "", ts=ts, status="sent")
        db.wa_contact_upsert(str(phone), out_ts=ts)
    except Exception as e:  # noqa: BLE001
        logger.warning("store outbound failed: %s", e)
    if wamid:
        try:
            db.wa_shadow_add(str(phone), wamid, text or "", reply_to=reply_to,
                             reply_preview=reply_preview, ts=ts)
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
    mid = _meta_send_template(phone, "new_message", [name or "לקוח/ה יקר/ה", body], [])
    _store_outbound(phone, (name + ": " if name else "") + body, wamid=mid, mtype="template")
    logger.info("wa send template new_message (meta) -> %s", phone)
    return {"sent": True, "via": "template", "message_id": mid}


def send_media(phone: str, filename: str, content: bytes, mime: str, caption: str = ""):
    """שליחת קובץ ללקוח **ישירות דרך Meta** (העלאה ל-/media → הודעת media). מחליף את
    נתיב ה-file-manager של קונקטופ. כפוף לחלון 24ש (template-only מחוצה לו)."""
    import os as _os
    import requests as _rq
    if len(content) > 15 * 1024 * 1024:
        raise WaError("קובץ גדול מדי (מקס׳ 15MB)")
    if not meta_direct_ready():
        raise WaError("Meta ישיר לא מוגדר")
    m = (mime or "").lower()
    kind = ("image" if m.startswith("image/") else
            "audio" if m.startswith("audio/") else
            "video" if m.startswith("video/") else "document")
    safe = re.sub(r"[^\w.\-]+", "_", filename or "file", flags=re.UNICODE) or "file"
    tok = _os.getenv("META_WA_TOKEN").strip()
    pid = _os.getenv("META_WA_PHONE_ID").strip()
    # 1) העלאה למטא → media_id
    try:
        up = _rq.post(f"{META_GRAPH}/{pid}/media",
                      headers={"Authorization": f"Bearer {tok}"},
                      data={"messaging_product": "whatsapp"},
                      files={"file": (safe, content, mime or "application/octet-stream")},
                      timeout=90)
    except Exception as e:  # noqa: BLE001
        raise WaError(f"העלאת מדיה למטא נכשלה: {e}") from e
    if up.status_code not in (200, 201):
        raise WaError(f"העלאת מדיה נכשלה ({up.status_code}): {up.text[:200]}")
    media_id = up.json().get("id")
    if not media_id:
        raise WaError("לא התקבל media_id ממטא")
    # 2) שליחת הודעת media
    obj = {"id": media_id}
    if caption and kind in ("image", "video", "document"):
        obj["caption"] = caption
    if kind == "document":
        obj["filename"] = safe
    payload = {"messaging_product": "whatsapp", "to": str(phone), "type": kind, kind: obj}
    r = _rq.post(f"{META_GRAPH}/{pid}/messages",
                 headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                 json=payload, timeout=40)
    if r.status_code not in (200, 201):
        try:
            win = _window_for(phone)
        except Exception:  # noqa: BLE001
            win = None
        if win is not None and not win["in_window"]:
            return {"sent": False, "needs_template": True, "window": win}
        raise WaError(f"שליחת המדיה נכשלה ({r.status_code}): {r.text[:200]}")
    wamid = ((r.json().get("messages") or [{}])[0]).get("id", "")
    try:
        import db as _db
        if wamid:                       # גיבוי הבייטים → הצגה בשיחה ובפאנל המדיה
            _db.wa_media_blob_set(wamid, mime or "application/octet-stream", content)
        _store_outbound(phone, caption or "", wamid=wamid, mtype=kind,
                        media_id=wamid or "", media_mime=mime or "", media_name=safe)
    except Exception as e:  # noqa: BLE001
        logger.warning("store outbound media failed: %s", e)
    logger.info("wa send %s (meta) -> %s (%s)", kind, phone, safe)
    return {"sent": True, "via": kind, "message_id": wamid}


# ── פעולות שיחה ─────────────────────────────────────────────────────

# ── תשובות שמורות + תבניות מאושרות מ-ConnectOp ─────────────────────
# התגלית (11/06, מתוך inbox.js של הדשבורד): התשובות חוזרות בשדה `results`
# (לא `data`!): op=inbox_saved_reply/get; תבניות: op=whatsapp/templates/get.

_sr_cache = {"at": 0.0, "items": None}
_tpl_cache = {"at": 0.0, "items": None}


def saved_replies():
    """תשובות מהירות לקונסולה — מקור native סטטי (wa_static.CANNED_REPLIES, נצרב
    מקונקטופ 18/06/2026). אין יותר תלות ב-dashboard."""
    from wa_static import CANNED_REPLIES
    return CANNED_REPLIES


def wa_templates():
    """תבניות WhatsApp מאושרות-מטא (id, שפה, גוף, מספר פרמטרים) — מקור native סטטי
    (wa_static.TEMPLATE_REGISTRY, נצרב מ-Meta 18/06/2026). אין יותר תלות בקונקטופ."""
    from wa_static import TEMPLATE_REGISTRY
    return TEMPLATE_REGISTRY


def send_wa_template(phone: str, template_id: str, params=None, language: str = "he"):
    """שליחת תבנית מאושרת כלשהי (לא רק new_message) — עוקפת את חלון ה-24ש."""
    tpl = next((t for t in wa_templates() if t["id"] == template_id), None)
    if tpl is None:
        raise WaError(f"תבנית '{template_id}' לא נמצאה")
    params = [str(p) for p in (params or [])]
    if len(params) < tpl["params"]:
        raise WaError(f"התבנית דורשת {tpl['params']} פרמטרים, התקבלו {len(params)}")
    mid = _meta_send_template(phone, template_id, params, [],
                              language=tpl.get("language") or language)
    _store_outbound(phone, _tpl_body_filled(template_id, params), wamid=mid, mtype="template")
    logger.info("wa send template %s (meta) -> %s", template_id, phone)
    return {"sent": True, "via": f"template:{template_id}", "message_id": mid}


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


def _meta_send_template(phone: str, template: str, body_params: list, button_params: list,
                        language: str = "he"):
    """שליחת תבנית ישירות דרך Meta Cloud API — עוקף את ConnectOp (שלא מסוגל
    להעביר כפתורי URL דינמיים). מחזיר message_id."""
    import os
    import requests as rq
    if not meta_direct_ready():
        raise WaError("חיבור Meta ישיר לא מוגדר (META_WA_TOKEN / META_WA_PHONE_ID)")
    components = []
    if body_params:
        components.append({"type": "body",
                           "parameters": [{"type": "text", "text": str(p)[:120]} for p in body_params]})
    if button_params:
        components.append({"type": "button", "sub_type": "url", "index": "0",
                           "parameters": [{"type": "text", "text": str(button_params[0])}]})
    payload = {"messaging_product": "whatsapp", "to": str(phone), "type": "template",
               "template": {"name": template, "language": {"code": language or "he"},
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
    win = _window_for(phone)
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
    # נשמר ב-wa_msg (התצוגה הנייטיב קוראת משם) + wa_shadow — אחרת ה-reply נעלם בריענון
    _store_outbound(phone, text, wamid=mid, mtype="text",
                    reply_to=reply_to, reply_preview=reply_preview)
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
        raise WaError(f"תבנית {PAY_TEMPLATE_ID} לא רשומה")
    params = [name or "לקוח/ה יקר/ה", str(order_number), str(total)]
    mid = _meta_send_template(phone, PAY_TEMPLATE_ID, params, [],
                              language=tpl.get("language") or "he")
    _shadow(phone, mid, _tpl_body_filled(PAY_TEMPLATE_ID, params))
    logger.info("wa send pay-template (meta) -> %s (order %s)", phone, order_number)
    return {"sent": True, "via": f"template:{PAY_TEMPLATE_ID}", "message_id": mid}


def search_conversations(q: str, limit: int = 50):
    """חיפוש native בכל אנשי הקשר (שם/טלפון) מהמאגר שלנו — מחליף את חיפוש קונקטופ."""
    q = (q or "").strip()
    if not q:
        return []
    import db
    stars = db.wa_stars()
    handoff = db.bot_handoff_phones()
    out = []
    for r in db.wa_search(q, limit):
        phone = r.get("phone")
        in_ts = int(r.get("last_in_ts") or 0)
        msg_ts = int(r.get("last_msg_ts") or 0)
        out.append({
            "phone": phone,
            "name": r.get("name") or phone,
            "last_msg": (r.get("last_msg") or "")[:120],
            "ts": msg_ts,
            "archived": bool(r.get("archived")),
            "live_chat": bool(r.get("live_chat")),
            "handoff": phone in handoff,
            "unread": bool(in_ts and in_ts >= msg_ts),
            "star": phone in stars,
            "pic": "",
        })
    return out


def media_list(phone: str, limit: int = 200):
    """כל המדיה מהשיחה (תמונות/וידאו/קבצים), חדש→ישן — מהמאגר native (wa_msg)."""
    import db
    out = []
    for m in db.wa_msg_thread(phone, limit=limit):
        murl = m.get("media_url")
        wamid_m = m.get("wamid")
        mt = (m.get("media_mime") or "").split("/")[0] or m.get("type") or "file"
        if murl:
            url = murl
        elif m.get("media_id") and wamid_m:
            url = f"/api/wa/media?wamid={wamid_m}&t={media_token(wamid_m)}"
        else:
            continue
        out.append({"type": mt, "url": url, "caption": "",
                    "ts": m.get("ts") or 0, "direction": m.get("direction")})
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out


# ── כרטיס פונה: ConnectOp + הזמנות אתר + מטא שלנו ──────────────────

_tags_cache = {"at": 0.0, "tags": None}


def account_tags():
    """רשימת התגים של החשבון (CRM של קונקטופ), cache 10 דקות. גרייסלי: אם קונקטופ
    לא זמין (אחרי ניתוק) — מחזיר רשימה ריקה במקום לקרוס."""
    now = time.time()
    if _tags_cache["tags"] is None or now - _tags_cache["at"] > 600:
        try:
            _tags_cache["tags"] = _pub().get_tags()
        except Exception as e:  # noqa: BLE001
            logger.warning("account_tags unavailable (connectop?): %s", e)
            _tags_cache["tags"] = []
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
    """תיוג CRM של קונקטופ. גרייסלי: אם קונקטופ לא זמין — מחזיר skipped במקום לקרוס."""
    try:
        if add:
            _pub().add_tag(phone, tag_id)
        else:
            _pub().remove_tag(phone, tag_id)
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        logger.warning("set_tag unavailable (connectop?): %s", e)
        return {"ok": False, "skipped": "connectop_unavailable"}


def archive(phone: str, archived: bool = True):
    """ארכוב native (דגל ב-DB — מה שה-inbox קורא). ללא קונקטופ."""
    import db
    db.wa_set_archived(phone, archived)
    return {"ok": True, "phone": phone, "archived": archived}


def _archive_legacy(phone: str, archived: bool = True):
    _dash_call(_dash().archive_conversation, phone, archive=archived)
    with _lock:
        _inbox_cache["rows"] = None  # שהשינוי ייראה מיד
    return {"ok": True}


def set_human(phone: str, enable: bool = True):
    """מתג אנושי/בוט native: enable=True → משתיק את הבוט שלנו לשיחה (bot_session='agent');
    enable=False → מחזיר לבוט. מחליף את set_human_mode של קונקטופ."""
    import db
    if enable:
        db.bot_session_set(str(phone), "agent", {"note": "מצב אנושי (ידני)"})
    else:
        db.bot_session_set(str(phone), "bot", {})
    with _lock:
        _inbox_cache["rows"] = None
    return {"ok": True}
