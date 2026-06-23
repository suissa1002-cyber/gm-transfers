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
  • **הרשמה ל-Stock Watch** (לקוח שמבקש עדכון כשמוצר/צבע *שאזל* חוזר למלאי) →
    `curl -s -X POST {BASE}/api/uri-bridge/stock-watch -H "X-Bridge-Key: {KEY}" -H "Content-Type: application/json" -d '{{"phone":"{phone}","name":"<שם הלקוח>","sku":"<מק\"ט הוריאציה שאזלה>","product_name":"<שם המוצר + צבע>","product_url":"<קישור>","notify":true}}'`.
    את ה-`sku` משיגים מ-`{BASE}/api/uri-bridge/variations/<product_id>` (כל וריאציה מחזירה `sku`+`in_stock`+`color` — בחר את הצבע ש-`in_stock=false`). השרת ממיר אוטומטית SKU→neworder_id ורושם.
    ✅ `notify:true` — השרת שולח ללקוח **אוטומטית** אישור הרשמה ("רשמתי אותך, נעדכן כשחוזר למלאי"). אתה לא צריך לשלוח הודעה נפרדת — רק לאשר לאסי שנרשם.
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
פלט: נוסח ללקוח — בין [DRAFT] ל-[/DRAFT] בלבד, בלי מילה מיותרת בתוכו; מחוץ לבלוק
רק שורת עובדות אם נחוצה (+שורות [NOTE] אם יש מה ללמוד). בלי סיכומים."""


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


def run_claude(prompt: str, resume_sid: str = None, fast: bool = False) -> tuple:
    """מריץ claude headless. fast=True → תיקייה נקייה + מודל מהיר (משימת בוט/לקוח).
    מחזיר (ok, text, session_id)."""
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
        timeout = 90 if fast else JOB_TIMEOUT
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
