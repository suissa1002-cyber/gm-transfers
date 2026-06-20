"""
wa_webhook — קבלת webhook ישיר מ-WhatsApp Cloud API של מטא.

פרויקט ניתוק קונקטופ, שלב 1 (קבלה). מאמת חתימה (X-Hub-Signature-256),
מפענח הודעות/סטטוסים/אנשי-קשר, ושומר בחנות שלנו (db.wa_msg / db.wa_contact).
בזמן מעבר מעביר עותק של ה-payload לקונקטופ כך ששום דבר לא נשבר עד ה-cutover.

רדום בפועל עד שמפנים את override_callback_uri אלינו ב-Meta (שלב 5).
env: META_APP_SECRET (אימות חתימה), META_WEBHOOK_VERIFY_TOKEN (אימות GET),
     WA_FORWARD_CONNECTOP=1 (העברה בזמן מעבר), WA_CONNECTOP_FORWARD_URL.
"""
import hashlib
import hmac
import json
import logging
import os

import requests

import db

logger = logging.getLogger("wa_webhook")

CONNECTOP_FORWARD = os.getenv("WA_CONNECTOP_FORWARD_URL",
                              "https://newapp.connectop.co.il/php/whatsapp.php")


def _forward_enabled() -> bool:
    return os.getenv("WA_FORWARD_CONNECTOP", "1") == "1"


def verify_signature(raw: bytes, sig_header: str) -> bool:
    """מאמת X-Hub-Signature-256 מול META_APP_SECRET. אם הסוד לא הוגדר — לא חוסם
    (שלב בנייה), רק מזהיר. ב-cutover חובה להגדיר."""
    secret = os.getenv("META_APP_SECRET", "").strip()
    if not secret:
        logger.warning("META_APP_SECRET not set — skipping signature check (build phase)")
        return True
    if not sig_header or not sig_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig_header)


def _msg_text(m: dict) -> str:
    t = m.get("type")
    if t == "text":
        return (m.get("text") or {}).get("body", "")
    if t in ("image", "video", "document", "audio", "sticker"):
        return (m.get(t) or {}).get("caption", "")
    if t == "interactive":
        it = m.get("interactive") or {}
        br = it.get("button_reply") or it.get("list_reply") or {}
        return br.get("title", "")
    if t == "button":
        return (m.get("button") or {}).get("text", "")
    if t == "location":
        loc = m.get("location") or {}
        return loc.get("name") or f"{loc.get('latitude')},{loc.get('longitude')}"
    if t == "reaction":
        return (m.get("reaction") or {}).get("emoji", "")
    if t == "order":
        return "[הזמנה מקטלוג]"
    return ""


def _msg_reply_id(m: dict) -> str:
    """ה-ID של בחירת הכפתור/רשימה — לניתוב הבוט. interactive → button/list reply id;
    button (כפתור quick-reply של **טמפלייט**) → ה-payload הדינמי (למשל ordstatus:46818)."""
    if m.get("type") == "interactive":
        it = m.get("interactive") or {}
        br = it.get("button_reply") or it.get("list_reply") or {}
        return br.get("id", "")
    if m.get("type") == "button":
        return (m.get("button") or {}).get("payload", "")
    return ""


def extract_inbound(raw: bytes):
    """מחזיר (phone, text, type, reply_id, wamid) של הודעת הלקוח הראשונה ב-payload,
    או None (אירוע סטטוס / לא הודעה / שגיאה). reply_id = id של כפתור/שורת-רשימה;
    wamid = מזהה ההודעה הנכנסת (לחיווי הקלדה / סימון נקרא)."""
    import json as _json
    try:
        data = _json.loads(raw.decode("utf-8", "ignore") or "{}")
        for entry in data.get("entry", []):
            for ch in entry.get("changes", []):
                for m in (ch.get("value", {}) or {}).get("messages", []):
                    return (m.get("from"), _msg_text(m), m.get("type"),
                            _msg_reply_id(m), m.get("id"))
    except Exception:  # noqa: BLE001
        return None
    return None


def extract_sender(raw: bytes):
    r = extract_inbound(raw)
    return r[0] if r else None


def _media_fields(m: dict):
    t = m.get("type")
    if t in ("image", "video", "document", "audio", "sticker"):
        md = m.get(t) or {}
        return md.get("id", ""), md.get("mime_type", ""), md.get("filename", "")
    return "", "", ""


def process(raw: bytes) -> dict:
    """מפענח payload של מטא ושומר בחנות. מחזיר סיכום."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return {"ok": False, "err": "bad json"}
    n_msg = n_stat = 0
    for entry in data.get("entry", []):
        for ch in entry.get("changes", []):
            v = ch.get("value", {}) or {}
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name")
                     for c in (v.get("contacts") or [])}
            for m in (v.get("messages") or []):
                phone = str(m.get("from") or "")
                wamid = m.get("id") or ""
                ts = int(m.get("timestamp") or 0)
                mtype = m.get("type") or "unknown"
                mid, mime, fname = _media_fields(m)
                reply_to = (m.get("context") or {}).get("id") or ""
                try:
                    db.wa_msg_upsert(wamid=wamid, phone=phone, direction="in", mtype=mtype,
                                     text=_msg_text(m), media_id=mid, media_mime=mime,
                                     media_name=fname, reply_to=reply_to, ts=ts,
                                     raw=json.dumps(m, ensure_ascii=False))
                    db.wa_contact_upsert(phone, name=names.get(phone), wa_id=phone, in_ts=ts)
                    n_msg += 1
                except Exception as e:  # noqa: BLE001
                    logger.warning("store inbound failed (%s): %s", wamid, e)
            for s in (v.get("statuses") or []):
                try:
                    st = s.get("status") or ""
                    err = ""
                    if st == "failed":
                        e0 = (s.get("errors") or [{}])[0]
                        details = (e0.get("error_data") or {}).get("details") or ""
                        err = (f"{e0.get('code')} · {e0.get('title') or e0.get('message') or ''}"
                               + (f" · {details}" if details else "")).strip()
                        logger.warning("WA DELIVERY FAILED phone=%s wamid=%s → %s",
                                       s.get("recipient_id"), s.get("id"), err)
                    db.wa_msg_set_status(s.get("id") or "", st, err)
                    n_stat += 1
                except Exception:  # noqa: BLE001
                    pass
    if n_msg or n_stat:
        logger.info("wa webhook stored: %d msg, %d status", n_msg, n_stat)
    return {"ok": True, "messages": n_msg, "statuses": n_stat}


def forward_to_connectop(raw: bytes, sig_header: str = ""):
    """מעביר עותק לקונקטופ בזמן מעבר — כך שהם ממשיכים לעבוד עד ה-cutover המלא."""
    if not _forward_enabled():
        return
    try:
        requests.post(CONNECTOP_FORWARD, data=raw,
                      headers={"Content-Type": "application/json",
                               "X-Hub-Signature-256": sig_header or ""}, timeout=8)
    except Exception as e:  # noqa: BLE001
        logger.warning("connectop forward failed: %s", e)
