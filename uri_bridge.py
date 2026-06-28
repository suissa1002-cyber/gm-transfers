#!/usr/bin/env python3
"""
uri_bridge — הגשר של אורי: GreenOS ←→ Claude Code על המק של אסי.

הרעיון: הטאב 💬 ב-GreenOS שולח בקשות לאורי (טיוטות/מלאי/מחירים) לתור
ב-gm-transfers; הסקריפט הזה רץ על המק (launchd, KeepAlive), מושך בקשות,
מריץ `claude -p` מקומית — **חיוב חבילת ה-Max, אפס טוקני API** — ומחזיר
את התשובה לחלון הצ'אט.

ל-claude יש גישה מלאה ל-workspace (כללי אורי, קליינטים של רון/ConnectOp,
מלאי חי) — אבל ההנחיה היא קריאה בלבד: לעולם לא לשלוח הודעות בעצמו.

התקנה: ~/Library/LaunchAgents/com.greenmobile.uri-bridge.plist (KeepAlive).
לוגים: ~/Library/Logs/uri-bridge.log
"""
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent   # gm-transfers repo root (vendored layout)
sys.path.insert(0, str(ROOT / "shared"))

# .env של ה-workspace (אם קיים — על Render אין קובץ, המשתנים מגיעים מסביבת השירות)
_envf = ROOT / ".env"
if _envf.exists():
    for line in _envf.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

BASE = os.environ.get("URI_BRIDGE_BASE", "https://gm-transfers.onrender.com")
KEY = os.environ.get("URI_BRIDGE_KEY", "")
CLAUDE = os.environ.get("URI_BRIDGE_CLAUDE", "/usr/local/bin/claude")
# בלי --model = ברירת המחדל של Claude Code (המודל החזק של חבילת ה-Max).
# sonnet היה מהיר אבל איכות נמוכה משמעותית מהסשנים הרגילים של אורי.
MODEL = os.environ.get("URI_BRIDGE_MODEL", "")
POLL_SEC = 4
JOB_TIMEOUT = 420  # המודל החזק + בדיקות מלאי/אתר לוקחים יותר מ-sonnet

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("uri_bridge")

import requests  # noqa: E402

H = {"X-Bridge-Key": KEY}


def fetch_thread_text(phone: str, limit: int = 25) -> str:
    """הקשר השיחה מ-ConnectOp (מקומית — הקליינט וה-token זמינים במק)."""
    try:
        from chatrace_dashboard_client import ChatRaceDashboardClient
        d = ChatRaceDashboardClient.from_env()
        msgs = list(reversed(d.get_conversation(phone, limit=limit)))
        lines = []
        for m in msgs:
            t = (m.get("text") or "").strip()
            if not t:
                continue
            who = "לקוח" if m.get("direction") == "in" else "גרין מובייל"
            lines.append(f"{who}: {t}")
        return "\n".join(lines[-limit:])
    except Exception as e:  # noqa: BLE001
        log.warning("thread fetch failed: %s", e)
        return "(לא הצלחתי למשוך את השיחה — ענה לפי השאלה בלבד)"


def fetch_thread_native(phone: str, limit: int = 8) -> str:
    """הקשר מהחנות הנייטיב (GreenOS) — כולל את הודעות הבוט/אורי עצמן, להמשכיות.
    נופל חזרה לקונקטופ אם לא זמין."""
    try:
        r = requests.get(f"{BASE}/api/uri-bridge/thread/{phone}?limit={limit}",
                         headers=H, timeout=15)
        if r.ok and (r.json().get("thread") or "").strip():
            return r.json()["thread"]
    except Exception as e:  # noqa: BLE001
        log.warning("native thread fetch failed: %s", e)
    return fetch_thread_text(phone, limit=limit)


def fetch_context(phone: str) -> str:
    """הקשר מ-GreenOS: הערות (הזיכרון המצטבר על הלקוח) + הזמנות אתר — מוזרק
    ל-prompt כדי לחסוך לאורי סבבי בדיקות (זה מה שלקח הכי הרבה זמן)."""
    try:
        r = requests.get(f"{BASE}/api/uri-bridge/context/{phone}", headers=H, timeout=20)
        if not r.ok:
            return ""
        d = r.json()
        parts = []
        if d.get("notes"):
            parts.append("### מה שכבר למדנו על הלקוח (הערות GreenOS):\n" + "\n".join(
                f"- [{n.get('author','')}] {n.get('text','')}" for n in d["notes"][:8]))
        if d.get("orders"):
            parts.append("### הזמנות אתר של הטלפון הזה:\n" + "\n".join(
                f"- #{o['id']} | {o['status']} | {o['currency']}{o['total']} | {o['date']} | " +
                ", ".join(o.get("items", [])[:3]) for o in d["orders"][:5]))
        return "\n\n".join(parts)
    except Exception as e:  # noqa: BLE001
        log.warning("context fetch failed: %s", e)
        return ""


def build_prompt(phone: str, question: str) -> str:
    thread = fetch_thread_native(phone, limit=20)   # native (GreenOS) — לא ConnectOp
    ctx = fetch_context(phone)
    return f"""אתה אורי — סוכן שירות הלקוחות של Green Mobile, בדיוק כמו בסשנים הרגילים שלך.
אסי פנה אליך מתוך מערכת GreenOS לגבי שיחת וואטסאפ עם לקוח.

## חובה לפני שאתה עונה — טען את ההקשר שלך:
1. קרא את URI_BRAIN.md (כללי הברזל, סגנון, משלוחים, מחיר מבצע).
2. אם השאלה נוגעת למלאי/מחיר/זמינות — בדוק **בפועל** דרך shared/neworder_client.py ומול האתר
   (WC variations — שדה price כולל מבצע). לעולם אל תנחש מידע שאפשר לבדוק.
3. הזמנות הלקוח כבר מצורפות למטה — אל תחפש אותן שוב אלא אם חסר פרט.
4. 🔧 **מחיר תיקון/החלפה** (מסך/סוללה/שקע/מצלמה/זכוכית — "כמה עולה מסך לאייפון 16 פרו") —
   ⚠️ **יש מחירון מעבדה רשמי. אל תמציא, אל תגיד 'תלוי בבדיקה' ואל תגיד 'לא מוכרים מסך בנפרד'.**
   שלוף: `curl -s "{BASE}/api/uri-bridge/repair?q=<דגם+מהות>" -H "X-Bridge-Key: {KEY}"`
   (`q=מסך iphone 16 pro`). found=true → ענה עם ה-`text` שחזר; found=false → רק אז נציג/אבדוק.

## השיחה האחרונה עם הלקוח ({phone}):
{thread}

{ctx}

## הבקשה של אסי:
{question}

## כללים:
- ⛔ אסור בהחלט לשלוח הודעות ללקוח, לשנות נתונים באתר/קופה/ConnectOp. אסי שולח ללקוח בעצמו.
- ✅ כתיבות פנימיות מותרות (ורצויות כשאסי מבקש):
  • **תזכורת אישית** ("תזכיר לי", "לחזור ללקוח ב...") → POST https://uri-stock-watcher.onrender.com/reminders
    (Authorization: Bearer <stock_watcher_token מתוך agents/uri/stock_watcher/.deploy_state.json>;
    body: {{"due_at":"YYYY-MM-DD HH:MM" שעון ישראל, "context":"...", "customer_name":"...", "customer_phone":"{phone}"}}).
    זו התזכורת שמופיעה בלוח Uri Stock Watcher ושולחת טלגרם בזמן שנקבע — זה הערוץ הנכון, לא Monday.
  • **הרשמה ל-Stock Watch** (לקוח שמבקש עדכון כשמוצר/צבע/נפח *שאזל* חוזר למלאי) →
    ⚠️ אל תירשם בעצמך, אל תקרא ל-API, אל תשלח הודעה. הרישום נעשה בלחיצת כפתור דטרמיניסטית
    אצל אסי — השרת פותר את המק\"ט לבדו ושולח ללקוח אישור אוטומטית. אתה רק **מזהה מהשיחה**
    ופולט שורה אחת: `[SWATCH product=<שם הדגם באנגלית>|url=<קישור לעמוד>|color=<הצבע שאזל>|size=<מידה/נפח>]`.
    מלא רק מה שרלוונטי (color/size ריק אם אין). **אל תמציא מק\"ט, בלי curl, בלי בדיקת מלאי.**
  • **משימת עבודה גדולה** (לא תזכורת-זמן) → משימה במאנדיי דרך agents/shared/monday_tasks (קבוצת uri).
  אחרי יצירה — אשר לאסי מה נוצר, איפה, ולמתי.
- מחיר ללקוח: תמיד מחיר האתר (שדה price — כולל מבצע), לעולם לא מחיר קופה. בפער קופה↔אתר — האתר מנצח.
- ניסוח ללקוח: עברית, סגנון אורי (חם, ענייני, בלי סיומות AI-יות). מותגים/דגמים באנגלית.
- **בכל תשובת-מוצר חובה קישור לעמוד המוצר** — לוריאציה המדויקת אם דובר עליה;
  slug עברי → קישור מקוצר TinyURL עם alias ‏gm- (הסעיף המלא ב-URI_BRAIN.md).
- מידע פנימי לאסי: תמציתי ומדויק, כולל המספרים שמצאת.

## למידה (חשוב!):
אם גילית משהו עמיד ששווה לזכור על הלקוח הזה לפעמים הבאות (מה הוא מחפש, העדפות,
רגישויות, הזמנה בתהליך) — הוסף בסוף התשובה שורה נפרדת בפורמט: [NOTE] <התובנה במשפט אחד>
(עד 2 שורות כאלה; הן נשמרות אוטומטית בכרטיס הלקוח ויחזרו אליך בפנייה הבאה.)

## פורמט הפלט (קריטי!):
- כשהתשובה כוללת נוסח הודעה ללקוח — עטוף את הנוסח המדויק *בלבד* בין שורה [DRAFT]
  לשורה [/DRAFT]. אסי שולח את הבלוק הזה ללקוח כמו-שהוא בלחיצה אחת — שום מילה
  מיותרת בתוכו.
- מחוץ לבלוק — רק אם באמת נחוץ: שורת עובדות אחת לאסי (מחיר/מלאי שמצאת).
  בלי סיכומים, בלי "בדקתי ו...", בלי לתאר מה עשית.
- שורות [NOTE] בסוף."""


def build_followup_prompt(phone: str, question: str) -> str:
    """בקשת המשך בסשן קיים — ההקשר כבר בזיכרון; שולחים רק עדכון שיחה + שאלה."""
    thread = fetch_thread_native(phone, limit=10)   # native (GreenOS) — לא ConnectOp
    return f"""בקשה נוספת מאסי על אותה שיחת וואטסאפ ({phone}).
עדכון אחרון מהשיחה (ייתכן שהתחדשה):
{thread}

הבקשה: {question}

אותם כללים (כולל: תזכורות דרך ה-API של Stock Watcher; אסור לשלוח ללקוח; מחיר אתר;
תשובת-מוצר חייבת קישור לעמוד/לוריאציה — slug עברי → TinyURL ‏gm-).
⚠️ הרשמת לקוח ל-Stock Watch (עדכון כשוריאציה שאזלה חוזרת) — אל תירשם בעצמך ואל תקרא ל-API;
פלוט שורה `[SWATCH product=<שם הדגם>|url=<קישור>|color=<צבע שאזל>|size=<מידה/נפח>]` (מלא רק מה
שרלוונטי, בלי להמציא מק\"ט). אסי לוחץ כפתור — השרת פותר את המק\"ט, רושם ושולח אישור. בלי curl.
פלט: נוסח ללקוח — בין [DRAFT] ל-[/DRAFT] בלבד, בלי מילה מיותרת בתוכו; מחוץ לבלוק
רק שורת עובדות אם נחוצה (+שורות [NOTE] אם יש מה ללמוד / שורת [SWATCH] אם רלוונטי). בלי סיכומים."""


# פעולות תפעוליות ישירות מאסי (stock-watch / תזכורת) — דטרמיניסטיות, לא צריכות את
# המוח הכבד (URI_BRAIN.md / בדיקת מלאי). מזוהות לפי מילות-מפתח → מסלול fast (sonnet),
# מ-9 turns/~5 דק' ל-~2 turns/שניות. (אסי 24/06 — "הוסף לסטוק וואצ" לקח 5 דק'.)
_OP_KEYWORDS = ("סטוק וואצ", "סטוק-וואצ", "stock watch", "stock-watch", "רשימת המתנה",
                "רשימת סטוק", "כשחוזר למלאי", "חוזר למלאי", "עדכן כשיחזור", "הוסף לרשימה",
                "תזכיר", "תזכורת", "reminder")


def _is_op_task(question: str) -> bool:
    q = (question or "").strip().lower()
    if q == "[warmup]":
        return False
    return any(k.lower() in q for k in _OP_KEYWORDS)


def build_panel_op_prompt(phone: str, question: str) -> str:
    """מסלול מהיר לפעולה תפעולית ישירה (stock-watch / תזכורת) — בלי קריאת קבצים,
    בלי בדיקת מלאי/מחיר. רק לבצע דרך ה-API ולאשר בשורה אחת."""
    thread = fetch_thread_native(phone, limit=12)
    ctx = fetch_context(phone)
    return f"""אסי מבקש שתבצע פעולה תפעולית ישירה על שיחת וואטסאפ ({phone}).
בצע אותה **מיד ובמינימום צעדים** — אל תקרא קבצים (לא URI_BRAIN.md), אל תבדוק מלאי/מחיר,
אל תחקור מעבר לנדרש. כל מה שצריך מתועד כאן למטה.

## השיחה האחרונה:
{thread}

{ctx}

## הבקשה של אסי:
{question}

## איך לבצע (לפי סוג):
- **הרשמה ל-Stock Watch** (עדכון כשמוצר/צבע/נפח שאזל חוזר למלאי): ⚠️ **אל תירשם בעצמך,
  אל תקרא ל-API, אל תשלח כלום.** הרישום נעשה בלחיצת כפתור דטרמיניסטית בצד אסי — והשרת
  פותר את המק\"ט לבדו ושולח ללקוח אישור אוטומטית. התפקיד שלך: רק **לזהות מהשיחה** מה הלקוח
  רוצה, ולפלוט שורה אחת בלבד בפורמט:
  `[SWATCH product=<שם המוצר באנגלית>|url=<קישור לעמוד המוצר>|color=<הצבע שאזל>|size=<מידה/נפח אם רלוונטי>]`
  • `product` — שם הדגם המדויק (Google Pixel Watch 4 וכו'). `url` — קישור לעמוד (אם יש לך).
  • `color`/`size` — מה שהלקוח ביקש (השאר ריק אם לא רלוונטי). **אל תמציא מק\"ט.** השרת
    מוצא את הוריאציה שאזלה לפי השם/קישור+צבע/מידה. **בלי curl, בלי בדיקת מלאי, בלי [DRAFT].**
- **תזכורת אישית**: `POST https://uri-stock-watcher.onrender.com/reminders`
  (Authorization: Bearer <token מ-agents/uri/stock_watcher/.deploy_state.json>;
  body: {{"due_at":"YYYY-MM-DD HH:MM" שעון ישראל,"context":"...","customer_name":"...","customer_phone":"{phone}"}}).

⛔ אסור לשלוח הודעות ללקוח בעצמך / לשנות נתונים באתר/קופה.
פלט: לסטוק-וואצ' — שורת `[SWATCH ...]` אחת (+אופציונלי חצי-משפט לאסי מה זוהה). לתזכורת —
שורה אחת לאסי מה נקבע ולמתי. בלי סיכומים, בלי [DRAFT]."""


def build_bot_prompt(phone: str, question: str) -> str:
    """משימת בוט (source='bot') — התשובה נשלחת **ישירות ללקוח** אוטומטית. מהירות
    קריטית: בלי קריאת קבצי פרויקט, בלי בדיקות מלאי כבדות. הכללים החיוניים מוזרקים
    כאן, ומוצרים מועמדים כבר צורפו לשאלה — כך שברוב המקרים אין צורך אפילו בקריאת WC."""
    thread = fetch_thread_native(phone, limit=8)   # כולל את הודעות אורי עצמן (המשכיות)
    ctx = fetch_context(phone)                      # הזמנות הלקוח + הערות מצטברות (היכרות)
    return f"""אתה אורי, שירות הלקוחות של Green Mobile. אתה עונה **ישירות ללקוח** בוואטסאפ —
הטקסט שתחזיר נשלח אליו כמו-שהוא, מיד. ענה **קצר וענייני**, ו**תמיד** תחבר את התשובה
למוצרים אמיתיים עם מחיר וקישור.

## מה אתה כבר יודע על הלקוח הזה (היכרות והמשכיות — השתמש בזה כשרלוונטי):
{ctx if ctx else "אין מידע קודם — לקוח חדש או בלי הזמנות/הערות."}

## מקורות המידע שלך:
⭐ **קיום / זמינות / מחיר — האתר הוא מקור האמת, לא הידע שלך.** Green Mobile מוכרת דגמים
עדכניים וחדשים (כולל כאלה ש"יצאו" אחרי מועד הידע שלך — iPhone 17/18, Galaxy חדש). **לעולם
אל תגיד שמוצר "לא קיים" / "עוד לא יצא" / "לא זמין" על סמך הידע שלך** — זה כמעט תמיד שגוי
ומביך מול לקוח שרואה אותו באתר. כל דגם שלקוח מזכיר → **תחפש אותו** (`search`); החיפוש
**עובד**, אל תניח שאין לך גישה. חיפוש ריק לחלוטין → "אבדוק ואחזור" / [HANDOFF], **לא** "לא קיים".
⭐ **פיצ'רים / מפרט — שונה!** כאן המידע באתר הוא נקודת פתיחה אך **יכול להכיל טעויות**. אל
תשכפל מפרט מהאתר בעיוורון. ענה **רק על מה שאתה בטוח בו בוודאות** (מתוך הצלבה של תיאור האתר
עם עובדות מבוססות וידועות על הדגם). אם פרט באתר נראה שגוי או שאינך בטוח — אל תצהיר עליו;
תן את מה שבטוח, או "אבדוק ואחזור". **עדיף לא לענות על מפרט מאשר לתת מפרט שגוי בביטחון.**
1. "המוצרים המועמדים" שצורפו לשאלה — כבר נשלפו מהאתר. התחל מהם.
2. אם הם לא מספיקים (השוואה, תקציב, תכונה ספציפית, דגם אחר) — **חפש בעצמך** באתר:
   `curl -s "{BASE}/api/uri-bridge/search?q=<מילות חיפוש>" -H "X-Bridge-Key: {KEY}"`
   (מחזיר id/name/price_from/type/in_stock/url). **מקסימום 2 חיפושים**. אל תקרא קבצים.
3. ⚠️ `price_from` = **מחיר הבסיס** (הוריאציה הזולה). לשאלות על **נפח/צבע/מחיר ספציפי**
   (כמו "256GB עד 700" / "שלט שחור") — בדוק את הוריאציות של המוצר לפי ה-id:
   `curl -s "{BASE}/api/uri-bridge/variations/<id>" -H "X-Bridge-Key: {KEY}"`
   (storage/color/price/in_stock/**url**). **לעולם אל תקבע ש"אין" לפי price_from — לוריאציה
   אחרת מחיר אחר!** (דוגמה: Redmi 15C מ-539₪, אבל 256GB עולה אחרת — תמיד תבדוק.)
   🛑 **מלאי = מקור אמת אחד: השדה `in_stock` של הוריאציה.** אם `in_stock=true` —
   הצבע/הנפח **זמין**, נקודה. **אסור לומר ללקוח שצבע/וריאציה אזל אם `in_stock=true`**,
   ואסור להמציא חוסר מלאי כדי להציע מוצר יקר יותר. אם הלקוח שאל על צבע ספציפי שזמin —
   ענה עליו ראשון, וקשר ל-**url של אותה וריאציה** (כולל פרמטר הצבע — קישור נקי ולחיץ).
   רק אם `in_stock=false` לאותה וריאציה — אז (ורק אז) ציין שאזל והצע חלופה.
4. ⭐ לשאלות **"X עם תכונה Y עד Z₪"** (כמו "אוזניות עם ביטול רעשים עד 250") — **חיפוש
   לפי שם מפספס!** התכונה נמצאת ב-attributes של המוצר, לא בכותרת. השתמש ב**סינון תכונה**:
   `curl -s "{BASE}/api/uri-bridge/filter?q=<השאלה>&max_price=<מחיר>" -H "X-Bridge-Key: {KEY}"`
   (מזהה ANC/'ביטול רעשים'/RGB... בעברית ובאנגלית, מחזיר את **כל** המתאימים בטווח עם count).
   **לעולם אל תגיד "אפשרות יחידה" בלי להריץ filter קודם!**
5. 📦 **שאלת הזמנה / משלוח** ("איפה ההזמנה", "מתי יגיע", "מה הסטטוס") — אם זה לא כבר
   ברשימת ההזמנות למעלה, בדוק בפועל:
   `curl -s "{BASE}/api/uri-bridge/order?phone={phone}" -H "X-Bridge-Key: {KEY}"`
   (גם לפי `&number=<מס' הזמנה>` או `&name=<שם>`). מחזיר status/items/total/date.
   ענה מהנתונים בלבד — **לעולם אל תמציא סטטוס או תאריך משלוח**. אם אין הזמנה — בקש מספר הזמנה.
6. 🔧 **מחיר תיקון/החלפה** (מסך, סוללה, שקע טעינה, מצלמה, זכוכית גב וכו' — "כמה עולה
   מסך לאייפון 16 פרו", "החלפת סוללה גלקסי S23") — ⚠️ **יש לנו מחירון מעבדה רשמי. אל
   תמציא ואל תגיד 'תלוי בבדיקה' או 'לא מוכרים מסך בנפרד'.** שלוף את המחיר:
   `curl -s "{BASE}/api/uri-bridge/repair?q=<דגם+מהות התיקון>" -H "X-Bridge-Key: {KEY}"`
   (למשל `q=מסך iphone 16 pro`). אם `found=true` — ענה ללקוח עם ה-`text` שחזר (מחירי
   המעבדה). אם `found=false` — אז (ורק אז) "אבדוק מול המעבדה ואחזור אליך" / נציג.

## כללי תשובה (חשוב!):
- **תמיד** הצע מוצר/ים קונקרטיים עם **מחיר + קישור** (ה-url מהחיפוש). **לעולם אל
  תענה "אין לי פרטים"** — אם אין התאמה מדויקת, הצע את הקרוב ביותר או שאל שאלת הבהרה
  קצרה (תקציב? יצרן מועדף?). רק אם באמת אין שום מוצר רלוונטי באתר — הצע מעבר לנציג.
- ⛔ **אל תמציא עובדות** — בלי שנת יציאה/דור ("של 2025"), בלי מפרט שלא ודאי. היצמד
  לשם, מחיר וקישור מהאתר. אם תיאור — כללי וזהיר ("מצלמה חזקה", לא מספרי mm מומצאים).
- ✍️ **עברית נקייה וברורה** — נסח טבעי וזורם. הימנע מצמדי מספר+אנגלית בתוך עברית
  (כמו "200mm") שנשברים בתצוגת RTL ויוצאים ג'יבריש. כתוב "זום אופטי חזק" במקום.
- 🔗 **המשכיות** — קרא את השיחה למטה. **אם הלקוח עונה לשאלה ששאלת קודם** (למשל
  "מצלמה" אחרי ש"מה חשוב לך?") — התייחס לתשובה והמשך משם. אל תתחיל מחדש ואל תתעלם.
- 🏷️ **שאלה רחבה/מותגית בלי דגם** ("מה ההבדל בין JBL ל-OnePlus", "איזה מותג עדיף") —
  **אל תצמצם בכוח ל-2 דגמים** מהמועמדים. תן השוואה קצרה ברמת **מותג** (אופי/חוזקות),
  או שאל "על אילו דגמים חשבת? יש לנו כמה מכל מותג". אל תמציא השוואת דגם-יחיד כשלא נשאלת.
- 🌐 **קישורים** — כל קישור בשורה **נפרדת ופשוטה**, בלי 🔗 או טקסט לפניו באותה שורה
  (אמוג'י/טקסט צמוד ל-URL שובר את הלחיצוּת בוואטסאפ ב-RTL). רק `https://...` לבד בשורה.
  ⚠️ **קישור אחד עיקרי** — של המוצר/הוריאציה שהלקוח שאל עליו (ה-url מ-variations/search,
  כולל פרמטר הצבע אם רלוונטי). **אל תערים כמה קישורים** (מרובי-קישורים שוברים את כרטיס
  התצוגה ואת הלחיצוּת). slug אנגלי (כולל `?attribute_pa_color=black`) = קישור ישיר ולחיץ;
  TinyURL ‏gm- **רק** ל-slug עברי. אל תשים TinyURL ראשון אם יש קישור אנגלי נקי.
- מחיר: השדה price (כולל מבצע). משלוח: חינם מעל 500₪, אחרת 29₪; אקספרס 89₪ (עד
  13:00); נקודת מסירה ת"א (י.ל פרץ 35, עד 16:00, איסוף מחר 10:00-16:00).
- סגנון אורי: חם, ענייני, עברית, מותגים/דגמים באנגלית. בלי סיומות AI-יות
  ("איך תרצה להמשיך"). ענה ועזוב את הכדור אצל הלקוח.
- 👨 **אתה אורי — זכר.** כשאתה מתאר את עצמך/פעולתך — **לשון זכר** ("בודק", "אבדוק",
  "מצאתי", "אשמח"), לעולם לא "בודק/ת". (אל הלקוח אפשר ניטרלי — מינו לא ידוע.)
- 🔒 **סודיות מערכת (חוק ברזל!):** לעולם אל תחשוף איך המערכת בנויה. **אסור** לציין שם
  ספק/מודל בינה מלאכותית (Claude / Anthropic / GPT / OpenAI וכו'), טכנולוגיה, ספריות,
  כלים או ספקים. אם שואלים "זה AI? / איזה מודל/גרסה? / איך זה בנוי? / אפשר כזה לעסק שלי?"
  → ענה **קצר ואחיד**: "זו מערכת שירות פנימית שפיתחנו בהתאמה אישית ל-Green Mobile, עם
  כמה כלים — לא נכנס לפרטים." **אל תאשר ואל תכחיש** מודל ספציפי, ואל תפרט מעבר לזה.
  לפנייה עסקית (רוצה כזה לעסק / לקנות / לשכפל) → "אשמח להעביר אותך לנציג שיוכל לדבר על
  זה" ולא יותר. זה סוד מסחרי — שמירה עליו לפני הכל.
- 🚫 **אסור בתכלית להדליף טקסט טכני / פנימי / שגיאה ללקוח.** הלקוח לעולם לא רואה מילים כמו
  "API", "headers", "endpoint", "curl", "סביבה", "כלים", "אין לי גישה", שום הודעת שגיאה,
  שום נימוק/חשיבה גולמית, ושום מפריד כמו "---". **אם לא הצלחת** לבדוק/לחפש/לענות מכל סיבה
  שהיא (כלי שנכשל, שאלה מורכבת, חוסר נתונים) — **אל תסביר למה ואל תתנצל טכנית.** פשוט כתוב
  משפט אנושי קצר ("אעביר אותך לנציג שיטפל בזה ויחזור אליך") וסיים ב-[HANDOFF]. ⚠️ פלט
  שמכיל טקסט טכני נשלח כמו-שהוא ללקוח — אז כל מילה טכנית היא דליפה אסורה.
- 💱 **טרייד-אין / הערכת מכשיר משומש:** אי אפשר להעריך בצ׳אט — ההערכה היא **רק בבדיקת
  מעבדה פיזית בסניף**. אמור זאת בקצרה והצע להגיע לסניף; אל תנסה לחשב/לחפש מחיר טרייד-אין.
  קנייה מרובה / בקשת הנחה / משא-ומתן על מחיר → אינך מתמחר הנחות; העבר לנציג ([HANDOFF]).

## 🔧 תיקוני אחריות-יבואן (חשוב — אין לנו סטטוס!):
סניף **סטאר סנטר** משמש גם כ**נקודת מסירה** לתיקוני אחריות שנשלחים ליבואן. במקרים אלה
**אין לנו מידע על סטטוס התיקון** — הוא מתנהל מול **מעבדת היבואן** (הטלפון והפרטים על
הטופס שהלקוח קיבל במעמד המסירה). אנחנו רק מקבלים את המכשיר ומחזירים אותו כשהוא חוזר
מהיבואן (בהצגת אותו טופס). היבואנים: **Hamilton (המילטון)** = Xiaomi + Anker ·
**Vishpar (וישפאר)** = Sony (PlayStation וכו').
→ לשאלת סטטוס על תיקון כזה (מותג של יבואן / נמסר בסטאר סנטר): **הסבר את זה ללקוח** —
שאנחנו נקודת מסירה, שהסטטוס מול מעבדת היבואן (טלפון על הטופס), ושניצור קשר כשהמכשיר חוזר.
זו תשובה מלאה ומדויקת — **אל תעביר לנציג** במקרה הזה. (תיקון שנעשה במעבדה שלנו ≠ זה.)

## גבולות + העברה לנציג (קריטי!):
- לעולם אל תשנה / תבטל / תזכה הזמנה, ואל תבטיח פעולה ("אבטל לך", "אשנה כתובת", "אחזיר כסף").
- מותר לך לבדוק ולספר מידע (מוצרים, מלאי, סטטוס הזמנה) — לא לבצע שינויים.
- 🚨 **כשצריך נציג אנושי** (ביטול/שינוי/החזר/תלונה/סטטוס תיקון מול סניף/כל מקרה שאינך
  יכול לפתור) — כתוב משפט קצר ("אעביר אותך לנציג שיבדוק ויחזור אליך") **וסיים בשורה
  נפרדת בדיוק:** [HANDOFF]
  ⚠️ **רק [HANDOFF] מעביר באמת** — הוא משתיק אותך ומתריע לצוות. **בלי [HANDOFF] שום
  העברה לא קורית** — אז לעולם אל תכתוב "מעביר לנציג"/"נציג יחזור" בלי לסיים ב-[HANDOFF],
  ואחרי שכתבת [HANDOFF] אל תמשיך לענות בהודעות הבאות (אתה כבר העברת).

## השיחה האחרונה ({phone}):
{thread}

## שאלת הלקוח:
{question}

החזר **רק** את הטקסט ללקוח (עברית, 2-6 משפטים, עם מחיר+קישור). בלי [DRAFT], בלי
הקדמות, בלי לתאר מה בדקת.

אם למדת תובנה עמידה על הלקוח (העדפה, דגם שמחפש, הזמנה בתהליך) — הוסף בשורה אחרונה
**נפרדת** בפורמט: [NOTE] <תובנה במשפט אחד>. היא נשמרת בכרטיס הלקוח ו**לא נשלחת אליו**."""


# זיכרון שיחה: phone → {sid, at}. בקשת המשך תוך 30 דק' ממשיכה את אותו סשן claude —
# ההקשר כבר טעון (וה-prompt cache חם אם תוך 5 דק') = מהיר משמעותית + למידה רציפה.
_sessions = {}
SESSION_TTL = 1800


# משימת בוט (לקוח חי) רצה ב**תיקייה נקייה** (בלי auto-load של ה-CLAUDE.md הענק
# של ה-workspace) ובמודל **מהיר** — זה מוריד את הזמן מ-~5 דק' ל-~8 שניות.
BOT_CWD = os.path.join(os.environ.get("TMPDIR", "/tmp"), "uri-bot")
BOT_MODEL = os.environ.get("URI_BOT_MODEL", "sonnet")
try:
    os.makedirs(BOT_CWD, exist_ok=True)
except Exception:  # noqa: BLE001
    BOT_CWD = "/tmp"


def run_claude(prompt: str, resume_sid: str = None, fast: bool = False, timeout_s: int = None) -> tuple:
    """מריץ claude headless. fast=True → תיקייה נקייה + מודל מהיר (משימת בוט/לקוח).
    timeout_s → override ל-timeout (פעולות צריכות יותר מתשובה). מחזיר (ok, text, session_id)."""
    try:
        env = {**os.environ, "ANTHROPIC_API_KEY": ""}  # ביטחון: שלא יחויב API key בטעות
        env.pop("CLAUDECODE", None)   # מאפשר הרצה גם מתוך סשן Claude Code (בדיקות)
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)
        # launchd לא מספק PATH מלא — claude (shim של node) צריך את node
        env["PATH"] = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:" + env.get("PATH", "")
        cmd = [CLAUDE, "-p", prompt, "--output-format", "json", "--dangerously-skip-permissions"]
        if resume_sid:
            cmd += ["--resume", resume_sid]
        model = BOT_MODEL if fast else MODEL
        if model:
            cmd += ["--model", model]
        cwd = BOT_CWD if fast else str(ROOT)
        timeout = timeout_s or (90 if fast else JOB_TIMEOUT)
        r = subprocess.run(
            cmd,
            cwd=cwd, capture_output=True, text=True, timeout=timeout,
            env=env,
        )
        out = (r.stdout or "").strip()
        if r.returncode != 0 or not out:
            return False, (r.stderr or out or "claude נכשל")[-400:], None
        try:
            j = json.loads(out)
            # מדידה (אפס שינוי התנהגות): turns גבוה = הרבה סבבי-כלים = איטי
            log.info("claude metrics: turns=%s dur=%sms api=%sms cost=$%s fast=%s",
                     j.get("num_turns"), j.get("duration_ms"),
                     j.get("duration_api_ms"), j.get("total_cost_usd"), fast)
            return True, (j.get("result") or "").strip(), j.get("session_id")
        except Exception:  # noqa: BLE001
            return True, out, None
    except subprocess.TimeoutExpired:
        return False, "תם הזמן (claude לא סיים תוך ~7 דקות)", None
    except Exception as e:  # noqa: BLE001
        return False, f"שגיאת הרצה: {e}", None


def _extract_notes(text: str):
    """מפריד שורות [NOTE] מהתשובה — נשמרות ככרטיס לקוח (הלמידה של אורי)."""
    notes, clean = [], []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[NOTE]"):
            notes.append(s[6:].strip())
        else:
            clean.append(line)
    return "\n".join(clean).strip(), notes[:2]


def process(job: dict):
    jid = job["id"]
    phone = job["phone"]
    log.info("job #%s phone=%s src=%s q=%r", jid, phone, job.get("source"), job["question"][:60])
    # ── משימת בוט: מסלול מהיר, תשובה ישירה ללקוח (בלי קריאת מסמכים/מלאי כבד) ──
    if job.get("source") == "bot":
        ok, text, _sid = run_claude(build_bot_prompt(phone, job["question"]), fast=True)
        answer = (text or "").strip() if ok else \
            "סליחה, יש לי תקלה רגעית — נציג יחזור אליך בהקדם 🙏"
        try:
            requests.post(f"{BASE}/api/uri-bridge/answer", headers=H,
                          json={"id": jid, "answer": answer,
                                "status": "done" if ok else "error"}, timeout=20)
        except Exception as e:  # noqa: BLE001
            log.error("bot answer post failed #%s: %s", jid, e)
        log.info("bot job #%s -> %s (%d chars)", jid, "done" if ok else "error", len(answer))
        return
    if (job["question"] or "").strip() == "[WARMUP]":
        # חימום סשן: ה-UI שולח את זה ברגע שהפאנל נפתח — טוענים את כל ההקשר עכשיו,
        # כך שהשאלה האמיתית של אסי תרוץ כ-followup על סשן חם (שניות במקום דקות).
        sess = _sessions.get(phone)
        ans = "מוכן"
        if not (sess and time.time() - sess["at"] < SESSION_TTL):
            ok, text, new_sid = run_claude(build_prompt(
                phone, "טען את כל ההקשר עכשיו (CLAUDE.md, השיחה, ההזמנות). "
                       "אל תבצע שום פעולה ואל תבדוק מלאי עדיין. ענה במילה אחת בלבד: מוכן"))
            if ok and new_sid:
                _sessions[phone] = {"sid": new_sid, "at": time.time()}
            ans = "מוכן" if ok else text
        try:
            requests.post(f"{BASE}/api/uri-bridge/answer", headers=H,
                          json={"id": jid, "answer": ans, "status": "done"}, timeout=20)
        except Exception as e:  # noqa: BLE001
            log.error("warmup answer post failed #%s: %s", jid, e)
        log.info("warmup #%s phone=%s done", jid, phone)
        return
    # ── ⚡ פעולה תפעולית ישירה (stock-watch/תזכורת) → מסלול מהיר (sonnet), לא המוח הכבד ──
    if _is_op_task(job["question"]):
        op_prompt = build_panel_op_prompt(phone, job["question"])
        # stock-watch: אורי רק מזהה וריאציה (GET variations) ופולט [SWATCH] — הכתיבה הכבדה
        # עברה לכפתור דטרמיניסטי בקונסולה, אז זה מהיר ואמין. 150ש' + retry על כשל רגעי.
        ok, text, _sid = run_claude(op_prompt, fast=True, timeout_s=150)
        if not ok:
            log.warning("op job #%s failed (%s) — retrying once", jid, (text or "")[:200])
            time.sleep(3)
            ok, text, _sid = run_claude(op_prompt, fast=True, timeout_s=200)
        if not ok:
            # חושפים את הסיבה האמיתית בלוג (היתה מוסתרת מאחורי הודעה גנרית) — לאבחון
            log.error("op job #%s FAILED after retry: %s", jid, (text or "no-text")[:400])
        answer = (text or "").strip() if ok else "סליחה, תקלה רגעית בביצוע הפעולה — נסה שוב 🙏"
        try:
            requests.post(f"{BASE}/api/uri-bridge/answer", headers=H,
                          json={"id": jid, "answer": answer,
                                "status": "done" if ok else "error"}, timeout=20)
        except Exception as e:  # noqa: BLE001
            log.error("op answer post failed #%s: %s", jid, e)
        log.info("op job #%s -> %s (%d chars, fast)", jid, "done" if ok else "error", len(answer))
        return
    # סשן קיים לשיחה? המשך אותו (מהיר + זוכר) — אחרת סשן חדש עם הקשר מלא
    sess = _sessions.get(phone)
    sid = sess["sid"] if sess and time.time() - sess["at"] < SESSION_TTL else None
    prompt = (build_followup_prompt(phone, job["question"]) if sid
              else build_prompt(phone, job["question"]))
    ok, text, new_sid = run_claude(prompt, resume_sid=sid)
    if not ok and sid:
        # ה-resume נכשל (סשן פג/נמחק) — fallback לסשן חדש מלא
        log.info("job #%s resume failed — fresh session", jid)
        _sessions.pop(phone, None)
        ok, text, new_sid = run_claude(build_prompt(phone, job["question"]))
    # עומס רגעי בצד Anthropic (529/overloaded) — ננסה שוב עד פעמיים
    for attempt in (1, 2):
        if ok or not any(s in text for s in ("Overloaded", "overloaded", "529", "rate limit")):
            break
        wait = 25 * attempt
        log.info("job #%s overloaded — retry %d in %ds", jid, attempt, wait)
        time.sleep(wait)
        ok, text, new_sid = run_claude(prompt, resume_sid=sid)
    if ok and new_sid:
        _sessions[phone] = {"sid": new_sid, "at": time.time()}
    answer, notes = (_extract_notes(text) if ok else (text, []))
    try:
        requests.post(f"{BASE}/api/uri-bridge/answer", headers=H,
                      json={"id": jid, "answer": answer,
                            "status": "done" if ok else "error"}, timeout=20)
        for n in notes:
            requests.post(f"{BASE}/api/uri-bridge/note", headers=H,
                          json={"phone": phone, "text": n}, timeout=15)
        log.info("job #%s -> %s (%d chars, %d notes, resume=%s)",
                 jid, "done" if ok else "error", len(answer), len(notes), bool(sid))
    except Exception as e:  # noqa: BLE001
        log.error("answer post failed for #%s: %s", jid, e)


def _heartbeat_loop():
    """thread נפרד — שולח heartbeat כל 60ש בלי קשר ללולאת ה-jobs, כך ש-job ארוך
    (3 דק') לא יגרום ל'גשר לא מחובר' שקרי בקונסולה."""
    while True:
        try:
            requests.post(f"{BASE}/api/uri-bridge/ping", headers=H, timeout=10)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(60)


def main():
    if not KEY:
        log.error("URI_BRIDGE_KEY missing in .env")
        sys.exit(1)
    log.info("uri_bridge up — base=%s model=%s", BASE, MODEL)
    threading.Thread(target=_heartbeat_loop, daemon=True, name="uri-heartbeat").start()
    while True:
        try:
            r = requests.get(f"{BASE}/api/uri-bridge/jobs", headers=H, timeout=20)
            if r.status_code == 200:
                for job in r.json().get("jobs", []):
                    process(job)
            else:
                log.warning("jobs poll HTTP %s", r.status_code)
                time.sleep(30)
        except Exception as e:  # noqa: BLE001
            log.warning("poll error: %s", e)
            time.sleep(30)
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
