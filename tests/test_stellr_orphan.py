"""בדיקות להנפקה שנתקעה אצל הספק.

⚠️ הרקע (10/08/2026, קבלה 20057462, סניף סטאר): הנפקת סוני ₪300 יצאה, התשובה
לא חזרה, שליחה שנייה חטפה 403 "המזהה כבר קיים" — ונשארנו בלי קוד, בלי חיוב
ידוע ובלי דרך לדעת מה קרה, כשלקוחה עומדת בחנות.

⛔ הבדיקות בוחנות **פלט** של activate/recover, לא את קוד המקור. בדיקה שקוראת
מקור עוברת גם כשההתנהגות נשברה — כבר נכווינו מזה.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stellr  # noqa: E402


class FakeResp:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text

    def json(self):
        import json
        return json.loads(self.text) if self.text else None


DUP_403 = '{"error":{"message":"Forbidden","description":"A transaction with the supplied ref already exists.","status":403}}'
CP_503 = '{"error":{"message":"Content partner error","description":"Downstream Content Partner unavailable","status":503}}'
NOT_FOUND = '{"error":{"message":"Not Found","description":"No matching transaction found.","status":404}}'
TX_OK = '{"id":"tx-991","pan":"1234567890123456","pin":"4321","status":"Active"}'


def _patch(handler):
    stellr._request = handler          # noqa: SLF001


def run():
    orig = stellr._request             # noqa: SLF001
    passed, failed = 0, []

    def check(name, cond):
        nonlocal passed
        if cond:
            passed += 1
        else:
            failed.append(name)

    try:
        # 1. 403 "כבר קיים" + הקוד קיים אצל הספק → חילוץ, לא כישלון ולא הנפקה שנייה.
        posts = []

        def h1(method, path, payload=None, timeout=40):
            if method == "POST":
                posts.append(path)
                return FakeResp(403, DUP_403)
            return FakeResp(200, TX_OK)

        _patch(h1)
        r = stellr.activate("729011854472", 300, "pos-x-1")
        check("403 כפול → ok", r.get("ok") is True)
        check("403 כפול → מחזיר PAN", r.get("pan") == "1234567890123456")
        check("403 כפול → מסומן כחילוץ", r.get("recovered") is True)
        check("403 כפול → הנפקה אחת בלבד", len(posts) == 1)

        # 2. 403 "כבר קיים" + הספק לא מגיע ל-CP → כישלון עם דגל תקוע.
        def h2(method, path, payload=None, timeout=40):
            return FakeResp(403, DUP_403) if method == "POST" else FakeResp(500, CP_503)

        _patch(h2)
        r = stellr.activate("729011854472", 300, "pos-x-2")
        check("403 + CP נפול → לא ok", r.get("ok") is False)
        check("403 + CP נפול → orphan", r.get("orphan") is True)
        check("403 + CP נפול → בלי PAN", not r.get("pan"))

        # 3. ניתוק בשליחה, ובדיעבד הקוד הונפק → חילוץ במקום דיווח כישלון.
        def h3(method, path, payload=None, timeout=40):
            if method == "POST":
                raise ConnectionError("read timeout")
            return FakeResp(200, TX_OK)

        _patch(h3)
        r = stellr.activate("729011854472", 300, "pos-x-3")
        check("ניתוק + קוד קיים → ok", r.get("ok") is True)
        check("ניתוק + קוד קיים → PIN", r.get("pin") == "4321")

        # 4. ניתוק והספק אומר במפורש 404 → כישלון נקי, בטוח להנפיק מחדש.
        def h4(method, path, payload=None, timeout=40):
            if method == "POST":
                raise ConnectionError("read timeout")
            return FakeResp(404, NOT_FOUND)

        _patch(h4)
        r = stellr.activate("729011854472", 300, "pos-x-4")
        check("ניתוק + 404 → לא ok", r.get("ok") is False)
        check("ניתוק + 404 → לא תקוע", r.get("orphan") is False)

        # 5. ⛔ הקריטי: ניתוק והספק לא עונה → אסור להסיק "לא הונפק".
        def h5(method, path, payload=None, timeout=40):
            if method == "POST":
                raise ConnectionError("read timeout")
            return FakeResp(500, CP_503)

        _patch(h5)
        r = stellr.activate("729011854472", 300, "pos-x-5")
        check("ניתוק + ספק אילם → תקוע", r.get("orphan") is True)

        # 6. recover מבחין בין "אין עסקה" לבין "לא ידוע" — עליו נשענת המחיקה.
        _patch(lambda m, p, payload=None, timeout=40: FakeResp(404, NOT_FOUND))
        check("recover 404 → exists=False", stellr.recover("r1").get("exists") is False)
        _patch(lambda m, p, payload=None, timeout=40: FakeResp(500, CP_503))
        check("recover 503 → exists=None", stellr.recover("r2").get("exists") is None)
        _patch(lambda m, p, payload=None, timeout=40: FakeResp(200, TX_OK))
        check("recover 200 → exists=True", stellr.recover("r3").get("exists") is True)

        # 7. recover לעולם לא שולח POST — זו הערובה שהחילוץ לא מנפיק.
        methods = []

        def h7(method, path, payload=None, timeout=40):
            methods.append(method)
            return FakeResp(404, NOT_FOUND)

        _patch(h7)
        stellr.recover("r4")
        check("recover — GET בלבד", methods == ["GET"])
    finally:
        stellr._request = orig         # noqa: SLF001

    print(f"עברו {passed}/{passed + len(failed)}")
    for f in failed:
        print("  ⛔", f)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
