"""פאבל כותב את הוראות ההעלאה למוצר חדש (22/10/2026, אסי: "שלא נצטרך הערות של אודי").

במקום שאודי יכתוב "יש להזין תחת הקטגוריות... תכונות... מק״ט קופה...", פאבל מרכיב את ההוראה מ:
  • ההוראות שאודי כתב בעבר למוצרים דומים (אותו מותג / סוג מוצר) — סגנון, אילו תכונות לכל סוג;
  • מוצר דומה שכבר באתר — הקטגוריות והתכונות *הקיימות* (לא ממציאים קטגוריות), שורת האחריות;
  • נתוני הקופה — מק״ט ומחיר לכל וריאציה, צבעים, נפחים, RAM;
  • הספק בחשבונית (המילטון = יבואן רשמי Xiaomi וכו') לשורת האחריות כשהמוצר הדומה מאותו ספק.
"""
import logging
import re

import supplier_invoice as SI

logger = logging.getLogger("transfers.product_notes")

UDI = ("Udi Pizhaza", "אודי")

_SCHEMA = {
    "type": "object",
    "properties": {
        "note": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "doubts": {"type": "string"},
    },
    "required": ["note", "confidence", "doubts"],
    "additionalProperties": False,
}

_PROMPT = """אתה פאבל, אחראי איכות בחנות סלולר Green Mobile. כתוב את הוראות ההעלאה לאתר עבור מוצר חדש,
בדיוק בסגנון ובמבנה שאודי (מנהל התוכן) כותב — ראה דוגמאות. גלי (סוכנת ההעלאה) מבצעת את ההוראה מילה במילה.

המוצר: {name}
מותג: {brand}
ספק בחשבונית: {supplier}
אחריות (נקבעה ע״י הבעלים בשידור — סופי, להעתיק כמו שהוא, לא לשנות ולא לסמן לאימות): {warranty}
וריאציות (מהקופה):
{variants}

מוצר דומה שכבר באתר (לקטגוריות ולתכונות הקיימות — השתמש רק בקטגוריות ובערכים שמופיעים כאן או בדוגמאות):
{ref}

דוגמאות להוראות שאודי כתב למוצרים דומים:
{examples}

כללים:
- קטגוריות: רק קטגוריות קיימות (מהמוצר הדומה / מהדוגמאות). אל תמציא קטגוריה.
- תכונות: אותן תכונות שאודי נותן לסוג המוצר הזה (לסמארטפון: בחירת צבע, בחירת נפח אחסון, זיכרון RAM, יצרן, מפרט טכני, מה בערכה, גודל מסך אם ידוע וכו').
  ערכי צבע/נפח/RAM — מהווריאציות בלבד. מפרט טכני ותכולת ערכה — "יש להזין את המפרט המתאים" כמו אודי (לא להמציא מפרט).
- אם יש וריאציות — ציין "ליסט וריאציות" ומפה כל וריאציה למק״ט הקופה ולמחיר שלה (מחיר מהקופה, ₪).
- אחריות: בדיוק כפי שנקבעה למעלה. (רק אם לא נקבעה — לפי המוצר הדומה, ולסמן לאימות.)
- בלי המצאות. מה שלא ידוע — "לאמת" + doubts.
- ⛔ רק הווריאציות שברשימה למעלה (מהקופה). אל תוסיף וריאציה/צבע/נפח שלא ברשימה, גם לא "כרגע אין במלאי".
- note בעברית, בשורות קצרות כמו אודי. confidence=low אם אין מוצר דומה או דוגמאות רלוונטיות."""


def _tokens(s):
    return {w for w in re.findall(r"[a-z0-9]+|[א-ת]{2,}", (s or "").lower()) if len(w) > 1}


def examples_for(name, brand, rows, k=6):
    nt, bt = _tokens(name), (brand or "").lower()
    def score(r):
        t = _tokens(r["name"])
        return len(nt & t) * 2 + (3 if bt and bt in (r["name"] or "").lower() else 0)
    best = sorted(rows, key=score, reverse=True)
    out, seen = [], set()
    for r in best:
        if r["name"] in seen or score(r) <= 0:
            continue
        seen.add(r["name"])
        out.append(r)
        if len(out) >= k:
            break
    if len(out) < 3:   # מעט דוגמאות מאותו מותג — משלימים בהוראות כלליות (אותו מבנה)
        out += [r for r in rows if r["name"] not in seen][: 3 - len(out)]
    return out


def ref_product(name, brand, wc_get, group=None):
    """מוצר דומה באתר מאותו *סוג* (טלפון ↔ טלפון, לא אוזניות של אותו מותג): מועמדים מכמה חיפושים,
    ניקוד: יש תכונות נפח/RAM כשלמוצר יש (מכשיר) · אותו מותג · מילים משותפות בשם."""
    words = [w for w in re.findall(r"[A-Za-z0-9+]+", name or "") if w.lower() != (brand or "").lower()]
    is_device = bool(group and any(v.get("storage") or v.get("ram") for v in group.get("variants") or []))
    qs = [" ".join(words[:2]), words[0] if words else "", brand or ""]
    if is_device:
        qs += [f"{brand} סמארטפון".strip(), "סמארטפון"]
    cand = {}
    for q in dict.fromkeys(x for x in qs if x and len(x) >= 2):
        for p in wc_get("/products", {"search": q, "per_page": 10, "status": "publish", "_fields": "id,name,categories,attributes,short_description"}) or []:
            cand.setdefault(p.get("id"), p)
    nt, bl = _tokens(name), (brand or "").lower()
    def score(p):
        an = " ".join(a.get("name") or "" for a in p.get("attributes") or [])
        dev = "נפח אחסון" in an or "ram" in an.lower()
        sc = (8 if dev == is_device else -8) + (3 if bl and bl in (p.get("name") or "").lower() else 0)
        return sc + 2 * len(nt & _tokens(p.get("name")))
    best = sorted(cand.values(), key=score, reverse=True)
    return best[0] if best else None


def ref_summary(p) -> str:
    if not p:
        return "(לא נמצא מוצר דומה)"
    cats = ", ".join(c.get("name") for c in p.get("categories") or [])
    attrs = "\n".join(f"  {a.get('name')}: {', '.join(a.get('options') or [])[:200]}" for a in p.get("attributes") or [])
    sd = re.sub(r"<[^>]+>", " ", p.get("short_description") or "")
    war = re.search(r"אחריות:?\s*([^\n]{3,120})", sd)
    return (f"{p.get('name')} (WC {p.get('id')})\nקטגוריות: {cats}\nתכונות:\n{attrs}\n"
            f"אחריות בתיאור הקצר: {war.group(1).strip() if war else 'לא צוין'}")


def write_note(group, supplier, examples, ref_text) -> dict:
    vs = "\n".join(
        f"- {' · '.join(x for x in [v.get('color'), v.get('ram') and v['ram'] + ' RAM', v.get('storage')] if x) or v.get('desc')}"
        f" — מק״ט {v.get('sku') or 'לא בקופה'}" + (f" — מחיר בקופה ₪{v['price']:g}" if v.get("price") else " — מחיר: לאמת")
        for v in group["variants"])
    ex = "\n\n".join(f"### {r['name']}" + (f" (מחיר {r['price']:g})" if r.get("price") else "") + f"\n{(r['body'] or '')[:1200]}"
                     for r in examples) or "(אין דוגמאות)"
    return SI._ask(_PROMPT.format(name=group["name"], brand=group.get("brand") or "", supplier=supplier or "",
                                  warranty=group.get("warranty") or "לא נקבעה",
                                  variants=vs, ref=ref_text, examples=ex), _SCHEMA)
