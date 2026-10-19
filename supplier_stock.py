"""
📦 GreenDay · סנכרון מלאי ספקים (19/10/2026, אסי)

ספק (יבואן, דרופשיפ "בית לקוח") שולח דוח זמינות שבועי (xlsx/csv). המודול:
  1. מפרסר את הדוח לפי קונפיג הספק (אותיות עמודות; שורת נתונים = יש ברקוד + זמינות חוקית).
  2. מסווג כל שורה לפי כללי הבעלים (ראה classify).
  3. מתאים ברקוד ספק → יחידה באתר (וריאציה / מוצר פשוט). הצעות אוטומטיות (SKU==ברקוד,
     SKU==דגם, זרעים ידניים) אינן מאושרות עד שאדם מאשר — ⛔ אין סנכרון כשיש שורה לא מאושרת.
  4. בונה תוכנית (dry-run) מול המצב החי באתר, סיכום "חכם" והשוואה לדוח הקודם.
  5. מבצע: batch ל-WC (stock_status + span של זמן אספקה בתיאור הוריאציה) עם התקדמות חיה
     ברשומת הריצה, ניקוי מטמון לעמודים חיים, והתראת טלגרם.

⛔ בטיחות: רק stock_status + span ה-gm-lead (ומטא _gm_lead_days למוצר פשוט). אף פעם לא מחיר / סטטוס פרסום.
⛔ יחידה עם מק"ט קופה (6 ספרות שמתחיל ב-5) + ניהול מלאי + כמות>0 = מלאי פיזי בסניף — לא נוגעים.

ה-I/O מול WC מוזרק (מחלקת WC / אובייקט עם get/post) — כך הבדיקות רצות בלי רשת.
"""

import base64
import csv
import html
import io
import logging
import os
import re
import time
from datetime import datetime, timezone

import db

logger = logging.getLogger("transfers.supplier_stock")

LEAD_SPAN = '<span class="gm-lead" data-days="{n}"></span>'
_LEAD_RE = re.compile(r'\s*<span\b[^>]*class=["\'][^"\']*\bgm-lead\b[^"\']*["\'][^>]*>\s*</span>', re.I)
_LEAD_DAYS_RE = re.compile(r'<span\b[^>]*class=["\'][^"\']*\bgm-lead\b[^"\']*["\'][^>]*data-days=["\'](\d+)["\']', re.I)
_POS_SKU_RE = re.compile(r"^5\d{5}$")
LEAD_META = "_gm_lead_days"
STALE_DAYS = 8          # דוח ישן מזה → תג אזהרה
RUN_STALE_SEC = 1800    # ריצה "running" ישנה מזה (שרת אותחל באמצע) — לא חוסמת ריצה חדשה

ISHPAR_KEY = "ishpar_dragon"
ISHPAR_NAME = "ישפאר — Dragon בית לקוח"
DEFAULT_CONFIG = {
    # אותיות עמודות בקובץ (A=1). זיהוי כותרות אוטומטי: שורת נתונים = ברקוד + זמינות מוכרת
    "columns": {"model": "A", "desc": "B", "price": "C", "barcode": "E", "avail": "F", "note": "G"},
    "in_values": ["יש מלאי"],
    "out_values": ["אין מלאי"],
    "note_keep": ["בודדות"],                       # "אין מלאי" + הערה שמכילה → ממשיכים למכור
    "note_lead_regex": r"(\d+)\s*ימי עסקים",        # "אין מלאי" + הערה תואמת → במלאי + זמן אספקה N
    "require_tag": True,                            # רק מוצרים עם תג "בית לקוח"
    "tag_id": 3630,
}
# זרעים ידניים לישפאר (אסי): ברקוד ספק → (מוצר, וריאציה|None). 46337 = מוצר פשוט.
ISHPAR_SEEDS = {
    "7290114432368": (46373, 46414),
    "7290114430210": (46337, None),
    "7290114433242": (46322, 46326),
    "7290114432382": (46278, 46282),
    "7290114432375": (46278, 46283),
    "7290114432436": (46212, 46222),
    "7290114430678": (46341, 46342),
    "7290017725925": (46243, 46257),
}

GROUPS = [  # סדר התצוגה בתצוגה המקדימה
    ("blocked", "לא מותאם / לא מאושר — חוסם סנכרון"),
    ("missing_wc", "מותאם ליחידה שלא נמצאה באתר — חוסם"),
    ("close", "ייסגר → אזל"),
    ("open", "ייפתח → במלאי"),
    ("lead", "זמן אספקה (הוספה / שינוי / הסרה)"),
    ("protected", "מלאי פיזי בסניף — לא נסגר"),
    ("unknown", "הערה לא מוכרת — לא ישתנה, החלטה ידנית"),
    ("untagged", "ללא תג בית לקוח — מדולג"),
    ("unchanged", "ללא שינוי"),
    ("ignored", "לא נמכר אצלנו"),
]


# ─────────────────────────── פרסור ───────────────────────────
def col_idx(letter) -> int:
    """'A'→0, 'E'→4, 'AA'→26. מספר (1-based) מתקבל גם."""
    s = str(letter or "").strip().upper()
    if s.isdigit():
        return int(s) - 1
    n = 0
    for ch in s:
        if not ("A" <= ch <= "Z"):
            raise ValueError(f"עמודה לא חוקית: {letter}")
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def norm_text(v) -> str:
    return re.sub(r"\s+", " ", str(v if v is not None else "")).strip()


def norm_barcode(v) -> str:
    """ברקוד → מחרוזת ספרות. int / float (7.29e12) / str ('7290…', ' 7290… ')."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return ""
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v == v and v > 0 else ""
    s = str(v).strip()
    if re.fullmatch(r"\d+\.0+", s):
        s = s.split(".")[0]
    return re.sub(r"\D", "", s)


def _read_table(content: bytes, file_name: str) -> list:
    name = (file_name or "").lower()
    if name.endswith(".csv") or name.endswith(".txt"):
        for enc in ("utf-8-sig", "cp1255", "latin-1"):
            try:
                text = content.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        return [list(r) for r in csv.reader(io.StringIO(text), dialect)]
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    ws = wb.worksheets[0]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    wb.close()
    return rows


def parse_report(content: bytes, file_name: str, config: dict) -> list:
    """שורות נתונים בלבד: יש ברקוד וזמינות ∈ in_values ∪ out_values. כותרות/קבוצות מדולגות."""
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    cols = {**DEFAULT_CONFIG["columns"], **(cfg.get("columns") or {})}
    ci = {k: col_idx(v) for k, v in cols.items() if v not in (None, "")}
    valid = {norm_text(x) for x in (cfg.get("in_values") or []) + (cfg.get("out_values") or [])}
    out = []
    seen = set()
    for i, r in enumerate(_read_table(content, file_name), start=1):
        def g(k):
            j = ci.get(k)
            return r[j] if j is not None and j < len(r) else None
        bc = norm_barcode(g("barcode"))
        av = norm_text(g("avail"))
        if not bc or av not in valid:
            continue
        if bc in seen:      # ברקוד כפול בדוח — השורה הראשונה קובעת
            continue
        seen.add(bc)
        price = g("price")
        out.append({"row": i, "model": norm_text(g("model")), "desc": norm_text(g("desc")),
                    "price": price if isinstance(price, (int, float)) else norm_text(price),
                    "barcode": bc, "avail": av, "note": norm_text(g("note"))})
    return out


# ─────────────────────────── כללים ───────────────────────────
def classify(row: dict, config: dict) -> dict:
    """kind: instock | keep | lead | oos | unknown.  status: המצב הרצוי באתר (None = לא לשנות).
    lead: ימי אספקה (int) או None (=להסיר)."""
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    av, note = norm_text(row.get("avail")), norm_text(row.get("note"))
    if av in {norm_text(x) for x in cfg.get("in_values") or []}:
        return {"kind": "instock", "status": "instock", "lead": None}
    if not note:
        return {"kind": "oos", "status": "outofstock", "lead": None}
    if any(k and k in note for k in cfg.get("note_keep") or []):
        return {"kind": "keep", "status": "instock", "lead": None}
    rx = cfg.get("note_lead_regex")
    if rx:
        m = re.search(rx, note)
        if m:
            try:
                return {"kind": "lead", "status": "instock", "lead": int(m.group(1))}
            except (IndexError, ValueError):
                pass
    return {"kind": "unknown", "status": None, "lead": None}


def report_status(row: dict, config: dict) -> str:
    """זמינות "אפקטיבית" לפי הדוח בלבד (להשוואה שבועית). הערה לא מוכרת = כמו אזל."""
    return classify(row, config)["status"] or "outofstock"


def lead_days(desc) -> int:
    m = _LEAD_DAYS_RE.search(desc or "")
    return int(m.group(1)) if m else None


def lead_apply(desc, days) -> str:
    """מסיר כל span של gm-lead ומוסיף אחד חדש אם days. אידמפוטנטי."""
    base = _LEAD_RE.sub("", desc or "").rstrip()
    if days:
        return base + LEAD_SPAN.format(n=int(days))
    return base


def is_pos_sku(sku) -> bool:
    return bool(_POS_SKU_RE.match(str(sku or "").strip()))


def is_physical(unit: dict) -> bool:
    """מלאי פיזי בסניפים: מק"ט קופה + ניהול מלאי + כמות>0."""
    try:
        q = float(unit.get("stock_quantity") or 0)
    except (TypeError, ValueError):
        q = 0
    return is_pos_sku(unit.get("sku")) and bool(unit.get("manage_stock")) and q > 0


# ─────────────────────────── WooCommerce ───────────────────────────
class WC:
    """לקוח WC REST מינימלי. ⚠️ UA דמוי-דפדפן — Cloudflare חוסם UA של ספריות."""
    UA = "Mozilla/5.0 (compatible; GreenOS/1.0; greenmobile.co.il)"

    def __init__(self, base, key, secret):
        self.base, self.auth = base.rstrip("/"), (key, secret)

    @classmethod
    def from_env(cls):
        u = os.getenv("WC_STORE_URL", "").rstrip("/")
        k, s = os.getenv("WC_CONSUMER_KEY", ""), os.getenv("WC_CONSUMER_SECRET", "")
        return cls(u, k, s) if (u and k and s) else None

    def _req(self, method, path, params=None, body=None, timeout=60):
        import requests as _rq
        r = _rq.request(method, f"{self.base}/wp-json/wc/v3{path}", params=params, json=body, auth=self.auth,
                        headers={"User-Agent": self.UA}, timeout=timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"WC {method} {path} → {r.status_code}: {r.text[:300]}")
        return r.json()

    def get(self, path, params=None):
        return self._req("GET", path, params=params)

    def post(self, path, body):
        return self._req("POST", path, body=body, timeout=120)


def _attrs_label(attrs) -> str:
    return " · ".join(str(a.get("option")) for a in (attrs or []) if a.get("option"))


def _chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


_PCACHE: dict = {}       # pid → (ts, product) — מטמון קצר לתצוגה מקדימה
_PCACHE_TTL = 120


def fetch_products(wc, pids, fresh=False) -> dict:
    """pid → {id,name,type,status,permalink,tags,sku,stock_status,manage_stock,stock_quantity,lead,
    variations:{vid: {...}}}. מוצר שלא נמצא — לא יופיע."""
    now = time.time()
    out, need = {}, []
    for p in {int(x) for x in pids if x}:
        c = _PCACHE.get(p)
        if c and not fresh and now - c[0] < _PCACHE_TTL:
            out[p] = c[1]
        else:
            need.append(p)
    for ch in _chunks(sorted(need), 50):
        arr = wc.get("/products", {"include": ",".join(map(str, ch)), "per_page": 100, "status": "any"}) or []
        for p in arr:
            meta = {m.get("key"): m.get("value") for m in (p.get("meta_data") or [])}
            try:
                ml = int(meta.get(LEAD_META)) if str(meta.get(LEAD_META) or "").strip() else None
            except (TypeError, ValueError):
                ml = None
            prod = {"id": int(p["id"]), "name": p.get("name") or "", "type": p.get("type") or "simple",
                    "status": p.get("status") or "", "permalink": p.get("permalink") or "",
                    "tags": [int(t.get("id")) for t in (p.get("tags") or []) if t.get("id")],
                    "sku": p.get("sku") or "", "stock_status": p.get("stock_status") or "",
                    "manage_stock": bool(p.get("manage_stock")), "stock_quantity": p.get("stock_quantity"),
                    "lead": ml, "variations": {}}
            if prod["type"] == "variable":
                page = 1
                while True:
                    vs = wc.get(f"/products/{prod['id']}/variations", {"per_page": 100, "page": page}) or []
                    for v in vs:
                        prod["variations"][int(v["id"])] = {
                            "id": int(v["id"]), "sku": v.get("sku") or "", "label": _attrs_label(v.get("attributes")),
                            "stock_status": v.get("stock_status") or "",
                            "manage_stock": v.get("manage_stock") is True, "stock_quantity": v.get("stock_quantity"),
                            "description": v.get("description") or "", "lead": lead_days(v.get("description")),
                            "status": v.get("status") or ""}
                    if len(vs) < 100:
                        break
                    page += 1
            _PCACHE[prod["id"]] = (now, prod)
            out[prod["id"]] = prod
    return out


def cache_clear():
    _PCACHE.clear()


def sku_lookup(wc, codes) -> dict:
    """קוד (ברקוד/דגם) → רשימת יחידות שה-SKU שלהן שווה לו (וריאציות כלולות ב-sku= של WC)."""
    found = {}
    codes = [c for c in {str(c).strip() for c in codes} if c]
    for ch in _chunks(sorted(codes), 40):
        arr = wc.get("/products", {"sku": ",".join(ch), "per_page": 100, "status": "any"}) or []
        for p in arr:
            sku = str(p.get("sku") or "").strip()
            if not sku:
                continue
            if p.get("type") == "variation" or p.get("parent_id"):
                u = {"pid": int(p.get("parent_id")), "vid": int(p["id"]), "label": p.get("name") or ""}
            else:
                u = {"pid": int(p["id"]), "vid": None, "label": p.get("name") or ""}
            for c in ch:
                if c.upper() == sku.upper():
                    found.setdefault(c, []).append(u)
    return found


def wc_search(wc, q, limit=10) -> list:
    """חיפוש מוצר לפי שם (התאמה ידנית) + הוריאציות שלו עם המאפיינים."""
    arr = wc.get("/products", {"search": q, "per_page": limit, "status": "any"}) or []
    out = []
    for p in arr:
        item = {"id": int(p["id"]), "name": p.get("name") or "", "type": p.get("type"), "status": p.get("status"),
                "sku": p.get("sku") or "", "permalink": p.get("permalink") or "",
                "stock_status": p.get("stock_status") or "",
                "tags": [t.get("name") for t in (p.get("tags") or [])], "variations": []}
        if p.get("type") == "variable":
            vs = wc.get(f"/products/{p['id']}/variations", {"per_page": 100}) or []
            item["variations"] = [{"id": int(v["id"]), "label": _attrs_label(v.get("attributes")),
                                   "sku": v.get("sku") or "", "stock_status": v.get("stock_status") or ""} for v in vs]
        out.append(item)
    return out


def resolve_unit(products, pid, vid):
    """(unit, product) או (None, product|None)."""
    p = products.get(int(pid)) if pid else None
    if not p:
        return None, None
    if vid:
        v = p["variations"].get(int(vid))
        if not v:
            return None, p
        return {"kind": "variation", "pid": p["id"], "vid": v["id"], "sku": v["sku"], "label": v["label"],
                "stock_status": v["stock_status"], "manage_stock": v["manage_stock"],
                "stock_quantity": v["stock_quantity"], "description": v["description"], "lead": v["lead"]}, p
    if p["type"] == "variable":
        return None, p     # מותאם להורה של מוצר משתנה בלי וריאציה — לא חוקי
    return {"kind": "simple", "pid": p["id"], "vid": None, "sku": p["sku"], "label": "",
            "stock_status": p["stock_status"], "manage_stock": p["manage_stock"],
            "stock_quantity": p["stock_quantity"], "description": "", "lead": p["lead"]}, p


# ─────────────────────────── ספקים + מיפוי ───────────────────────────
def ensure_seed() -> dict:
    """ספק #1 (ישפאר) + זרעי מיפוי — פעם אחת. מחזיר את הספק."""
    s = db.ss_supplier_by_key(ISHPAR_KEY)
    if s:
        return s
    sid = db.ss_supplier_save(ISHPAR_NAME, dict(DEFAULT_CONFIG), skey=ISHPAR_KEY)
    for bc, (pid, vid) in ISHPAR_SEEDS.items():
        db.ss_map_upsert(sid, bc, wc_product_id=pid, wc_variation_id=vid, confirmed=0, ignored=0, source="seed")
    return db.ss_supplier_get(sid)


def suggest_mappings(sid, rows, wc, report_id=None) -> dict:
    """רושם כל ברקוד בדוח בטבלת המיפוי (first_seen_report), ומציע התאמה לשורות בלי יחידה:
    (א) SKU == ברקוד, (ב) SKU == קוד דגם. רק התאמה יחידה; כמה התאמות → נשאר פתוח עם הערה.
    ⛔ ההצעה אינה מאושרת — confirmed=0 עד אישור אדם."""
    maps = db.ss_map_all(sid)
    stats = {"new": 0, "by_sku": 0, "by_model": 0, "ambiguous": 0}
    for r in rows:
        m = maps.get(r["barcode"])
        if not m:
            db.ss_map_upsert(sid, r["barcode"], model=r["model"], description=r["desc"], confirmed=0, ignored=0,
                             first_seen_report=report_id)
            stats["new"] += 1
        else:
            f = {"model": r["model"], "description": r["desc"]}
            if not m.get("first_seen_report") and report_id:
                f["first_seen_report"] = report_id
            db.ss_map_upsert(sid, r["barcode"], **f)
    maps = db.ss_map_all(sid)
    open_rows = [r for r in rows if not maps[r["barcode"]].get("ignored") and not maps[r["barcode"]].get("wc_product_id")]
    if not open_rows or wc is None:
        return stats
    by_bc = sku_lookup(wc, [r["barcode"] for r in open_rows])
    left = []
    for r in open_rows:
        hits = by_bc.get(r["barcode"]) or []
        if len(hits) == 1:
            h = hits[0]
            db.ss_map_upsert(sid, r["barcode"], wc_product_id=h["pid"], wc_variation_id=h["vid"], label=h["label"],
                             source="sku", confirmed=0, note="")
            stats["by_sku"] += 1
        elif len(hits) > 1:
            db.ss_map_upsert(sid, r["barcode"], note=f"{len(hits)} יחידות באתר עם אותו SKU — לבחור ידנית")
            stats["ambiguous"] += 1
        else:
            left.append(r)
    by_model = sku_lookup(wc, [r["model"] for r in left if r.get("model")]) if left else {}
    for r in left:
        hits = by_model.get(r.get("model") or "") or []
        if len(hits) == 1:
            h = hits[0]
            db.ss_map_upsert(sid, r["barcode"], wc_product_id=h["pid"], wc_variation_id=h["vid"], label=h["label"],
                             source="model", confirmed=0, note="")
            stats["by_model"] += 1
    return stats


CERTAIN_SOURCES = ("sku", "seed")   # "התאמות וודאיות": SKU זהה לברקוד + התאמות ידניות שהבעלים קבע מראש


def map_action(sid, barcode, action, by, pid=None, vid=None, label="") -> dict:
    """confirm | ignore | unignore | set (בחירה ידנית + אישור) | clear (ביטול התאמה)."""
    maps = db.ss_map_all(sid)
    m = maps.get(str(barcode))
    if not m:
        raise ValueError("הברקוד לא מוכר לספק הזה")
    now = db.now_iso()
    if action == "confirm":
        if not m.get("wc_product_id"):
            raise ValueError("אין התאמה לאשר — בחרו מוצר קודם")
        db.ss_map_upsert(sid, barcode, confirmed=1, ignored=0, confirmed_by=by, confirmed_at=now)
    elif action == "ignore":
        db.ss_map_upsert(sid, barcode, ignored=1, confirmed=0, confirmed_by=by, confirmed_at=now)
    elif action == "unignore":
        db.ss_map_upsert(sid, barcode, ignored=0)
    elif action == "set":
        if not pid:
            raise ValueError("חסר מוצר")
        db.ss_map_upsert(sid, barcode, wc_product_id=int(pid), wc_variation_id=int(vid) if vid else None,
                         label=label or "", source="manual", confirmed=1, ignored=0, confirmed_by=by,
                         confirmed_at=now, note="")
    elif action == "clear":
        db.ss_map_upsert(sid, barcode, wc_product_id=None, wc_variation_id=None, label="", source="",
                         confirmed=0, confirmed_by=None, confirmed_at=None)
    else:
        raise ValueError("פעולה לא מוכרת")
    return db.ss_map_all(sid).get(str(barcode))


def confirm_certain(sid, by, barcodes=None) -> int:
    """אישור גורף של ההתאמות הוודאיות (SKU==ברקוד, וזרעים ידניים). לא נוגע בהצעות לפי דגם."""
    n = 0
    for b, m in db.ss_map_all(sid).items():
        if barcodes is not None and b not in barcodes:
            continue
        if m.get("wc_product_id") and not m.get("confirmed") and not m.get("ignored") \
                and m.get("source") in CERTAIN_SOURCES:
            db.ss_map_upsert(sid, b, confirmed=1, confirmed_by=by, confirmed_at=db.now_iso())
            n += 1
    return n


# ─────────────────────────── תוכנית (dry-run) ───────────────────────────
def build_plan(config: dict, rows: list, maps: dict, products: dict, has_prev: bool = False,
               report_id=None) -> dict:
    """שורה לכל רשומת דוח: מה המצב באתר, מה יהיה, ולמה. לא נוגע ברשת."""
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    tag_id = int(cfg.get("tag_id") or 0)
    out = []
    for r in rows:
        c = classify(r, cfg)
        m = maps.get(r["barcode"]) or {}
        pr = {**{k: r.get(k) for k in ("barcode", "model", "desc", "avail", "note", "price")},
              "kind": c["kind"], "want_status": c["status"], "want_lead": c["lead"],
              "map_confirmed": bool(m.get("confirmed")), "map_ignored": bool(m.get("ignored")),
              "map_source": m.get("source") or "", "map_note": m.get("note") or "",
              "pid": m.get("wc_product_id"), "vid": m.get("wc_variation_id"),
              "wc_name": "", "wc_label": m.get("label") or "", "permalink": "", "pstatus": "", "sku": "",
              "cur_status": "", "new_status": "", "cur_lead": None, "new_lead": None,
              "status_change": False, "lead_change": "", "group": "", "msg": "",
              "is_new": bool(has_prev and report_id and m.get("first_seen_report") == report_id)}
        unit, prod = (resolve_unit(products, m.get("wc_product_id"), m.get("wc_variation_id"))
                      if m.get("wc_product_id") else (None, None))
        if prod:
            pr.update(wc_name=prod["name"], permalink=prod["permalink"], pstatus=prod["status"])
        if unit:
            pr.update(sku=unit["sku"], wc_label=unit["label"] or pr["wc_label"], cur_status=unit["stock_status"],
                      cur_lead=unit["lead"], unit_kind=unit["kind"], manage_stock=bool(unit.get("manage_stock")))
        if m.get("ignored"):
            pr["group"], pr["msg"] = "ignored", "סומן 'לא נמכר אצלנו'"
        elif not m.get("wc_product_id"):
            pr["group"], pr["msg"] = "blocked", m.get("note") or "אין התאמה לאתר"
        elif not unit:
            pr["group"] = "missing_wc"
            pr["msg"] = ("היחידה לא נמצאה באתר (נמחקה?)" if not prod else
                         "מוצר משתנה — צריך לבחור וריאציה" if prod["type"] == "variable" and not m.get("wc_variation_id")
                         else "הוריאציה לא נמצאה במוצר")
        elif not m.get("confirmed"):
            pr["group"] = "blocked"
            pr["msg"] = {"sku": "הצעה: SKU זהה לברקוד — ממתין לאישור", "model": "הצעה: SKU = קוד הדגם — ממתין לאישור",
                         "seed": "התאמה ידנית מוגדרת מראש — ממתין לאישור"}.get(m.get("source"), "ממתין לאישור")
        elif cfg.get("require_tag", True) and tag_id and tag_id not in (prod.get("tags") or []):
            pr["group"], pr["msg"] = "untagged", "למוצר אין תג 'בית לקוח' — מדולג (ניתן לשנות בהגדרות הספק)"
        elif is_physical(unit):
            pr["group"], pr["msg"] = "protected", "מלאי פיזי בסניף — לא נסגר"
            pr["new_status"], pr["new_lead"] = unit["stock_status"], unit["lead"]
        elif c["kind"] == "unknown":
            pr["group"], pr["msg"] = "unknown", "הערה לא מוכרת — לא שונה אוטומטית"
            pr["new_status"], pr["new_lead"] = unit["stock_status"], unit["lead"]
        else:
            pr["new_status"], pr["new_lead"] = c["status"], c["lead"]
            pr["status_change"] = unit["stock_status"] != c["status"]
            cl, nl = unit["lead"], c["lead"]
            pr["lead_change"] = ("" if cl == nl else "add" if cl is None else "remove" if nl is None else "change")
            if pr["status_change"]:
                pr["group"] = "close" if c["status"] == "outofstock" else "open"
            elif pr["lead_change"]:
                pr["group"] = "lead"
            else:
                pr["group"] = "unchanged"
            if c["kind"] == "keep":
                pr["msg"] = "יחידות בודדות — ממשיכים למכור"
            elif c["kind"] == "lead":
                pr["msg"] = f"זמן אספקה {c['lead']} ימי עסקים"
        out.append(pr)
    blocked = [p for p in out if p["group"] in ("blocked", "missing_wc")]
    return {"rows": out, "blocked": bool(blocked), "n_blocked": len(blocked),
            "summary": summarize(out, products)}


def _disp(p) -> str:
    n = p.get("wc_name") or p.get("desc") or p.get("model") or p.get("barcode")
    return f"{n} · {p['wc_label']}" if p.get("wc_label") else n


def summarize(rows: list, products: dict = None, results: dict = None) -> dict:
    """הסיכום ה"חכם" — אותם מונים לתצוגה מקדימה ("צפוי") ולאחר ביצוע (results = barcode→{ok,error}).
    lists: מפתח מונה → ברקודים (ה-UI פותח את השורות שמאחורי כל מונה)."""
    results = results or {}
    failed = {b for b, r in results.items() if not r.get("ok")}

    def eff(p):   # שורה שנכשלה בביצוע לא נספרת כשינוי
        return p["barcode"] not in failed

    L = {k: [] for k in ("matched", "unmatched", "ignored", "back", "down", "lead_set", "lead_removed",
                         "unchanged", "protected", "unknown", "untagged", "errors", "new")}
    for p in rows:
        g, b = p["group"], p["barcode"]
        if g == "ignored":
            L["ignored"].append(b)
        elif g in ("blocked", "missing_wc"):
            L["unmatched"].append(b)
        else:
            L["matched"].append(b)
        if p.get("is_new"):
            L["new"].append(b)
        if g == "protected":
            L["protected"].append(b)
        elif g == "unknown":
            L["unknown"].append(b)
        elif g == "untagged":
            L["untagged"].append(b)
        elif g in ("open", "close", "lead", "unchanged"):
            if b in failed:
                L["errors"].append(b)
                continue
            if g == "open":
                L["back"].append(b)
            elif g == "close":
                L["down"].append(b)
            if p.get("lead_change") in ("add", "change"):
                L["lead_set"].append(b)
            elif p.get("lead_change") == "remove":
                L["lead_removed"].append(b)
            if g == "unchanged":
                L["unchanged"].append(b)
    # ליסטים שהושפעו + ליסט שלם שאזל/חזר (לפי כל הוריאציות של המוצר, לא רק אלה שבדוח)
    touched, whole_oos, whole_back = {}, [], []
    after = {}
    for p in rows:
        if p["group"] in ("open", "close", "lead") and eff(p) and p.get("pid"):
            touched[int(p["pid"])] = p.get("pstatus") or ""
            if p.get("status_change"):
                after[(int(p["pid"]), int(p["vid"]) if p.get("vid") else None)] = p["new_status"]
    for pid, pst in touched.items():
        prod = (products or {}).get(pid)
        if not prod:
            continue
        if prod.get("variations"):
            before_st = {vid: v["stock_status"] for vid, v in prod["variations"].items()}
            after_st = {vid: after.get((pid, vid), st) for vid, st in before_st.items()}
        else:
            before_st = {None: prod.get("stock_status")}
            after_st = {None: after.get((pid, None), prod.get("stock_status"))}
        b_all = all(s == "outofstock" for s in before_st.values())
        a_all = all(s == "outofstock" for s in after_st.values())
        if a_all and not b_all:
            whole_oos.append({"pid": pid, "name": prod["name"], "status": pst})
        elif b_all and not a_all:
            whole_back.append({"pid": pid, "name": prod["name"], "status": pst})
    names = {p["barcode"]: _disp(p) for p in rows}
    return {"counts": {k: len(v) for k, v in L.items()} | {"in_report": len(rows),
                                                           "lead_any": len(L["lead_set"]) + len(L["lead_removed"])},
            "lists": L, "names": {k: [names[b] for b in L[k]] for k in ("back", "down", "lead_set", "lead_removed",
                                                                         "errors", "new", "unknown", "protected")},
            "listings": {"live": sum(1 for s in touched.values() if s == "publish"),
                         "draft": sum(1 for s in touched.values() if s != "publish"),
                         "whole_oos": whole_oos, "whole_back": whole_back},
            "errors": {b: results[b].get("error") for b in failed}}


def diff_reports(prev_rows: list, cur_rows: list, config: dict, prev_date: str = "") -> dict:
    """השוואה שבועית לפי הדוח עצמו: חזרו / ירדו / חדשים בדוח / נעלמו מהדוח."""
    if prev_rows is None:
        return None
    P = {r["barcode"]: r for r in prev_rows}
    C = {r["barcode"]: r for r in cur_rows}
    lite = lambda r: {"barcode": r["barcode"], "model": r.get("model"), "desc": r.get("desc")}  # noqa: E731
    back, down = [], []
    for b, r in C.items():
        if b in P:
            ps, cs = report_status(P[b], config), report_status(r, config)
            if ps == "outofstock" and cs == "instock":
                back.append(lite(r))
            elif ps == "instock" and cs == "outofstock":
                down.append(lite(r))
    return {"prev_date": prev_date, "back": back, "down": down,
            "new": [lite(r) for b, r in C.items() if b not in P],
            "gone": [lite(r) for b, r in P.items() if b not in C]}


# ─────────────────────────── ביצוע ───────────────────────────
def build_payloads(rows: list) -> list:
    """רק שורות עם שינוי. וריאציות: batch לכל מוצר. מוצרים פשוטים: batch אחד (עד 100).
    ⛔ רק stock_status / description (span) / מטא זמן אספקה — אף פעם מחיר או סטטוס פרסום."""
    per_prod, simples = {}, []
    for p in rows:
        if p["group"] not in ("open", "close", "lead"):
            continue
        if not (p.get("status_change") or p.get("lead_change")):
            continue
        if p.get("unit_kind") == "variation":
            u = {"id": int(p["vid"])}
            if p["status_change"]:
                u["stock_status"] = p["new_status"]
                # ⚠️ עם manage_stock=true ו-qty 0, WC מתעלם מ-stock_status=instock (מחשב מהכמות).
                # פתיחה למלאי של ספק "בית לקוח" = ניהול לפי סטטוס, לא כמות.
                if p["new_status"] == "instock" and p.get("manage_stock"):
                    u["manage_stock"] = False
            if p["lead_change"]:
                u["description"] = lead_apply(p.get("_desc", ""), p["new_lead"])
            per_prod.setdefault(int(p["pid"]), []).append((p["barcode"], u))
        else:
            u = {"id": int(p["pid"])}
            if p["status_change"]:
                u["stock_status"] = p["new_status"]
                # ⚠️ עם manage_stock=true ו-qty 0, WC מתעלם מ-stock_status=instock (מחשב מהכמות).
                # פתיחה למלאי של ספק "בית לקוח" = ניהול לפי סטטוס, לא כמות.
                if p["new_status"] == "instock" and p.get("manage_stock"):
                    u["manage_stock"] = False
            if p["lead_change"]:
                u["meta_data"] = [{"key": LEAD_META, "value": str(p["new_lead"]) if p["new_lead"] else ""}]
            simples.append((p["barcode"], u))
    out = [{"path": f"/products/{pid}/variations/batch", "pid": pid, "items": items}
           for pid, items in per_prod.items()]
    for ch in _chunks(simples, 100):
        out.append({"path": "/products/batch", "pid": None, "items": ch})
    return out


def attach_descriptions(rows: list, products: dict):
    """התיאור הנוכחי של כל וריאציה — כדי להוסיף/להסיר את ה-span בלי לדרוס תוכן."""
    for p in rows:
        if p.get("unit_kind") == "variation" and p.get("pid") and p.get("vid"):
            v = ((products.get(int(p["pid"])) or {}).get("variations") or {}).get(int(p["vid"])) or {}
            p["_desc"] = v.get("description") or ""


def execute(wc, payloads: list, progress=None) -> dict:
    """מריץ את ה-batches. barcode → {ok, error}. שגיאה בבאץ' אחד לא עוצרת את השאר."""
    res, done = {}, 0
    for b in payloads:
        bcs = [bc for bc, _ in b["items"]]
        try:
            resp = wc.post(b["path"], {"update": [u for _, u in b["items"]]}) or {}
            upd = {int(x.get("id") or 0): x for x in (resp.get("update") or [])}
            for bc, u in b["items"]:
                x = upd.get(int(u["id"]))
                err = (x or {}).get("error") if x else "לא חזר בתשובת WC"
                res[bc] = {"ok": not err, "error": (err.get("message") if isinstance(err, dict) else err) or ""}
        except Exception as e:  # noqa: BLE001
            for bc in bcs:
                res[bc] = {"ok": False, "error": str(e)[:300]}
        done += len(bcs)
        if progress:
            progress(done)
    return res


def wp_purge(permalink) -> bool:
    """ניקוי מטמון עמוד באתר (gm-catalog). בלי creds של WP — מדלג בשקט."""
    base = os.getenv("WC_STORE_URL", "https://greenmobile.co.il").rstrip("/") or "https://greenmobile.co.il"
    u, pw = os.getenv("WP_USERNAME", ""), os.getenv("WP_APP_PASSWORD", "")
    if not (u and pw and permalink):
        return False
    try:
        import requests as _rq
        r = _rq.post(f"{base}/wp-json/gm-catalog/v1/purge", json={"url": permalink}, auth=(u, pw),
                     headers={"User-Agent": WC.UA}, timeout=30)
        return r.status_code < 400
    except Exception as e:  # noqa: BLE001
        logger.warning("purge %s failed: %s", permalink, e)
        return False


def plan_for_report(sid, rid, wc, fresh=False) -> dict:
    """התוכנית המלאה לדוח שמור: מיפוי עדכני + מצב חי באתר + השוואה לדוח הקודם."""
    sup = db.ss_supplier_get(sid)
    rep = db.ss_report_get(rid)
    if not sup or not rep or int(rep["supplier_id"]) != int(sid):
        raise ValueError("הדוח לא נמצא")
    maps = db.ss_map_all(sid)
    pids = {m["wc_product_id"] for b, m in maps.items() if m.get("wc_product_id") and not m.get("ignored")
            and b in {r["barcode"] for r in rep["rows"]}}
    products = fetch_products(wc, pids, fresh=fresh) if (wc and pids) else {}
    prev = db.ss_report_prev(sid, rid)
    plan = build_plan(sup["config"], rep["rows"], maps, products, has_prev=bool(prev), report_id=int(rid))
    plan["diff"] = diff_reports(prev["rows"], rep["rows"], sup["config"], prev.get("uploaded_at") or "") if prev else None
    plan["report"] = {k: rep.get(k) for k in ("id", "file_name", "uploaded_by", "uploaded_at", "n_rows")}
    plan["supplier"] = {"id": sup["id"], "name": sup["name"]}
    plan["_products"] = products
    return plan


def telegram_text(sup_name, run: dict, summary: dict, diff: dict, link: str = "") -> str:
    """הודעת טלגרם קצרה עם אותו סיכום חכם (HTML)."""
    c = summary["counts"]
    e = html.escape
    top = lambda key, n=5: ", ".join(e(x) for x in summary["names"].get(key, [])[:n]) + (  # noqa: E731
        f" ועוד {len(summary['names'][key]) - n}" if len(summary["names"].get(key, [])) > n else "")
    lines = [f"📦 <b>סנכרון מלאי ספק — {e(sup_name)}</b>",
             f"קובץ: {e(run.get('file_name') or '')} · ע״י {e(run.get('started_by') or '')}",
             f"בדוח {c['in_report']} · הותאמו {c['matched']} · לא נמכר אצלנו {c['ignored']}"]
    if c["back"]:
        lines.append(f"✅ חזרו למלאי: {c['back']} — {top('back')}")
    if c["down"]:
        lines.append(f"⛔ ירדו מהמלאי: {c['down']} — {top('down')}")
    if c["lead_set"] or c["lead_removed"]:
        lines.append(f"🕒 זמן אספקה נוסף/עודכן: {c['lead_set']} · הוסר: {c['lead_removed']}")
    lines.append(f"↔️ ללא שינוי: {c['unchanged']}" + (f" · 🛡️ מוגנים: {c['protected']}" if c["protected"] else ""))
    if c["unknown"]:
        lines.append(f"⚠️ הערה לא מוכרת (לא שונה): {c['unknown']}")
    if c["errors"]:
        lines.append(f"❌ שגיאות עדכון: {c['errors']} — {top('errors', 3)}")
    li = summary["listings"]
    lines.append(f"{li['live']} ליסטים חיים הושפעו · {li['draft']} טיוטות")
    for w in li["whole_oos"][:5]:
        lines.append(f"🔴 ליסט שלם אזל: {e(w['name'])}")
    for w in li["whole_back"][:5]:
        lines.append(f"🟢 ליסט שלם חזר: {e(w['name'])}")
    if diff:
        lines.append(f"לעומת הדוח הקודם ({_dmy(diff.get('prev_date'))}): {len(diff['back'])} חזרו, "
                     f"{len(diff['down'])} ירדו, {len(diff['new'])} חדשים בדוח, {len(diff['gone'])} נעלמו מהדוח")
    if link:
        lines.append(f"🔗 {link}")
    return "\n".join(lines)


def _dmy(iso) -> str:
    try:
        d = datetime.fromisoformat(str(iso))
        return d.strftime("%d/%m")
    except (TypeError, ValueError):
        return str(iso or "")[:10]


def report_age_days(iso) -> float:
    try:
        d = datetime.fromisoformat(str(iso))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - d).total_seconds() / 86400
    except (TypeError, ValueError):
        return None


def run_apply(run_id, sid, rid, wc, purge=wp_purge, notify=None, link="") -> dict:
    """הביצוע עצמו (רץ ב-thread מ-main). כותב התקדמות לרשומת הריצה; מחזיר את הסיכום."""
    try:
        db.ss_run_update(run_id, phase="קורא את המצב החי באתר…")
        plan = plan_for_report(sid, rid, wc, fresh=True)
        if plan["blocked"]:
            db.ss_run_update(run_id, status="error", phase="נחסם", finished_at=db.now_iso(),
                             error=f"{plan['n_blocked']} שורות לא מותאמות/לא מאושרות — הסנכרון נחסם")
            return None
        products = plan.pop("_products")
        rows = plan["rows"]
        attach_descriptions(rows, products)
        payloads = build_payloads(rows)
        total = sum(len(b["items"]) for b in payloads)
        db.ss_run_update(run_id, total=total, done=0, phase=f"מסנכרן… 0/{total}")
        results = execute(wc, payloads,
                          progress=lambda d: db.ss_run_update(run_id, done=d, phase=f"מסנכרן… {d}/{total}"))
        # ניקוי מטמון — רק מוצרים חיים שבאמת השתנו
        ok_pids = {int(p["pid"]) for p in rows if results.get(p["barcode"], {}).get("ok")}
        purged = 0
        for pid in ok_pids:
            pr = products.get(pid) or {}
            if pr.get("status") == "publish" and pr.get("permalink"):
                db.ss_run_update(run_id, phase=f"מנקה מטמון באתר… ({purged + 1}/{len(ok_pids)})")
                purged += 1 if purge and purge(pr["permalink"]) else 0
        cache_clear()
        summary = summarize(rows, products, results)
        summary["purged"] = purged
        res_rows = [{k: p.get(k) for k in ("barcode", "model", "desc", "avail", "note", "group", "pid", "vid",
                                           "wc_name", "wc_label", "permalink", "pstatus", "cur_status", "new_status",
                                           "cur_lead", "new_lead", "lead_change", "status_change", "msg", "is_new")}
                    | ({"ok": results[p["barcode"]]["ok"], "error": results[p["barcode"]]["error"]}
                       if p["barcode"] in results else {}) for p in rows]
        n_err = summary["counts"]["errors"]
        db.ss_run_update(run_id, status="done", finished_at=db.now_iso(), done=total,
                         phase=f"הסתיים · {total - n_err}/{total} עודכנו" + (f" · {n_err} שגיאות" if n_err else ""),
                         summary=summary, diff=plan.get("diff"), results=res_rows)
        if notify:
            try:
                sup = db.ss_supplier_get(sid)
                notify(telegram_text(sup["name"], db.ss_run_get(run_id), summary, plan.get("diff"), link))
            except Exception as e:  # noqa: BLE001
                logger.warning("supplier stock telegram failed: %s", e)
        return summary
    except Exception as e:  # noqa: BLE001
        logger.exception("supplier stock run %s failed", run_id)
        db.ss_run_update(run_id, status="error", phase="נכשל", finished_at=db.now_iso(), error=str(e)[:500])
        return None


def run_is_stale(run: dict) -> bool:
    if not run or run.get("status") != "running":
        return False
    age = report_age_days(run.get("started_at"))
    return age is not None and age * 86400 > RUN_STALE_SEC


def decode_upload(content_b64: str) -> bytes:
    s = content_b64 or ""
    if s.startswith("data:"):
        s = s.split(",", 1)[1]
    return base64.b64decode(s)
