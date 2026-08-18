"""
GreenOS → NewOrder/Morning POS — סוכן הורדה מהמלאי (RPA, מכונת Windows).

מה הוא עושה בכל סבב:
  1. קורא את דגלי השליטה מ-GreenOS (enabled / dry_run). מושבת → לא עושה כלום.
  2. מושך פעולות הורדה 'pending' מהתור.
  3. לכל פעולה: תופס אותה (claim, אטומי), מצלם מלאי *לפני* (דרך GreenOS),
     מזין לקופה (או dry-run — בלי שמירה), מצלם מלאי *אחרי*, ומדווח done/error.

בטיחות:
  • ברירת מחדל: dry_run. שום דבר לא נשמר בקופה עד שאסי מכבה dry_run ב-GreenOS.
  • אימות: המלאי חייב לרדת בדיוק בכמות שהוזנה, אחרת = error ולא ממשיך.
  • claim אטומי מונע הרצה כפולה. צילום מסך נשמר לכל פעולה/כשל.
  • kill-switch: enabled=false ב-GreenOS עוצר הכל מיידית.

הרצה:  python agent.py
תלויות:  pip install -r requirements.txt
"""
import os
import sys
import time
import io

import requests

# ── עדכון עצמי של הדרייבר ────────────────────────────────────────────
# ⚠️ חייב לרוץ **לפני** ה-import. אחרת מריצים קוד ישן בלי לדעת: ה-CDN של GitHub
# מגיש גרסה מהמטמון, וכבר פעמיים רצה בדיקה שלמה על דרייבר ישן והמסקנות ממנה
# היו שגויות (07/08). ניתן לכבות ב-POS_AGENT_AUTOUPDATE=0.
_REPO = os.environ.get("POS_REPO", "suissa1002-cyber/gm-transfers")
_DRIVER_PATH = "pos_agent/pos_driver.py"
_DRIVER_URL = os.environ.get(
    "POS_DRIVER_URL",
    "https://raw.githubusercontent.com/%s/main/%s" % (_REPO, _DRIVER_PATH))


def _fetch_driver():
    """מוריד את הדרייבר העדכני. מחזיר (טקסט, מקור) או (None, סיבה).

    ⚠️ raw.githubusercontent יושב מאחורי CDN ש**מתעלם מפרמטרים בכתובת**, ולכן
    התעלול של `?t=<זמן>` לא עקף כלום: הסוכן הוריד גרסה ישנה, ראה שהיא זהה
    למקומית, ולא עדכן — בשקט (אסי, 07/08). לכן קודם כל דרך ה-API, שמחזיר את
    התוכן של ה-commit העדכני ולא נשמר במטמון קצה."""
    api = "https://api.github.com/repos/%s/contents/%s?ref=main" % (_REPO, _DRIVER_PATH)
    try:
        r = requests.get(api, timeout=25, headers={
            "Accept": "application/vnd.github.raw",
            "Cache-Control": "no-cache", "Pragma": "no-cache"})
        if r.ok and r.text:
            r.encoding = "utf-8"
            return r.text, "API"
    except Exception as e:                # noqa: BLE001
        print("עדכון דרייבר: API נכשל (%s) — מנסה raw" % e)
    try:
        r = requests.get(_DRIVER_URL, timeout=25,
                         params={"t": str(int(time.time()))},
                         headers={"Cache-Control": "no-cache", "Pragma": "no-cache"})
        if not r.ok:
            return None, "HTTP %s" % r.status_code
        r.encoding = "utf-8"
        return r.text, "raw"
    except Exception as e:                # noqa: BLE001
        return None, str(e)


def _ver_of(text):
    import re
    m = re.search(r'DRIVER_VERSION\s*=\s*"([^"]+)"', text or "")
    return m.group(1) if m else "?"


def _self_update_agent():
    """מעדכן את **הקובץ הזה** מ-GitHub ומפעיל את עצמו מחדש אם השתנה.

    ⚠️ עד 09/08/2026 העדכון העצמי כיסה רק את pos_driver.py, ולכן תיקון באגים
    בסוכן עצמו (כמו ההגנה מפני הזנה כפולה) לא הגיע למכונה עד הפעלה ידנית."""
    if os.environ.get("POS_AGENT_AUTOUPDATE", "1") == "0":
        return
    me = os.path.abspath(__file__)
    api = "https://api.github.com/repos/%s/contents/pos_agent/agent.py?ref=main" % _REPO
    try:
        r = requests.get(api, timeout=25, headers={
            "Accept": "application/vnd.github.raw",
            "Cache-Control": "no-cache", "Pragma": "no-cache"})
        if not (r.ok and r.text):
            return
        r.encoding = "utf-8"
        new_src = r.text
    except Exception as e:                # noqa: BLE001
        print("עדכון סוכן: נכשל (%s) — ממשיכים עם הקובץ המקומי" % e)
        return
    try:
        with open(me, encoding="utf-8") as f:
            if f.read() == new_src:
                return
        compile(new_src, me, "exec")      # ⛔ לא כותבים קוד שבור על עצמנו
    except Exception as e:                # noqa: BLE001
        print("עדכון סוכן: הגרסה שהורדה אינה תקינה (%s) — נשארים על המקומית" % e)
        return
    try:
        import shutil
        shutil.copyfile(me, me + ".bak")
        with open(me, "w", encoding="utf-8") as f:
            f.write(new_src)
    except Exception as e:                # noqa: BLE001
        print("עדכון סוכן: כתיבה נכשלה (%s)" % e)
        return
    print("עדכון סוכן: גרסה חדשה נכתבה — מפעיל מחדש")
    os.execv(sys.executable, [sys.executable] + sys.argv)


def _self_update():
    if os.environ.get("POS_AGENT_AUTOUPDATE", "1") == "0":
        return
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pos_driver.py")
    try:
        new, src = _fetch_driver()
        if new is None:
            print("עדכון דרייבר: %s — ממשיכים עם הקובץ המקומי" % src)
            return
        if "DRIVER_VERSION" not in new or len(new) < 5000:
            print("עדכון דרייבר: התוכן נראה שגוי — ממשיכים עם הקובץ המקומי")
            return
        cur = ""
        if os.path.exists(path):
            cur = open(path, encoding="utf-8").read()
        if new != cur:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(new)
            # ⚠️ מנקים __pycache__: אם ה-.pyc הישן נשאר, Python עלול לטעון אותו
            # והקובץ החדש לא ייכנס לתוקף (נצפה 07/08).
            try:
                import shutil
                shutil.rmtree(os.path.join(os.path.dirname(path), "__pycache__"),
                              ignore_errors=True)
            except Exception:                # noqa: BLE001
                pass
            print("↻ הדרייבר עודכן מ-GitHub (%s): %s ← %s"
                  % (src, _ver_of(new), _ver_of(cur)))
        else:
            # ⚠️ אומרים גם כשאין שינוי, עם הגרסה: "שקט" הוא בדיוק מה שהסתיר
            # שהורדנו גרסה ישנה מהמטמון וחשבנו שאנחנו מעודכנים.
            print("✓ הדרייבר עדכני (%s): %s" % (src, _ver_of(cur)))
    except Exception as e:                   # noqa: BLE001
        print("עדכון דרייבר נכשל (%s) — ממשיכים עם הקובץ המקומי" % e)


_self_update()
_self_update_agent()

try:
    import pos_driver
except Exception as e:                       # noqa: BLE001
    print("pos_driver import failed:", e)
    pos_driver = None

# ── קונפיג ────────────────────────────────────────────────────────────
GREENOS_URL = os.environ.get("GREENOS_URL", "https://gm-transfers.onrender.com").rstrip("/")
_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_key.txt")
SHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screenshots")
CITY_BRANCH = 3                              # מחסן\מרלוג
os.makedirs(SHOT_DIR, exist_ok=True)


def _load_key() -> str:
    k = os.environ.get("GREENOS_ADMIN_KEY", "").strip()
    if k:
        return k
    if os.path.exists(_KEY_FILE):
        return open(_KEY_FILE, encoding="utf-8").read().strip()
    k = input("הזן מפתח ניהול GreenOS (נשמר מקומית ל-agent_key.txt): ").strip()
    if k:
        open(_KEY_FILE, "w", encoding="utf-8").write(k)
    return k


_dry_done = {}             # {id: זמן} — הודגם ב-dry; פג אחרי 5 דק' כדי לאפשר ניסיון חוזר
_DRY_TTL = 300

KEY = _load_key()
H = {"X-Admin-Key": KEY, "Content-Type": "application/json"}


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S") + "  " + str(msg)
    print(line, flush=True)
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent.log"),
                  "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:                        # noqa: BLE001
        pass


# ── GreenOS API ───────────────────────────────────────────────────────
def api(method, path, **kw):
    r = requests.request(method, GREENOS_URL + path, headers=H, timeout=30, **kw)
    if r.status_code == 401:
        log("401 — מפתח ניהול שגוי. מחק את agent_key.txt והרץ שוב.")
        sys.exit(1)
    return r


def get_config():
    # ping=1 → חותמת חיים למסך. locked מדווח אם מסך המחשב נעול: הדופק עצמו הוא
    # בקשת רשת ועובד מצוין על מסך נעול, ולכן בלעדיו המסך היה מציג "סוכן פעיל"
    # בזמן ששום הזנה לקופה לא יכולה לרוץ.
    lk = 0
    try:
        if pos_driver is not None and not pos_driver._desktop_active():
            lk = 1
    except Exception:                        # noqa: BLE001
        pass
    r = api("GET", "/api/admin/pos/agent-config?ping=1&locked=%d" % lk)
    return r.json() if r.ok else {"enabled": False, "dry_run": True, "poll_sec": 25}


def get_pending():
    # due=1 → רק פעולות שהגיע זמנן. פעולה שנכשלה חוזרת לתור עם השהיה עולה,
    # כדי שלא ננסה אותה בלולאה צמודה — אבל היא **תמיד** תחזור עד שתרד בפועל.
    r = api("GET", "/api/admin/pos/removals?status=pending&due=1&limit=50")
    return (r.json() or {}).get("removals", []) if r.ok else []


def claim(rid):
    r = api("POST", "/api/admin/pos/removal/%s/claim" % rid)
    return r.json() if r.ok else None        # None = 409 (כבר נתפס) או שגיאה


def report(rid, status, pos_doc_no="", error=""):
    api("POST", "/api/admin/pos/removal/%s/result" % rid,
        json={"status": status, "pos_doc_no": pos_doc_no, "error": error[:400]})


def stock_of(sku):
    """מלאי הפריט בסניף סיטי — קריאה **טרייה** (בלי מטמון).

    ⛔ עד 09/08/2026 קראנו כאן /api/admin/pos/lookup, שם כרטיס המוצר ממוטמן
    180 שניות. הקריאה שאחרי ההזנה החזירה את הערך שלפניה, האימות הסיק
    "לא ירד", והפעולה הוזנה שוב לקופה — 4 תעודות על יחידה אחת (#42)."""
    r = api("GET", "/api/admin/pos/stock-fresh?pid=%s" % sku)
    if not r.ok:
        return None
    st = (r.json() or {}).get("stock") or {}
    v = st.get(str(CITY_BRANCH), st.get(CITY_BRANCH))
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def already_in_pos(rid):
    """האם הפעולה כבר נרשמה בקופה? (נשאל רק בניסיון חוזר.)
    מחזיר (applied, מספרי תעודות) — applied=True ⇒ אסור להזין שוב."""
    r = api("GET", "/api/admin/pos/removal/%s/pos-applied" % rid)
    if not r.ok:
        return False, ""
    d = r.json() or {}
    return bool(d.get("applied")), ",".join([x for x in (d.get("docs") or []) if x])


# ── עיבוד פעולה ───────────────────────────────────────────────────────
def snapshot_before(items):
    return {it["sku"]: stock_of(it["sku"]) for it in items}


def verify_after(items, before):
    """המלאי ירד לפחות בכמות שהוזנה? מחזיר (ok, פירוט).

    ⚠️ התנאי הוא "ירד לפחות", לא "ירד בדיוק": בין הקריאות עלולה להיכנס מכירה
    או הורדה אחרת בסניף, ואז ירידה גדולה מהצפוי אינה סיבה להזין שוב — הזנה
    חוזרת מורידה מלאי אמיתי פעם נוספת."""
    problems = []
    for it in items:
        sku, qty = it["sku"], float(it.get("qty") or 1)
        b = before.get(sku)
        a = stock_of(sku)
        if b is None or a is None:
            problems.append("%s: מלאי לא נקרא (before=%s after=%s)" % (sku, b, a))
        elif (b - a) < qty - 0.001:
            problems.append("%s: ציפינו לירידה %g, ירד %g" % (sku, qty, (b - a)))
        elif (b - a) > qty + 0.001:
            log("  ℹ️ %s ירד %g במקום %g — תנועה נוספת בסניף בין הקריאות" %
                (sku, (b - a), qty))
    return (not problems), " · ".join(problems)


def process(r, cfg=None):
    rid = r["id"]
    items = r.get("items") or []
    cfg = cfg or get_config()
    dry = cfg.get("dry_run", True)
    tuning = cfg.get("tuning") or {}
    log("פעולה #%s (%d פריטים, %s) — %s" %
        (rid, len(items), "אסי" if False else r.get("employee_name", ""),
         "DRY-RUN" if dry else "חי"))

    if not claim(rid):
        log("  דילוג — כבר נתפס/בוטל")
        return
    if pos_driver is None:
        report(rid, "error", error="pos_driver לא נטען על המכונה")
        return

    # 🛡️ ניסיון חוזר: אם הפעולה כבר נרשמה בקופה — לא מזינים שוב.
    # (#42, 09/08/2026: אימות שגוי החזיר את הפעולה לתור ארבע פעמים, וכל פעם
    #  ירדה יחידה אמיתית מהמלאי.)
    if int(r.get("attempts") or 1) > 1 and not dry:
        applied, docs = already_in_pos(rid)
        if applied:
            log("  ↩︎ כבר ירד בקופה (תעודות %s) — מסומן כהוזן בלי הזנה נוספת" % (docs or "?"))
            report(rid, "done", pos_doc_no=docs)
            return

    before = snapshot_before(items)
    shot = os.path.join(SHOT_DIR, "removal_%s.png" % rid)
    try:
        doc_no = pos_driver.apply_removal(r, dry_run=dry, screenshot_path=shot, tuning=tuning)
    except Exception as e:                    # noqa: BLE001
        # השרת מחזיר את הפעולה לתור אוטומטית (backoff) — לא נוטשים הורדה בשגיאה.
        msg = str(e)
        low = msg.lower()
        if "access is denied" in low or "no rights" in low:
            # ⚠️ 18/08: כשל הרשאות דווח כ"מסך נעול" ושלח את אסי לבדוק את המסך
            # בזמן שהקופה הייתה פתוחה מולו. ההודעה חייבת לומר את האמת.
            msg = getattr(pos_driver, "NOT_ADMIN_MSG", msg)
        elif "no active desktop" in low:
            msg = getattr(pos_driver, "DESKTOP_LOCKED_MSG", msg)
        log("  ❌ כשל בהזנה: %s — יוחזר לתור לניסיון נוסף" % msg)
        report(rid, "error", error=msg)
        # ⛔ בלי ניקוי מסך כשהמסך נעול — גם הוא מבוסס קליקים ורק ייכשל שוב
        if "נעול" not in msg:
            try:                               # שאריות מסך יחסמו את הניסיון הבא
                pos_driver.recover()
            except Exception as e2:            # noqa: BLE001
                log("  (ניקוי מסך נכשל: %s)" % e2)
        return

    if dry:
        # ב-dry-run לא נשמר כלום — מחזירים לתור כדי שריצה חיה תוכל לבצע אותו.
        # ⚠️ אבל מסמנים מקומית שכבר הודגם, אחרת הסוכן היה מריץ את אותה פעולה
        # בלולאה אינסופית בכל סבב (וגם נתקע על דיאלוג שנשאר פתוח מהסבב הקודם).
        _dry_done[rid] = time.time()
        log("  ✓ dry-run הושלם (צילום: %s). נשאר ב-pending, לא יורץ שוב ב-dry." % shot)
        report(rid, "pending")
        return

    ok, detail = verify_after(items, before)
    if ok:
        log("  ✅ הוזן ואומת — המלאי ירד כצפוי (תעודה %s)" % (doc_no or "?"))
        report(rid, "done", pos_doc_no=doc_no or "")
    else:
        log("  ⚠️ הוזן אבל האימות נכשל: %s" % detail)
        report(rid, "error", error="אימות מלאי נכשל: " + detail)


def main():
    if pos_driver is not None:
        pos_driver.LOG = log         # הודעות הדרייבר (כולל שורת הזמנים) → agent.log
    ver = getattr(pos_driver, "DRIVER_VERSION", "?") if pos_driver else "no-driver"
    log("סוכן הורדה מהמלאי — GreenOS=%s | גרסת דרייבר: %s" % (GREENOS_URL, ver))
    if pos_driver is not None:
        try:
            elev = pos_driver._is_elevated()
            if elev is False:
                log("⚠️ הסוכן רץ **בלי הרשאות מנהל** — Windows יחסום שליטה על חלונות "
                    "הקופה. לסגור ולהריץ מחדש משורת פקודה כמנהל.")
            elif elev:
                log("הרשאות מנהל: כן")
        except Exception:                     # noqa: BLE001
            pass
    log("ממתין לפעולות. (enabled/dry_run נשלטים מ-GreenOS; ברירת מחדל: מושבת+dry)")
    _last_upd = time.time()
    while True:
        nap = 5
        try:
            # בדיקת עדכון לעצמו כשאין פעולה בעבודה — חצי שעה מספיקה
            if time.time() - _last_upd > 1800:
                _last_upd = time.time()
                _self_update_agent()
            cfg = get_config()
            # ⏱️ poll_sec נמוך = הפעולה נתפסת כמעט מיד אחרי השמירה במסך. זו קריאה
            # אחת קלה לשרת שלנו (לא ל-NewOrder), ולכן אין לה מחיר במכסה.
            nap = max(3, int(cfg.get("poll_sec", 5)))
            if not cfg.get("enabled"):
                time.sleep(max(10, nap))
                continue
            for r in get_pending():
                _t = _dry_done.get(r["id"])
                if cfg.get("dry_run", True) and _t and (time.time() - _t) < _DRY_TTL:
                    continue            # הודגם ב-dry לאחרונה; אחרי 5 דק' ננסה שוב
                process(r, cfg)
        except Exception as e:                # noqa: BLE001
            log("סבב נכשל: %s" % e)
        time.sleep(nap)                       # ⛔ בלי get_config נוסף — סבב = קריאה אחת


if __name__ == "__main__":
    main()
