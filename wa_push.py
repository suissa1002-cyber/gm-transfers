"""
wa_push — Web Push (PWA) להתראות וואטסאפ כשהאפליקציה סגורה.

iOS תומך ב-Web Push מ-16.4 אבל **רק כש-GreenOS מותקן על מסך הבית**
(שתף ← הוסף למסך הבית). הדפדפן נרשם דרך ה-service worker (static/sw.js)
ושולח את ה-subscription ל-`POST /api/admin/wa/push/subscribe`.

זיהוי הודעות נכנסות: ג'וב שרת כל 60ש משווה את חותמת הזמן של כל שיחה
למצב השמור (kv `wa_push_state`); שיחה שהתעדכנה → בודקים את ההודעה
האחרונה בפועל — רק `direction == "in"` (לקוח) מייצר push, כדי שתשובות
בוט/נציג לא יציפו. שירות Render Free ישן מחוץ לשעות הפעילות (ה-keepalive
מגביל לשעות החנות) — בזמן שינה אין push; את הלילה מכסה הבוט בטלגרם.
"""
import json
import logging
import os
import time

import config as cfg
import db

logger = logging.getLogger("wa_push")

VAPID_PRIVATE = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_SUB = os.getenv("VAPID_SUB", "mailto:greenmobilechat@gmail.com")

_STATE_KEY = "wa_push_state"


def subscribe(sub: dict, ua: str = ""):
    endpoint = (sub or {}).get("endpoint", "")
    if not endpoint:
        raise ValueError("subscription missing endpoint")
    db.wa_push_sub_add(endpoint, json.dumps(sub), ua[:300])
    return {"ok": True, "devices": len(db.wa_push_subs())}


def send_to_all(title: str, body: str, url: str = "/?wa=1", phone: str = "",
                badge: int = 0, kind: str = "wa", tag: str = "") -> int:
    """שולח push לכל המכשירים הרשומים; מוחק מנויים מתים (404/410).
    kind: 'wa' (וואטסאפ) או 'order' (הזמנה חדשה) — קובע יעד פתיחה ו-tag בצד הלקוח."""
    if not VAPID_PRIVATE:
        logger.warning("VAPID keys not configured — skipping push")
        return 0
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        logger.error("pywebpush not installed")
        return 0
    payload = json.dumps({"title": title, "body": body, "url": url, "phone": phone,
                          "badge": int(badge or 0), "kind": kind, "tag": tag},
                         ensure_ascii=False)
    sent = 0
    for s in db.wa_push_subs():
        try:
            webpush(
                subscription_info=json.loads(s["sub"]),
                data=payload,
                vapid_private_key=VAPID_PRIVATE,
                vapid_claims={"sub": VAPID_SUB},
                ttl=3600,
            )
            sent += 1
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):
                db.wa_push_sub_delete(s["endpoint"])
                logger.info("push sub expired — removed (%s...)", s["endpoint"][:40])
            else:
                logger.warning("push failed (%s): %s", code, e)
        except Exception as e:  # noqa: BLE001
            logger.warning("push error: %s", e)
    return sent


def poll_and_push():
    """ג'וב מתוזמן: מזהה הודעות נכנסות חדשות ושולח push."""
    if not db.wa_push_subs():
        return  # אין מכשירים רשומים — לא שורפים קריאות
    import wa
    try:
        convs = wa.list_conversations(include_archived=False)
    except Exception as e:  # noqa: BLE001
        logger.warning("poll: inbox failed: %s", e)
        return
    raw = db.sales_state_get(_STATE_KEY)
    state = json.loads(raw) if raw else None
    new_state = {c["phone"]: c["ts"] for c in convs}
    if state is None:
        # ריצה ראשונה — בסיס בלבד, בלי התראות רטרואקטיביות
        db.sales_state_set(_STATE_KEY, json.dumps(new_state))
        return
    for c in convs:
        prev = state.get(c["phone"], 0)
        if c["ts"] <= prev:
            continue
        # השיחה התעדכנה — האם ההודעה האחרונה נכנסת (מהלקוח)?
        try:
            msgs = wa.get_thread(c["phone"], limit=5)["messages"]
        except Exception:  # noqa: BLE001
            continue
        fresh_in = [m for m in msgs if m.get("direction") == "in"
                    and (m.get("ts") or 0) > prev]
        if not fresh_in:
            continue
        last = fresh_in[-1]
        body = (last.get("text") or "").strip() or "📷 מדיה"
        unread = sum(1 for x in convs if x.get("unread"))   # למונה על אייקון האפליקציה
        n = send_to_all(f"💬 {c['name']}", body[:140],
                        url=f"/?wa=1&phone={c['phone']}", phone=str(c["phone"]),
                        badge=max(1, unread))
        logger.info("wa push: %s -> %d devices", c["phone"], n)
        time.sleep(0.3)
    db.sales_state_set(_STATE_KEY, json.dumps(new_state))


# ── 🎉 push על הזמנות חדשות מהאתר (כשהאפליקציה סגורה) ──
_ORDERS_STATE_KEY = "orders_push_state"


def _wc_orders_latest():
    import requests
    base = os.getenv("WC_STORE_URL", "").rstrip("/")
    k = os.getenv("WC_CONSUMER_KEY", "")
    s = os.getenv("WC_CONSUMER_SECRET", "")
    if not (base and k and s):
        return []
    try:
        r = requests.get(f"{base}/wp-json/wc/v3/orders",
                         params={"per_page": 10, "orderby": "date", "order": "desc",
                                 "status": ["processing", "pending", "on-hold"]},
                         auth=(k, s), timeout=30)
        return r.json() if r.ok else []
    except Exception as e:  # noqa: BLE001
        logger.warning("orders push: fetch failed: %s", e)
        return []


def poll_and_push_orders():
    """ג'וב מתוזמן: מזהה הזמנות אתר חדשות ושולח push '🎉 הזמנה חדשה'."""
    if not db.wa_push_subs():
        return  # אין מכשירים רשומים — לא שורפים קריאות
    orders = _wc_orders_latest()
    if not orders:
        return
    ids = [str(o.get("id")) for o in orders]
    raw = db.sales_state_get(_ORDERS_STATE_KEY)
    seen = set(json.loads(raw)) if raw else None
    if seen is None:
        # ריצה ראשונה — בסיס בלבד, בלי התראות רטרואקטיביות
        db.sales_state_set(_ORDERS_STATE_KEY, json.dumps(ids))
        return
    fresh = [o for o in orders if str(o.get("id")) not in seen]
    # מצב מעודכן: איחוד מזהים, מוגבל ל-200 האחרונים
    db.sales_state_set(_ORDERS_STATE_KEY,
                       json.dumps(list(dict.fromkeys(ids + list(seen)))[:200]))
    if not fresh:
        return
    for o in reversed(fresh):                       # ישן → חדש
        items = o.get("line_items") or []
        nm = (items[0].get("name") or "הזמנה") if items else "הזמנה"
        n_items = sum(int(li.get("quantity") or 1) for li in items)
        num = o.get("number")
        body = f"#{num} · {nm[:50]}" + (f" ({n_items})" if n_items > 1 else "")
        n = send_to_all("🎉 נכנסה הזמנה חדשה!", body, url="/?orders=1",
                        badge=len(fresh), kind="order", tag=f"order-{num}")
        logger.info("order push: #%s -> %d devices", num, n)
        time.sleep(0.3)
