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
    r = api("GET", "/api/admin/pos/agent-config?ping=1")   # ping=1 → חותמת חיים למסך
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
    """מלאי הפריט בסניף סיטi (דרך GreenOS — הסוכן לא צריך טוקן NewOrder)."""
    r = api("GET", "/api/admin/pos/lookup?code=%s&branch_id=%d" % (sku, CITY_BRANCH))
    if not r.ok:
        return None
    prod = (r.json() or {}).get("product") or {}
    st = prod.get("stock") or {}
    v = st.get(str(CITY_BRANCH), st.get(CITY_BRANCH))
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── עיבוד פעולה ───────────────────────────────────────────────────────
def snapshot_before(items):
    return {it["sku"]: stock_of(it["sku"]) for it in items}


def verify_after(items, before):
    """המלאי ירד בדיוק בכמות שהוזנה? מחזיר (ok, פירוט)."""
    problems = []
    for it in items:
        sku, qty = it["sku"], float(it.get("qty") or 1)
        b = before.get(sku)
        a = stock_of(sku)
        if b is None or a is None:
            problems.append("%s: מלאי לא נקרא (before=%s after=%s)" % (sku, b, a))
        elif abs((b - a) - qty) > 0.001:
            problems.append("%s: ציפינו לירידה %g, ירד %g" % (sku, qty, (b - a)))
    return (not problems), "; ".join(problems)


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

    before = snapshot_before(items)
    shot = os.path.join(SHOT_DIR, "removal_%s.png" % rid)
    try:
        doc_no = pos_driver.apply_removal(r, dry_run=dry, screenshot_path=shot, tuning=tuning)
    except Exception as e:                    # noqa: BLE001
        # השרת מחזיר את הפעולה לתור אוטומטית (backoff) — לא נוטשים הורדה בשגיאה.
        log("  ❌ כשל בהזנה: %s — יוחזר לתור לניסיון נוסף" % e)
        report(rid, "error", error=str(e))
        try:                                   # מנקים שאריות מסך כדי שהניסיון הבא יתחיל נקי
            pos_driver.recover()
        except Exception as e2:                # noqa: BLE001
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
    ver = getattr(pos_driver, "DRIVER_VERSION", "?") if pos_driver else "no-driver"
    log("סוכן הורדה מהמלאי — GreenOS=%s | גרסת דרייבר: %s" % (GREENOS_URL, ver))
    log("ממתין לפעולות. (enabled/dry_run נשלטים מ-GreenOS; ברירת מחדל: מושבת+dry)")
    while True:
        nap = 5
        try:
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
