"""קטלוג ההרשאות של GreenOS — מקור אחד לשרת ולמסך המשתמשים.

כל פיצ'ר = תיבת סימון אחת במסך "משתמשים", וממופה לקידומות נתיב. הבדיקה בשרת
נעשית לפי הנתיב, כך שעובד בלי הרשאה נחסם גם אם ניגש ישירות לכתובת — ההסתרה
במסך היא נוחות, לא אבטחה.

⚠️ נתיב תחת /api/admin שלא ממופה כאן = **בעלים בלבד**. ברירת מחדל סגורה: נקודת
קצה חדשה שנשכח למפות לא נחשפת בטעות לעובדים — היא פשוט חסומה להם עד שממפים.
"""

# (מפתח, תווית בעברית, תיאור קצר, קידומות נתיב)
FEATURES = [
    ("orders", "הזמנות", "מסך ההזמנות, פרטי הזמנה, משלוחים ותשלומים מהירים", [
        "/api/admin/orders", "/api/admin/order-services", "/api/admin/order-plan",
        "/api/admin/scheduled-status", "/api/admin/pay", "/api/admin/wc-search",
        "/api/admin/wc-link", "/api/admin/repairs",
    ]),
    ("fraud", "טריאז' הונאות", "בדיקת הזמנות חשודות והמדד", ["/api/admin/fraud"]),
    ("whatsapp", "וואטסאפ ואלה", "שיחות לקוחות, שליחת הודעות, הבוט אלה", [
        "/api/admin/wa", "/api/admin/ella", "/api/admin/uri", "/api/admin/uri-cost",
    ]),
    ("crm", "לקוחות", "כרטיסי לקוח והיסטוריה", ["/api/admin/crm"]),
    ("calls", "שיחות טלפון", "מרכזיה ותיעוד שיחות", ["/api/admin/pbx"]),
    ("transfers", "העברות ומלאי", "תוכנית העברות, מלאי חי, הורדות מלאי וקטלוג", [
        "/api/admin/plan", "/api/admin/plan-fulfilled", "/api/admin/pos",
        "/api/admin/live-stock", "/api/admin/live-search", "/api/admin/serial-index",
        "/api/admin/serial-sync", "/api/admin/removals", "/api/admin/removals-backfill",
        "/api/admin/removals-ingest", "/api/admin/removals-status", "/api/admin/rebalance",
        "/api/admin/rebalance-scan", "/api/admin/misroute", "/api/admin/numeric-receivers",
        "/api/admin/relabel-receiver", "/api/admin/sold-reconcile", "/api/admin/stock-watch",
        "/api/admin/catalog", "/api/admin/catalog-refresh", "/api/admin/recommendations",
        "/api/admin/overview",
    ]),
    ("codes", "קודים דיגיטליים", "צפייה בקודים שהונפקו (Stellr)", ["/api/admin/stellr"]),
    ("coupons", "קופונים", "רשימת קופונים ומימושים", ["/api/admin/coupons"]),
    ("checks", "ניהול צ'קים", "לוח הצ'קים, דוחות והדפסה", ["/api/checks"]),
    ("schedule", "סידור עבודה", "סידור משמרות לסניפים", ["/api/schedule", "/api/admin/shift-dm-test"]),
    ("zap", "זאפ ומחירים", "השוואת מחירים ופיד זאפ", ["/api/admin/zap"]),
    ("services", "Green Care וטרייד-אין", "פוליסות Green Care ומחשבון טרייד-אין", [
        "/api/admin/greencare", "/api/admin/tradein", "/api/admin/service-flags",
    ]),
    ("addons", "מערכת תוספות (אביזרים)", "ניהול התוספות שמוצעות ללקוח בעמוד המוצר", ["/api/admin/addons"]),
    ("invoices", "חשבוניות", "חשבוניות ספקים", ["/api/admin/invoices", "/api/admin/invoices-today"]),
    ("updates", "עדכונים לסניפים", "פרסום עדכונים ושידורים לסניפים", [
        "/api/admin/branch-updates", "/api/admin/branch-update-image",
        "/api/admin/broadcast", "/api/admin/broadcasts",
    ]),
    ("reports", "דשבורד ודוחות", "מסך הבית, סקירות ונתוני מכירות", [
        "/api/admin/home", "/api/admin/brief", "/api/admin/digest",
        "/api/admin/sales", "/api/admin/sales-status", "/api/admin/sales-backfill",
        "/api/admin/sales-ingest", "/api/admin/special",
    ]),
    # "משימות סוכנים" (/api/admin/tasks) — בעלים בלבד: לא ממופה בכוונה (13/10: תווית "משימות צוות"
    # הטעתה והוענקה לעובד). משימות צוות אמיתיות = נושאים בצ'אט הצוות.
]

# פעולות רגישות — תיבה נפרדת מעבר להרשאת הפיצ'ר. נבדקות בתוך נקודת הקצה עצמה
# (הנתיב לבדו לא מספיק: שינוי סטטוס ל"בוטל" ושינוי ל"הושלם" עוברים באותו נתיב).
SENSITIVE = [
    ("orders.cancel", "ביטול וזיכוי הזמנות", "orders"),
    ("orders.trash", "העברת הזמנות לפח", "orders"),
    ("codes.issue", "הנפקת קודים דיגיטליים", "codes"),
    ("coupons.create", "יצירה וביטול של קופונים", "coupons"),
]

# מי מבצע את הבקשה הנוכחית — לייחוס הודעות וואטסאפ ("מי ענה").
# dict {"kind": user|console|bot|system|scheduled, "name": "..."}; None = לא ידוע
# (webhook/רקע) → כל פונקציית שליחה קובעת ברירת מחדל משלה (בוט/אוטומטי).
import contextvars as _cv
ACTOR = _cv.ContextVar("gm_actor", default=None)

FEATURE_KEYS = {f[0] for f in FEATURES}
SENSITIVE_KEYS = {s[0] for s in SENSITIVE}
ALL_KEYS = FEATURE_KEYS | SENSITIVE_KEYS

# קידומות ארוכות קודם — "/api/admin/removals-status" לפני "/api/admin/removals"
_PREFIXES = sorted(((p, f[0]) for f in FEATURES for p in f[3]), key=lambda x: -len(x[0]))


def feature_for_path(path: str):
    """הפיצ'ר שנדרש לנתיב. None = לא מוגן ע"י הרשאות משתמש (ציבורי/מכשיר).
    "owner" = נתיב ניהול שלא ממופה — בעלים בלבד."""
    p = (path or "").rstrip("/") or "/"
    for pref, key in _PREFIXES:
        if p == pref or p.startswith(pref + "/") or p.startswith(pref + "?"):
            return key
    if p.startswith("/api/admin/") or p == "/api/admin":
        return "owner"
    return None


def clean_perms(perms) -> list:
    """רק מפתחות מוכרים; פעולה רגישה בלי הפיצ'ר שלה — נזרקת."""
    s = {str(x) for x in (perms or []) if str(x) in ALL_KEYS}
    parent = {k: f for k, _, f in SENSITIVE}
    return sorted(k for k in s if k in FEATURE_KEYS or parent.get(k) in s)


def catalog() -> dict:
    """לממשק: הפיצ'רים והפעולות הרגישות, מקובצים."""
    return {
        "features": [{"key": k, "label": l, "desc": d} for k, l, d, _ in FEATURES],
        "sensitive": [{"key": k, "label": l, "feature": f} for k, l, f in SENSITIVE],
    }
