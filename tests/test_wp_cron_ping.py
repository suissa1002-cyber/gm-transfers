"""בדיקות לפינג הקרון של וורדפרס.

⚠️ הרקע (11/08/2026): המשימות המתוזמנות של וורדפרס לא רצו מאז 17/05. הצטברו
כ-112,000 שורות זבל בטבלת ההגדרות, ו-/wp-json/ טיפס ל-8 שניות. ריצת קרון אחת
החזירה אותו ל-0.15 שניות.

⛔ הבדיקות בוחנות התנהגות — מה נשלח ומתי מתריעים. הן לא קוראות את קוד המקור.
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run():
    calls, alerts, state = [], [], {}
    passed, failed = 0, []

    def check(name, cond):
        nonlocal passed
        if cond:
            passed += 1
        else:
            failed.append(name)

    class R:
        def __init__(self, code):
            self.status_code = code

    # מזייפים את requests לפני הייבוא של main כדי לא לגעת ברשת אמיתית.
    fake_rq = types.ModuleType("requests")
    outcome = {"code": 200, "raise": False}

    def _get(url, timeout=None, headers=None):
        calls.append({"url": url, "timeout": timeout, "ua": (headers or {}).get("User-Agent", "")})
        if outcome["raise"]:
            raise ConnectionError("timeout")
        return R(outcome["code"])

    fake_rq.get = _get
    fake_rq.post = lambda *a, **k: R(200)
    fake_rq.HTTPError = Exception
    fake_rq.ConnectionError = ConnectionError
    real_rq = sys.modules.get("requests")
    import main
    # העבודה מייבאת requests בתוך הפונקציה, לכן הזיוף חייב להישאר מותקן
    # לאורך כל הריצות — לא רק בזמן הייבוא של main.
    sys.modules["requests"] = fake_rq

    main._tg_admin = lambda msg: alerts.append(msg)
    main.db.sales_state_set = lambda k, v: state.__setitem__(k, v)
    orig_env = dict(os.environ)
    os.environ["WC_STORE_URL"] = "https://example.test"
    os.environ.pop("WP_CRON_PING", None)
    try:
        # 1. ריצה תקינה — פונים ל-wp-cron.php עם סוכן משתמש של וורדפרס.
        main._WPCRON_FAILS["n"] = 0
        main._wp_cron_job()
        check("נשלחה קריאה אחת", len(calls) == 1)
        check("היעד הוא wp-cron.php", "/wp-cron.php" in calls[0]["url"])
        # ⛔ הלקח מ-12/08: ערך אחרי doing_wp_cron גורם לוורדפרס לצאת מיד.
        check("⛔ doing_wp_cron בלי ערך", calls[0]["url"].endswith("?doing_wp_cron"))
        check("⛔ בלי סימן שווה בכתובת", "doing_wp_cron=" not in calls[0]["url"])
        check("⛔ סוכן משתמש של וורדפרס", calls[0]["ua"].startswith("WordPress/"))
        check("⛔ לא סוכן דפדפני", "Mozilla" not in calls[0]["ua"])
        check("timeout נדיב לתור ארוך", (calls[0]["timeout"] or 0) >= 60)
        check("נרשמה הצלחה", state.get("wp_cron_last_ok"))
        check("בלי התראה מיותרת", alerts == [])

        # 2. כשל בודד — שקט. לא מציפים את הטלגרם על רעש רגעי.
        outcome["raise"] = True
        state.clear()
        main._wp_cron_job()
        check("כשל בודד — בלי התראה", alerts == [])
        check("כשל — לא נרשמה הצלחה", not state.get("wp_cron_last_ok"))

        # 3. שלושה כשלים ברצף — התראה אחת בלבד, גם אם ממשיכים להיכשל.
        main._wp_cron_job()
        main._wp_cron_job()
        check("3 כשלים → התראה", len(alerts) == 1)
        main._wp_cron_job()
        main._wp_cron_job()
        check("⛔ בלי הצפה — עדיין התראה אחת", len(alerts) == 1)

        # 4. התאוששות — מודיעים שחזר, ומאפסים את המונה.
        outcome["raise"] = False
        main._wp_cron_job()
        check("התאוששות → הודעה", len(alerts) == 2 and "חזר" in alerts[1])
        check("המונה אופס", main._WPCRON_FAILS["n"] == 0)

        # 5. תשובה שאינה 200 נחשבת כשל — 403 של Cloudflare אינו הצלחה.
        outcome["code"] = 403
        state.clear()
        main._wp_cron_job()
        check("403 נספר ככשל", main._WPCRON_FAILS["n"] == 1)
        check("403 — לא נרשמה הצלחה", not state.get("wp_cron_last_ok"))
        outcome["code"] = 200

        # 6. מתג כיבוי — לא פונים בכלל.
        n = len(calls)
        os.environ["WP_CRON_PING"] = "0"
        main._wp_cron_job()
        check("מתג כיבוי עוצר את הפנייה", len(calls) == n)
    finally:
        os.environ.clear()
        os.environ.update(orig_env)
        if real_rq is not None:
            sys.modules["requests"] = real_rq
        else:
            sys.modules.pop("requests", None)

    print(f"עברו {passed}/{passed + len(failed)}")
    for f in failed:
        print("  ⛔", f)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
