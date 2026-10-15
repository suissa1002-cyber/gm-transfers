"""GreenDay · לוח מוצרים — שלב 1: חלון ל-Monday (15/10/2026).

הלוח "ניהול מוצרים" (2014994464) מסונכרן ל-DB של GreenOS (gd_product / gd_product_sub /
gd_product_update), וכל עריכה ב-GreenDay נכתבת **מיד ל-Monday** (write-through) — כי
הסוכנים (יובל / גלי / QA) עדיין קוראים וכותבים במאנדיי. בשלב 2 (מעבר) הכיוון מתהפך:
GreenDay = מקור האמת, והסוכנים עוברים ל-API שלנו אחד-אחד.

⚠️ ערכי הסטטוס הם התוויות בעברית בדיוק כמו במאנדיי (הסוכנים כותבים {"label": "..."}).
⚠️ "מוצרי קלוד" = ארכיון: מוצרים שעלו לאוויר, נבדקו והושלמו (אסי, 15/10).
"""
import json
import logging
import os
import time

import requests

import db

logger = logging.getLogger("greenday")

BOARD_ID = "2014994464"
GROUPS = [   # (monday group id, כותרת בממשק, צבע) — לפי סדר התצוגה
    ("topics", "מוצרים חדשים", "#579bfc"),
    ("group_mm3y9b6z", "מוצרים קיימים — עדכונים", "#fdab3d"),
    ("group_title", "מוצרים שהושלמו", "#00c875"),
    ("group_mktye2j3", "מוצרים ששולמו", "#a25ddc"),
    ("group_mkzqzksn", "ארכיון — עלו לאוויר ונבדקו", "#7e8597"),
]
GROUP_IDS = {g[0] for g in GROUPS}
STATUSES = [   # תוויות + צבעים זהים למאנדיי
    ("ממתין", "#fdab3d"), ("בעבודה", "#007eb5"), ("ממתין לבדיקה ופרסום", "#9d50dd"),
    ("ממתין לתיקון", "#ff7575"), ("תקוע", "#df2f4a"), ("הושלם", "#00c875"),
]
STATUS_LABELS = {s[0] for s in STATUSES}
STORAGES = ["32GB", "64GB", "128GB", "256GB", "512GB", "1TB", "8+256", "8-256"]
LIST_TYPES = ["ליסט רגיל.עד 1 וריאציה", "ליסט וריאציות"]

# שדה ב-GreenDay → (עמודת מאנדיי, סוג)
FIELDS = {
    "name":       ("name", "text"),
    "status":     ("status", "status"),
    "price":      ("numeric_mks5nhn6", "number"),
    "pos_sku":    ("text_mm1ar35n", "text"),
    "colors":     ("text_mks55fzt", "text"),
    "storages":   ("dropdown_mks5ymd", "dropdown"),
    "notes":      ("text_mks5kxq8", "text"),
    "image_link": ("text_mks5sfqd", "text"),
    "wc_id":      ("text_mksvwy0v", "text"),
    "list_type":  ("dropdown_mkswhe07", "dropdown"),
    "mdate":      ("date4", "date"),
}
COL_TO_FIELD = {v[0]: k for k, v in FIELDS.items() if v[0] != "name"}
SUB_COLS = {"text_mks5k2w8": "notes", "text_mks799n7": "colors", "numeric_mks7y7ye": "price",
            "text_mks5h1wt": "warranty", "status": "status", "text_mksevzkk": "link"}


def is_primary() -> bool:
    """שלב 2 (מעבר): GreenDay = מקור האמת. אין סנכרון מ-Monday ואין כתיבה אליו — Monday מוקפא כגיבוי."""
    return (db.setting_get("greenday_mode") or "") == "primary"


def _token() -> str:
    return os.getenv("MONDAY_API_TOKEN", "").strip()


def gql(query: str, variables=None, timeout=90):
    tok = _token()
    if not tok:
        raise RuntimeError("MONDAY_API_TOKEN לא מוגדר")
    for attempt in range(3):
        r = requests.post("https://api.monday.com/v2", json={"query": query, "variables": variables or {}},
                          headers={"Authorization": tok, "API-Version": "2024-10"}, timeout=timeout)
        if r.status_code == 429 or (r.status_code >= 500 and attempt < 2):
            time.sleep(3 * (attempt + 1))
            continue
        data = r.json()
        if data.get("errors"):
            msg = str(data["errors"])[:300]
            if "complexity" in msg.lower() and attempt < 2:
                time.sleep(15)
                continue
            raise RuntimeError(msg)
        return data["data"]
    raise RuntimeError("Monday לא זמין")


_ITEM_FIELDS = """id name updated_at created_at group { id }
  column_values(ids: [%s]) { id text value }
  subitems { id name updated_at column_values { id text } }""" % ",".join(
    f'"{c}"' for c in COL_TO_FIELD)


def _parse_item(it: dict) -> dict:
    p = {"monday_id": str(it["id"]), "name": it["name"], "grp": (it.get("group") or {}).get("id") or "",
         "monday_updated": it.get("updated_at") or "", "created_at": it.get("created_at") or ""}
    for c in it.get("column_values") or []:
        f = COL_TO_FIELD.get(c["id"])
        if not f:
            continue
        txt = c.get("text") or ""
        if f == "storages":
            p[f] = [x.strip() for x in txt.split(",") if x.strip()]
        elif f == "price":
            try:
                p[f] = float(txt) if txt else None
            except ValueError:
                p[f] = None
        else:
            p[f] = txt
    p["subs"] = []
    for s in it.get("subitems") or []:
        row = {"monday_id": str(s["id"]), "name": s["name"]}
        for c in s.get("column_values") or []:
            k = SUB_COLS.get(c["id"])
            if k:
                row[k] = c.get("text") or ""
        p["subs"].append(row)
    return p


def _fetch_all_items() -> list:
    items = []
    d = gql("query { boards(ids: [%s]) { items_page(limit: 100) { cursor items { %s } } } }"
            % (BOARD_ID, _ITEM_FIELDS))
    page = d["boards"][0]["items_page"]
    items += page["items"]
    cur = page.get("cursor")
    while cur:
        d = gql("query ($c: String!) { next_items_page(limit: 100, cursor: $c) { cursor items { %s } } }"
                % _ITEM_FIELDS, {"c": cur})
        items += d["next_items_page"]["items"]
        cur = d["next_items_page"].get("cursor")
    return items


def _fetch_updates(monday_ids: list) -> dict:
    """עדכונים (הערות אודי / דוחות הסוכנים) — רק לפריטים שהשתנו. text_body = בלי HTML (בטוח לתצוגה)."""
    out = {}
    for i in range(0, len(monday_ids), 25):
        chunk = monday_ids[i:i + 25]
        d = gql("query ($ids: [ID!]) { items(ids: $ids) { id updates(limit: 100) { id text_body created_at "
                "creator { name } assets { id name file_extension } replies { id text_body created_at creator { name } } } } }",
                {"ids": chunk})
        for it in d["items"]:
            ups = []
            for u in it.get("updates") or []:
                ups.append({"monday_id": str(u["id"]), "author": (u.get("creator") or {}).get("name") or "",
                            "body": u.get("text_body") or "", "created_at": u.get("created_at") or "",
                            "assets": [{"name": a.get("name") or "", "ext": a.get("file_extension") or ""}
                                       for a in (u.get("assets") or [])],
                            "replies": [{"author": (r.get("creator") or {}).get("name") or "",
                                         "body": r.get("text_body") or "", "created_at": r.get("created_at") or ""}
                                        for r in (u.get("replies") or [])]})
            out[str(it["id"])] = ups
    return out


ACTIVE_GROUPS = ["topics", "group_mm3y9b6z"]   # איפה העבודה השוטפת — מסונכרן כל 5 דק'


def _fetch_groups(gids: list) -> list:
    items = []
    d = gql("query ($g: [String]) { boards(ids: [%s]) { groups(ids: $g) { items_page(limit: 100) { cursor items { %s } } } } }"
            % (BOARD_ID, _ITEM_FIELDS), {"g": gids})
    for g in d["boards"][0]["groups"]:
        items += g["items_page"]["items"]
        cur = g["items_page"].get("cursor")
        while cur:
            n = gql("query ($c: String!) { next_items_page(limit: 100, cursor: $c) { cursor items { %s } } }"
                    % _ITEM_FIELDS, {"c": cur})
            items += n["next_items_page"]["items"]
            cur = n["next_items_page"].get("cursor")
    return items


def sync(full: bool = False, full_updates: bool = False) -> dict:
    """Monday → GreenDay.

    ⚠️ מכסת API יומית של Monday משותפת עם הסוכנים — לכן ברירת המחדל היא **הקבוצות הפעילות
    בלבד** (~1 קריאה). סנכרון מלא של 801 הפריטים: כל 6 שעות או ידנית. פריט שיצא מקבוצה פעילה
    (הועבר לארכיון/נמחק) נמשך בנפרד לפי id כדי שהקבוצה שלו תתעדכן מיד."""
    if is_primary():
        return {"mode": "primary", "skipped": True}
    t0 = time.time()
    if full:
        items = _fetch_all_items()
    else:
        items = _fetch_groups(ACTIVE_GROUPS)
        got = {str(i["id"]) for i in items}
        missing = [m for m in db.gd_active_ids(ACTIVE_GROUPS) if m not in got]
        for i in range(0, len(missing), 50):
            d = gql("query ($ids: [ID!]) { items(ids: $ids) { %s } }" % _ITEM_FIELDS, {"ids": missing[i:i + 50]})
            items += d["items"]
            got |= {str(x["id"]) for x in d["items"]}
        for m in missing:
            if m not in got:   # נמחק במאנדיי
                db.gd_mark_deleted_ids([m])
    known = db.gd_monday_versions()   # monday_id → monday_updated
    changed = []
    seen = set()
    for it in items:
        p = _parse_item(it)
        seen.add(p["monday_id"])
        if p["grp"] not in GROUP_IDS:
            continue
        if full_updates or known.get(p["monday_id"]) != p["monday_updated"]:
            changed.append(p["monday_id"])
        db.gd_product_upsert(p)
    ups = _fetch_updates(changed) if changed else {}
    for mid, lst in ups.items():
        db.gd_updates_replace(mid, lst)
    gone = db.gd_mark_deleted(seen) if (full and items) else 0
    db.setting_set("greenday_synced_at", str(int(time.time())))
    if full:
        db.setting_set("greenday_full_synced_at", str(int(time.time())))
    res = {"mode": "full" if full else "active", "items": len(items), "changed": len(changed),
           "deleted": gone, "sec": round(time.time() - t0, 1)}
    logger.info("greenday sync: %s", res)
    return res


# ── כתיבה ל-Monday (write-through) ──
def _col_value(field: str, value):
    col, kind = FIELDS[field]
    if kind == "status":
        if value not in STATUS_LABELS:
            raise ValueError("סטטוס לא מוכר")
        return col, json.dumps({"label": value}, ensure_ascii=False), False
    if kind == "dropdown":
        labels = [v for v in (value or []) if v]
        return col, json.dumps({"labels": labels}, ensure_ascii=False), False
    if kind == "date":
        return col, json.dumps({"date": value} if value else {}), False
    if kind == "number":
        return col, "" if value in (None, "") else str(float(value)).rstrip("0").rstrip("."), True
    return col, str(value or ""), True


def set_field(monday_id: str, field: str, value):
    if field not in FIELDS:
        raise ValueError("שדה לא מוכר")
    if field == "status" and value not in STATUS_LABELS:
        raise ValueError("סטטוס לא מוכר")
    if is_primary():
        db.gd_set_field(monday_id, field, value)
        return
    col, val, simple = _col_value(field, value)
    if simple:
        gql("mutation ($b: ID!, $i: ID!, $c: String!, $v: String) { change_simple_column_value("
            "board_id: $b, item_id: $i, column_id: $c, value: $v) { id updated_at } }",
            {"b": BOARD_ID, "i": str(monday_id), "c": col, "v": val})
    else:
        gql("mutation ($b: ID!, $i: ID!, $c: String!, $v: JSON!) { change_column_value("
            "board_id: $b, item_id: $i, column_id: $c, value: $v) { id updated_at } }",
            {"b": BOARD_ID, "i": str(monday_id), "c": col, "v": val})


def move_group(monday_id: str, grp: str):
    if grp not in GROUP_IDS:
        raise ValueError("קבוצה לא מוכרת")
    if is_primary():
        db.gd_set_field(monday_id, "grp", grp)
        return
    gql("mutation ($i: ID!, $g: String!) { move_item_to_group(item_id: $i, group_id: $g) { id } }",
        {"i": str(monday_id), "g": grp})


def create_item(name: str, grp: str, values: dict) -> str:
    if is_primary():
        return db.gd_create_local(name, grp, values)
    cols = {}
    for f, v in (values or {}).items():
        if f in FIELDS and f != "name" and v not in (None, "", []):
            col, kind = FIELDS[f]
            if kind == "status":
                cols[col] = {"label": v}
            elif kind == "dropdown":
                cols[col] = {"labels": v}
            elif kind == "date":
                cols[col] = {"date": v}
            else:
                cols[col] = str(v)
    d = gql("mutation ($b: ID!, $g: String!, $n: String!, $v: JSON) { create_item(board_id: $b, group_id: $g, "
            "item_name: $n, column_values: $v) { id } }",
            {"b": BOARD_ID, "g": grp, "n": name, "v": json.dumps(cols, ensure_ascii=False)})
    return str(d["create_item"]["id"])


def item_url(monday_id: str) -> str:
    """קישור לפריט במאנדיי (לקבצים מצורפים שעוד לא מועברים). slug החשבון נשמר אחרי שליפה ראשונה."""
    slug = db.setting_get("monday_account_slug")
    if not slug:
        try:
            slug = gql("query { account { slug } }")["account"]["slug"]
            db.setting_set("monday_account_slug", slug)
        except Exception:  # noqa: BLE001
            slug = ""
    return (f"https://{slug}.monday.com/boards/{BOARD_ID}/pulses/{monday_id}" if slug
            else f"https://monday.com/boards/{BOARD_ID}/pulses/{monday_id}")


import re as _re
_SIG = _re.compile(r"^\s*(.{1,40}?) · GreenDay\s*\n?")


def split_author(body: str, monday_author: str):
    """עדכון שנכתב ב-GreenDay נרשם במאנדיי בשם בעל הטוקן (אסי) — הכותב האמיתי בשורה הראשונה
    "אודי · GreenDay". מחזיר (כותב לתצוגה, גוף בלי שורת החתימה, נכתב ב-GreenDay?)."""
    m = _SIG.match(body or "")
    if m:
        rest = (body or "")[m.end():]
        return m.group(1).strip(), _re.sub(r"\n{3,}", "\n\n", rest).strip(), True
    return monday_author or "", _re.sub(r"\n{3,}", "\n\n", body or "").strip(), False


def _update_body(author: str, text: str) -> str:
    import html as _h
    return (f'<div dir="rtl"><b>{_h.escape(author)} · GreenDay</b><br>'
            + _h.escape(text).replace("\n", "<br>") + "</div>")


def edit_update(update_id: str, author: str, text: str):
    if is_primary() or str(update_id).startswith("gdu"):
        db.gd_update_edit(update_id, text)
        return
    gql("mutation ($i: ID!, $b: String!) { edit_update(id: $i, body: $b) { id } }",
        {"i": str(update_id), "b": _update_body(author, text)})


def create_update(monday_id: str, author: str, text: str) -> str:
    """עדכון (הוראות לסוכנים) — נרשם במאנדיי בשם בעל הטוקן, לכן מוסיפים מי כתב בפועל."""
    if is_primary():
        return db.gd_update_add(monday_id, author, text)
    body = _update_body(author, text)
    d = gql("mutation ($i: ID!, $b: String!) { create_update(item_id: $i, body: $b) { id created_at } }",
            {"i": str(monday_id), "b": body})
    return str(d["create_update"]["id"])


def refresh_items(monday_ids: list):
    """רענון כמה פריטים בקריאה אחת (בלי עדכונים) — אחרי פעולה קבוצתית."""
    if is_primary():
        return
    ids = [str(x) for x in monday_ids]
    for i in range(0, len(ids), 50):
        d = gql("query ($ids: [ID!]) { items(ids: $ids) { %s } }" % _ITEM_FIELDS, {"ids": ids[i:i + 50]})
        for it in d["items"]:
            db.gd_product_upsert(_parse_item(it))


def refresh_item(monday_id: str):
    """אחרי כתיבה — מושכים את הפריט מחדש מ-Monday כדי שה-DB ישקף בדיוק את מה שנשמר שם."""
    if is_primary():
        return
    d = gql("query ($ids: [ID!]) { items(ids: $ids) { %s } }" % _ITEM_FIELDS, {"ids": [str(monday_id)]})
    if d["items"]:
        db.gd_product_upsert(_parse_item(d["items"][0]))
    ups = _fetch_updates([str(monday_id)])
    for mid, lst in ups.items():
        db.gd_updates_replace(mid, lst)
