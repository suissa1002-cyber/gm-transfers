"""בדיקות לסיגנלים שהחליפו את IPQS בטריאז' האימות.

⚠️ הרקע (אסי, 18/08/2026): הקרדיט ב-IPQualityScore נגמר ולא מתחדש —
"insufficient credits" אומת חי. איתו נפלו זיהוי מייל חד-פעמי, גיל מייל,
ו-VoIP בטלפון. שכבת הרשת (ip-api), גרף הכרטיסים והלקוח החוזר נשארו חיים.

⛔ הבדיקות בוחנות **החלטות** — מה מסומן ומה לא. לא את קוד המקור.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402


def run():
    passed, failed = 0, []

    def check(name, cond):
        nonlocal passed
        if cond:
            passed += 1
        else:
            failed.append(name)

    kind = main._il_phone_kind

    # 1. ⛔ הליבה: 07X = וירטואלי. זה מה שהחליף את דגל ה-VoIP של IPQS.
    for ph in ("0721234567", "0731234567", "0741234567",
               "0761234567", "0771234567", "0781234567"):
        check(f"07X וירטואלי: {ph}", kind(ph) == "virtual")

    # 2. סלולרי אמיתי — 98% מהלקוחות שלנו. ⛔ סימון שגוי כאן חוסם לקוח אמיתי.
    for ph in ("0501234567", "0521234567", "0531234567", "0541234567",
               "0551234567", "0581234567", "+972541112223", "972501234567"):
        check(f"⛔ סלולרי לא מסומן: {ph}", kind(ph) == "mobile")

    # 3. קווי מזוהה בנפרד — לא הונאה, רק לא שכיח בקוד דיגיטלי.
    for ph in ("031234567", "0412345678", "086543210", "091234567"):
        check(f"קווי: {ph}", kind(ph) == "landline")

    # 4. קלט חסר/שבור לא מפיל ולא מסמן.
    for ph in ("", None, "123", "abc", "05"):
        check(f"קלט לא תקין → unknown: {ph!r}", kind(ph) == "unknown")

    # 5. מייל חד-פעמי — הרשימה המקומית לבדה, בלי רשת.
    net = main._disposable_email
    real_get = None
    try:
        import requests
        real_get = requests.get
        requests.get = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no net"))
        check("⛔ מקומי עובד בלי רשת", net("x@mailinator.com") is True)
        check("⛔ נכשל-פתוח: תקלת רשת ≠ חד-פעמי", net("x@some-unknown-domain-xyz.com") is False)
        check("דומיין תקין לא מסומן", net("x@gmail.com") is False)
        check("קלט ריק", net("") is False)
        check("בלי @ ", net("notanemail") is False)
    finally:
        if real_get:
            requests.get = real_get

    # 6. הרשימה המקומית התרחבה משמעותית — 24 היו מעט מדי.
    check("⛔ רשימה מורחבת", len(main._DISPOSABLE_DOMAINS) >= 50)
    check("כוללת את הוותיקים", "mailinator.com" in main._DISPOSABLE_DOMAINS)

    # 7. לגיטימייזר הקופה נכשל-פתוח — לעולם לא מעלה סיכון.
    import poller
    orig = poller.client
    poller.client = lambda: (_ for _ in ()).throw(RuntimeError("neworder down"))
    try:
        check("⛔ קופה נפולה → False (לא מוריד ולא מעלה)",
              main._pos_known_customer("0501234567") is False)
    finally:
        poller.client = orig

    # 8. proxycheck — מבחין בין ענן עסקי ל-VPN/TOR. זה מה שמונע צביעת
    # לקוח שגלש דרך ענן לגיטימי כאדום.
    import requests
    real = requests.get

    class R:
        def __init__(self, j): self._j = j
        status_code = 200
        def json(self): return self._j

    def fake(url, params=None, timeout=None, **kw):
        if 'proxycheck' in url:
            ip = url.rsplit('/', 1)[-1]
            data = {'8.8.8.8': {'proxy': 'no', 'type': 'Business', 'risk': 0},
                    '45.83.91.1': {'proxy': 'yes', 'type': 'VPN', 'risk': 73,
                                   'operator': {'name': 'NordVPN'}},
                    '185.220.101.1': {'proxy': 'yes', 'type': 'TOR', 'risk': 100}}
            return R({'status': 'ok', ip: data.get(ip, {})})
        if 'dns.google' in url:
            dom = (params or {}).get('name', '')
            return R({'Answer': [{'data': '10 mx'}]} if dom != 'nomx-zz.com' else {})
        raise RuntimeError('unexpected: ' + url)

    requests.get = fake
    try:
        for k in ('pxc:8.8.8.8', 'pxc:45.83.91.1', 'pxc:185.220.101.1',
                  'mx:gmail.com', 'mx:nomx-zz.com'):
            try: main.db.sales_state_set(k, '')
            except Exception: pass
        biz = main._proxycheck('8.8.8.8')
        vpn = main._proxycheck('45.83.91.1')
        tor = main._proxycheck('185.220.101.1')
        check("⛔ ענן עסקי אינו פרוקסי", biz.get('proxy') is False and biz.get('risk') == 0)
        check("VPN מזוהה עם מפעיל", vpn.get('type') == 'VPN' and vpn.get('operator') == 'NordVPN')
        check("TOR מזוהה בנפרד", tor.get('type') == 'TOR' and tor.get('risk') == 100)
        check("MX תקין", main._email_has_mx('a@gmail.com') is True)
        check("⛔ בלי MX הקוד לא יגיע", main._email_has_mx('a@nomx-zz.com') is False)
    finally:
        requests.get = real

    # ⛔ נכשל-פתוח בשני החדשים
    requests.get = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('no net'))
    try:
        check("⛔ proxycheck נופל → {} ", main._proxycheck('9.9.9.9') == {})
        check("⛔ DNS נופל → לא חוסם", main._email_has_mx('a@unseen-dom-zz9.com') is True)
    finally:
        requests.get = real

    print(f"עברו {passed}/{passed + len(failed)}")
    for f in failed:
        print("  ⛔", f)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
