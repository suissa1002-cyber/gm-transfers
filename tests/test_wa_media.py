#!/usr/bin/env python3
"""Regression test — נועל את נתיב המדיה היוצאת של WhatsApp.

תופס את שתי הרגרסיות שקרו בעבר:
  1. send_media לא שולח דרך Meta הישיר / לא בונה payload נכון (למשל חזרה לקונקטופ).
  2. מדיה יוצאת לא נשמרת → לא מוצגת בשיחה ובפאנל המדיה (הבאג מ-18/06/2026).

רץ בלי לגעת ב-Meta אמיתי (mock) ובלי DB אמיתי (SQLite זמני) — בטוח להרצה בכל מקום.
הרצה: python3 tests/test_wa_media.py    (קוד יציאה 0=עבר, 1=נכשל)
"""
import os
import sys
import base64
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# DB זמני + env דמה ל-Meta — חייב להיקבע *לפני* import של db/config
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["TRANSFERS_DB_PATH"] = _tmp.name
os.environ.setdefault("META_WA_TOKEN", "TEST_TOKEN")
os.environ.setdefault("META_WA_PHONE_ID", "TEST_PHONE_ID")
os.environ.pop("DATABASE_URL", None)  # מבטיח SQLite, לא Postgres

import db          # noqa: E402
import wa          # noqa: E402
import requests    # noqa: E402

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
WAMID = "wamid.TEST_MEDIA_123"
PHONE = "972500000000"

_calls = []


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._p = payload
        self.ok = 200 <= status < 300
        self.text = str(payload)

    def json(self):
        return self._p


def _fake_post(url, *a, **k):
    """מחליף את Meta — מתעד URLs ומחזיר העלאה+שליחה מזויפות."""
    _calls.append(url)
    if "graph.facebook.com" in url and url.endswith("/media"):
        return _Resp(200, {"id": "UPLOAD_TEST_ID"})
    if "graph.facebook.com" in url and url.endswith("/messages"):
        return _Resp(200, {"messages": [{"id": WAMID}]})
    raise AssertionError("send_media פנה ל-URL לא צפוי (לא Meta?): " + url)


def main():
    db.init_db()
    requests.post = _fake_post  # mock Meta — לא נוגעים ברשת

    res = wa.send_media(PHONE, "invoice.png", PNG, "image/png", "כיתוב בדיקה")
    assert res.get("sent") is True, f"send_media לא דיווח sent: {res}"
    assert res.get("message_id") == WAMID, f"message_id שגוי: {res}"

    # ⚓ נשלח דרך Meta הישיר (graph) — שתי קריאות: /media ואז /messages
    assert any(u.endswith("/media") for u in _calls), "לא בוצעה העלאת מדיה ל-Meta"
    assert any(u.endswith("/messages") for u in _calls), "לא בוצעה שליחת הודעה ל-Meta"

    # 1) הבייטים גובו ב-wa_media_blob
    blob = db.wa_media_blob_get(WAMID)
    assert blob and blob[1] == PNG, "בייטים של התמונה היוצאת לא גובו (wa_media_blob)"

    # 2) ההודעה נשמרה עם media_id (אחרת לא תוצג)
    m = db.wa_msg_get(WAMID)
    assert m and m.get("media_id"), f"wa_msg ללא media_id: {m}"

    # 3) get_thread_native מציג את התמונה בשיחה
    msgs = wa.get_thread_native(PHONE).get("messages", [])
    media_msgs = [x for x in msgs if x.get("media")]
    assert media_msgs, "תמונה יוצאת לא מוצגת בשיחה (get_thread_native)"
    url = (media_msgs[0]["media"][0] or {}).get("url", "")
    assert url, "פריט המדיה בשיחה ללא URL"

    # 4) media_list מציג אותה בפאנל המדיה
    assert wa.media_list(PHONE), "תמונה יוצאת לא מופיעה בפאנל המדיה (media_list)"

    # 5) serve_media מגיש את הבייטים הנכונים
    content, _mime = wa.serve_media(WAMID)
    assert content == PNG, "serve_media החזיר בייטים שגויים"

    print("✅ WhatsApp outgoing-media regression test PASSED")


if __name__ == "__main__":
    code = 0
    try:
        main()
    except AssertionError as e:
        print("❌ FAIL:", e)
        code = 1
    except Exception as e:  # noqa: BLE001
        print("❌ ERROR:", repr(e))
        code = 1
    finally:
        try:
            os.unlink(_tmp.name)
        except Exception:  # noqa: BLE001
            pass
    sys.exit(code)
