"""חשבונית ספק ← → כניסה למלאי ב-NewOrder (21/10/2026, אסי).

עובד מעלה תמונה/PDF של חשבונית או תעודת משלוח כנושא "חשבונית ספק" ב-GreenDay.
כאן:
  1. extract()  — Claude קורא מהמסמך: ספק, מספר מסמך, תאריך ושורות מוצר+כמות.
  2. find_ops() — מחפש ב-NewOrder את פעולות "כניסה למלאי" המתאימות: קודם לפי מספר המסמך
                  (כפי שהעובד הקליד בקופה), ואחרת לפי ספק + תאריך קרוב.
  3. map_lines() — Claude ממפה כל שורה בחשבונית לפריטים בכניסה (שמות שונים: "Redmi Note 17
                  256GB Purple" מול "Xiaomi Redmi Note 17 256GB 5G - Purple"). הכמויות עצמן
                  מחושבות כאן, דטרמיניסטית — המודל רק מצמיד שמות.
  4. compare()  — לכל שורה: נכנס / חלקי / לא נכנס / לבדיקה, ופריטים שנכנסו ולא בחשבונית.

התוצאה גלויה לבעלים בלבד (ראה main.py). מחירים לא נבדקים — בקופה העלות בכניסות רשומה 0.
"""
import base64
import json
import logging
import os
import re
from datetime import datetime, timedelta

logger = logging.getLogger("transfers.supplier_invoice")

MODEL = os.getenv("INVOICE_MODEL", "claude-opus-5-5")
STOCK_IN = 1                      # NewOrder operationType: כניסה למלאי
SEARCH_DAYS_BACK = 10             # חלון חיפוש כניסות סביב תאריך המסמך / ההעלאה (קריאות NewOrder מוגבלות 100/דקה)

_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "readable": {"type": "boolean"},
        "doc_type": {"type": "string", "enum": ["חשבונית מס", "תעודת משלוח", "חשבונית זיכוי", "הזמנה", "אחר"]},
        "supplier": {"type": "string"},
        "doc_number": {"type": "string"},
        "doc_date": {"type": "string"},
        "items": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "description": {"type": "string"},
                "code": {"type": "string"},
                "qty": {"type": "number"},
            },
            "required": ["description", "code", "qty"],
            "additionalProperties": False,
        }},
        "notes": {"type": "string"},
    },
    "required": ["readable", "doc_type", "supplier", "doc_number", "doc_date", "items", "notes"],
    "additionalProperties": False,
}

_EXTRACT_PROMPT = """זה מסמך ספק (חשבונית / תעודת משלוח) של רשת חנויות סלולר. חלץ ממנו:
- supplier: שם הספק כפי שמופיע בכותרת המסמך (לא שם הלקוח — הלקוח הוא Green Mobile / גרין מובייל / ארט מובייל).
- doc_number: מספר החשבונית או תעודת המשלוח (ספרות בלבד אם אפשר). אם יש גם מספר הזמנה — העדף את מספר המסמך עצמו.
- doc_date: תאריך המסמך בפורמט YYYY-MM-DD, או "" אם לא מופיע.
- items: כל שורת מוצר — description כפי שכתוב (דגם, נפח, צבע), code = מק"ט/ברקוד אם מופיע בשורה (אחרת ""), qty = כמות.
  אל תכלול שורות של משלוח, הנחה, מע"מ או סיכומים. אם אותו מוצר מופיע בכמה שורות — השאר כמו במסמך.
- readable: false אם המסמך מטושטש/חתוך מכדי לקרוא את שורות המוצרים בביטחון.
- notes: הערה קצרה בעברית אם משהו לא ברור (שורה שלא נקראה, כמות מוטלת בספק); אחרת "".
אל תנחש ערכים שלא רואים במסמך."""

_MAP_SCHEMA = {
    "type": "object",
    "properties": {
        "lines": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "line": {"type": "integer"},
                "stock_ids": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            },
            "required": ["line", "stock_ids", "confidence"],
            "additionalProperties": False,
        }},
    },
    "required": ["lines"],
    "additionalProperties": False,
}

_MAP_PROMPT = """התאם בין שורות חשבונית ספק לבין פריטים שנקלטו למלאי בקופה.
השמות שונים בניסוח (עברית/אנגלית, סדר מילים, "5G", קיצורי צבעים כמו Slv=Silver, Blk=Black) — התאם לפי
מותג + דגם + נפח אחסון/זיכרון + צבע. שורה בחשבונית שמתאימה לכמה פריטי קופה (למשל אותו דגם בכמה צבעים
כשהחשבונית לא מפרטת צבע) — החזר את כולם ב-stock_ids. אם אין פריט מתאים — stock_ids ריק.
confidence: high = דגם+נפח+צבע תואמים; medium = דגם+נפח תואמים, צבע לא מצוין/לא ודאי; low = ניחוש.
⛔ אל תצמיד דגם שונה (Pro ≠ Pro Max ≠ בסיסי, 128GB ≠ 256GB).
החזר שורה אחת לכל שורת חשבונית, לפי מספר השורה (line).

שורות החשבונית:
{inv}

פריטים שנקלטו בקופה (id · שם · כמות):
{stock}"""


def _client():
    import anthropic
    return anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY") or None, timeout=180.0, max_retries=2)


def _ask(content, schema) -> dict:
    """קריאה אחת ל-Claude עם פלט JSON מובנה. refusal / תשובה ריקה → RuntimeError."""
    c = _client()
    r = c.beta.messages.create(
        model=MODEL, max_tokens=8000,
        betas=["server-side-fallback-2026-07-01"],
        extra_body={"fallbacks": "default"},
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": content}],
    )
    if r.stop_reason == "refusal":
        raise RuntimeError("המודל סירב לעבד את המסמך")
    txt = next((b.text for b in r.content if getattr(b, "type", "") == "text"), "")
    if not txt:
        raise RuntimeError(f"תשובה ריקה מהמודל ({r.stop_reason})")
    return json.loads(txt)


def extract(data: bytes, mime: str) -> dict:
    b64 = base64.standard_b64encode(data).decode()
    if mime == "application/pdf":
        block = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}}
    elif mime in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        block = {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}}
    else:
        raise ValueError("סוג קובץ לא נתמך — צריך תמונה או PDF")
    out = _ask([block, {"type": "text", "text": _EXTRACT_PROMPT}], _EXTRACT_SCHEMA)
    out["items"] = [i for i in out.get("items") or [] if (i.get("qty") or 0) > 0]
    return out


# ── איתור הכניסה למלאי ──
def _digits(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def _norm(s) -> str:
    s = re.sub(r'["\'״׳`.,()\-]', " ", str(s or "").lower())
    s = re.sub(r"\b(בע\s?מ|בעמ|ltd|limited|inc|חברת)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _supplier_match(a, b) -> bool:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    ta, tb = set(a.split()), set(b.split())
    return bool(ta & tb and len(ta & tb) / min(len(ta), len(tb)) >= 0.5)


def doc_numbers_match(inv_no, op_no) -> bool:
    """מספר מסמך כפי שהוקלד בקופה ↔ כפי שנקרא מהחשבונית. העובד לפעמים מקליד חלק/תחילית
    או עם אפסים — שוויון ספרות, או אחד מכיל את השני כשיש לפחות 5 ספרות."""
    a, b = _digits(inv_no).lstrip("0"), _digits(op_no).lstrip("0")
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 5 and short in long_


def _parse_date(s):
    for f in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(s)[:19] if "T" in str(s) else str(s)[:10], f)
        except ValueError:
            continue
    return None


def find_ops(ex: dict, uploaded_at: datetime, client) -> tuple:
    """→ (ops, how). how: 'doc' (לפי מספר מסמך) / 'supplier' (ספק+תאריך) / ''."""
    anchor = _parse_date(ex.get("doc_date")) or uploaded_at
    frm = min(anchor, uploaded_at) - timedelta(days=SEARCH_DAYS_BACK)
    to = max(anchor, uploaded_at, datetime.now()) + timedelta(days=1)
    ops = []
    for page in range(1, 6):
        chunk = client.get_stock_operations(from_date=frm.strftime("%Y-%m-%d"), to_date=to.strftime("%Y-%m-%d"),
                                            page_size=200, page_num=page,
                                            items_for=lambda o: o.get("operationType") == STOCK_IN) or []
        ops += [o for o in chunk if o.get("operationType") == STOCK_IN]
        if len(chunk) < 200:
            break
    by_doc = [o for o in ops if doc_numbers_match(ex.get("doc_number"), o.get("documentNumber"))]
    if by_doc:
        return by_doc, "doc"
    sup = (ex.get("supplier") or "").strip()
    cand = [o for o in ops if _supplier_match(sup, ((o.get("supplier") or {}).get("name")))]
    if not cand:
        return [], ""
    # אותו ספק: הכניסה הקרובה ביותר לתאריך המסמך (עד 10 ימים), ואם יש כמה באותו יום — כולן
    def dist(o):
        d = _parse_date(o.get("createDate"))
        return abs((d - anchor).days) if d else 999
    best = min(dist(o) for o in cand)
    if best > 10:
        return [], ""
    return [o for o in cand if dist(o) == best], "supplier"


# ── התאמת שורות ──
def stock_lines(ops) -> list:
    """מאחד פריטים מכל הכניסות שנמצאו: id → {id, name, qty}."""
    agg = {}
    for o in ops:
        for it in o.get("stockItems") or []:
            k = str(it.get("id"))
            a = agg.setdefault(k, {"id": k, "name": it.get("name") or k, "qty": 0.0})
            a["qty"] += float(it.get("quantity") or 0)
    return list(agg.values())


def map_lines(items, stock) -> dict:
    if not items or not stock:
        return {}
    inv = "\n".join(f"{i + 1}. {x['description']}" + (f" (מק\"ט {x['code']})" if x.get("code") else "") + f" — כמות {x['qty']:g}"
                    for i, x in enumerate(items))
    st = "\n".join(f"{s['id']} · {s['name']} · {s['qty']:g}" for s in stock)
    res = _ask(_MAP_PROMPT.format(inv=inv, stock=st), _MAP_SCHEMA)
    valid = {s["id"] for s in stock}
    out = {}
    for ln in res.get("lines") or []:
        i = int(ln.get("line") or 0) - 1
        if 0 <= i < len(items):
            out[i] = {"ids": [x for x in ln.get("stock_ids") or [] if x in valid], "confidence": ln.get("confidence") or "low"}
    return out


def compare(items, stock, mapping) -> dict:
    """הקצאה דטרמיניסטית: כל פריט קופה מחולק בין שורות החשבונית שהוצמדו אליו, לפי הסדר.
    status לשורה: ok / partial / missing / unsure. extras = נכנס לקופה ולא בחשבונית."""
    left = {s["id"]: s["qty"] for s in stock}
    names = {s["id"]: s["name"] for s in stock}
    lines = []
    for i, it in enumerate(items):
        m = mapping.get(i) or {"ids": [], "confidence": "low"}
        need, got, used = float(it["qty"]), 0.0, []
        for sid in m["ids"]:
            take = min(left.get(sid, 0.0), need - got)
            if take > 0:
                left[sid] -= take
                got += take
                used.append({"id": sid, "name": names.get(sid, sid), "qty": take})
            if got >= need:
                break
        if not m["ids"]:
            st = "missing"
        elif m["confidence"] == "low":
            st = "unsure"
        elif got >= need:
            st = "ok"
        elif got > 0:
            st = "partial"
        else:
            st = "missing"
        lines.append({"description": it["description"], "code": it.get("code") or "", "qty": need, "got": got,
                      "status": st, "confidence": m["confidence"], "matched": used})
    extras = [{"id": k, "name": names[k], "qty": v} for k, v in left.items() if v > 0]
    n_ok = sum(1 for x in lines if x["status"] == "ok")
    overall = "ok" if lines and n_ok == len(lines) and not extras else ("mismatch" if lines else "empty")
    return {"lines": lines, "extras": extras, "overall": overall,
            "inv_qty": sum(x["qty"] for x in lines), "stock_qty": sum(s["qty"] for s in stock)}


def op_summary(ops) -> list:
    return [{"id": o.get("id"), "date": o.get("createDate"), "doc": o.get("documentNumber") or "",
             "employee": o.get("employee") or "", "supplier": (o.get("supplier") or {}).get("name") or "",
             "qty": float(o.get("totalQuantity") or 0)} for o in ops]
