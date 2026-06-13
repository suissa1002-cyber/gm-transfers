"""
wa_backfill — שאיבה חד-פעמית של כל היסטוריית השיחות מ-ConnectOp/ChatRace
לחנות העצמאית שלנו (db.wa_msg / db.wa_contact).

⚠️ למה צריך: מטא לא נותנת היסטוריה (Cloud API מספק רק מרגע ה-webhook והלאה).
ההיסטוריה יושבת ב-ChatRace, ומזהה ההודעה שם **זהה ל-wamid של מטא**, אז הרצף
מתחבר חלק בלי כפילויות (wa_msg_upsert אידמפוטנטי לפי wamid).

רץ כג'וב רקע (לא חוסם). מצב התקדמות ב-sales_state 'wa_backfill_progress'.
"""
import json
import logging

import db
import wa

logger = logging.getLogger("wa_backfill")

_MEDIA_MIME = {"image": "image/*", "video": "video/*", "audio": "audio/*",
               "document": "application/octet-stream", "sticker": "image/webp"}


def _map(m: dict) -> dict:
    text = m.get("text") or ""
    mtype = "text"
    reply_to = ""
    content = m.get("content")
    if isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "template":
                t2, _ = wa._render_template_block(b)
                text = t2 or text
                mtype = "template"
            c = b.get("context")
            if isinstance(c, dict) and (c.get("id") or c.get("message_id")):
                reply_to = c.get("id") or c.get("message_id")
    media = wa._extract_media(content)
    media_url = media_mime = ""
    if media:
        mtype = media[0].get("type") or "media"
        media_url = media[0].get("url") or ""
        media_mime = _MEDIA_MIME.get(mtype, "")
        if not text:
            text = media[0].get("caption") or ""
    return {"wamid": m.get("id") or "", "direction": m.get("direction") or "in",
            "mtype": mtype, "text": text, "media_url": media_url, "media_mime": media_mime,
            "reply_to": reply_to, "ts": int(m.get("ts") or 0)}


def _set_progress(d):
    db.sales_state_set("wa_backfill_progress", json.dumps(d, ensure_ascii=False))


def progress() -> dict:
    raw = db.sales_state_get("wa_backfill_progress")
    return json.loads(raw) if raw else {"running": False, "done": 0, "convs": 0, "msgs": 0}


def _list_all_conversations(batch: int = 100, max_total: int = 20000) -> list:
    """מונה את כל השיחות דרך pagination ישיר מול הדשבורד (offset), בלי cache ובלי
    limit ענק (שהדשבורד דוחה). מסנן ל-WhatsApp (channel 5). [{phone,name,archived}]."""
    out, seen, off = [], set(), 0
    while len(out) < max_total:
        try:
            resp = wa._dash_call(wa._dash()._post_user_php,
                                 {"op": "conversations", "op1": "get", "offset": off, "limit": batch})
        except Exception as e:  # noqa: BLE001
            logger.warning("backfill: conv page off=%d failed: %s", off, e)
            break
        rows = resp.get("data", []) if isinstance(resp, dict) else []
        if not rows:
            break
        for r in rows:
            if str(r.get("channel")) != "5":
                continue
            ph = r.get("ms_id")
            if not ph or ph in seen:
                continue
            seen.add(ph)
            out.append({"phone": ph,
                        "name": r.get("full_name") or r.get("first_name") or ph,
                        "archived": str(r.get("archived", "0")) == "1"})
        if len(rows) < batch:
            break
        off += batch
    return out


def run(per_conv_max: int = 1500) -> dict:
    """שואב את כל השיחות (כולל מארכיון) וכל ההודעות שלהן לחנות. אידמפוטנטי."""
    convs = _list_all_conversations()
    total_c = len(convs)
    done_c = total_m = 0
    _set_progress({"running": True, "convs": total_c, "done": 0, "msgs": 0})
    logger.info("backfill start: %d conversations", total_c)
    for c in convs:
        phone = c.get("phone")
        if not phone:
            continue
        try:
            msgs = wa._dash_call(wa._dash().get_conversation_full, phone,
                                 max_messages=per_conv_max, batch_size=50, sleep_between=0.4)
        except Exception as e:  # noqa: BLE001
            logger.warning("backfill conv %s failed: %s", phone, e)
            msgs = []
        last_in = last_msg = 0
        for m in msgs:
            mm = _map(m)
            if not mm["wamid"]:
                continue
            try:
                if db.wa_msg_upsert(wamid=mm["wamid"], phone=str(phone), direction=mm["direction"],
                                    mtype=mm["mtype"], text=mm["text"], media_url=mm["media_url"],
                                    media_mime=mm["media_mime"], reply_to=mm["reply_to"],
                                    ts=mm["ts"], status="historic"):
                    total_m += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("backfill store failed (%s): %s", mm["wamid"], e)
                continue
            if mm["direction"] == "in":
                last_in = max(last_in, mm["ts"])
            last_msg = max(last_msg, mm["ts"])
        try:
            db.wa_contact_upsert(str(phone), name=c.get("name"), wa_id=str(phone),
                                 in_ts=last_in, out_ts=last_msg)
        except Exception:  # noqa: BLE001
            pass
        done_c += 1
        if done_c % 10 == 0:
            _set_progress({"running": True, "convs": total_c, "done": done_c, "msgs": total_m})
            logger.info("backfill: %d/%d convs, %d msgs", done_c, total_c, total_m)
    _set_progress({"running": False, "convs": total_c, "done": done_c, "msgs": total_m})
    logger.info("backfill done: %d convs, %d msgs", done_c, total_m)
    return {"convs": done_c, "msgs": total_m}
