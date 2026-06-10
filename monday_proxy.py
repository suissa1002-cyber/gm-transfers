"""
Monday proxy — עמוד "משימות סוכנים" ב-Ops Hub.
הטוקן נשאר בצד השרת (env: MONDAY_API_TOKEN); הדפדפן מדבר רק עם ה-API שלנו.
בורד: משימות סוכנים 5092673295.
"""

import logging
import os
import time

import requests

logger = logging.getLogger("transfers.monday")

API = "https://api.monday.com/v2"
BOARD_ID = 5092673295
COLS = {
    "status":   "color_mm145dt4",
    "priority": "color_mm142kr1",
    "type":     "color_mm141v23",
    "notes":    "text_mm14kfck",
    "start":    "date_mm14kqgx",
    "end":      "date_mm14pzhs",
    "tasknum":  "numeric_mm40m0ej",
}
STATUS_LABELS = ["לא התחיל", "בעבודה", "הושלם", "תקוע"]

_cache = {"at": 0.0, "data": None}
_CACHE_TTL = 60


def _token():
    return os.getenv("MONDAY_API_TOKEN", "").strip()


def _gql(query: str) -> dict:
    r = requests.post(API, json={"query": query},
                      headers={"Authorization": _token(),
                               "API-Version": "2024-10"}, timeout=30)
    r.raise_for_status()
    out = r.json()
    if out.get("errors"):
        raise RuntimeError(str(out["errors"])[:300])
    return out["data"]


def available() -> bool:
    return bool(_token())


def fetch_tasks(force: bool = False) -> dict:
    """כל המשימות בבורד + קבוצות + URL. cache 60 שניות."""
    if not force and _cache["data"] and time.time() - _cache["at"] < _CACHE_TTL:
        return _cache["data"]
    col_ids = '","'.join(COLS.values())
    q = f'''query {{ boards(ids:[{BOARD_ID}]) {{
        url groups {{ id title }}
        items_page(limit:250) {{ cursor items {{
            id name created_at updated_at group {{ id title }}
            column_values(ids:["{col_ids}"]) {{ id text }}
        }} }} }} }}'''
    data = _gql(q)
    board = data["boards"][0]
    items = list(board["items_page"]["items"])
    cursor = board["items_page"].get("cursor")
    while cursor:
        q2 = f'''query {{ next_items_page(cursor:"{cursor}", limit:250) {{ cursor items {{
            id name created_at updated_at group {{ id title }}
            column_values(ids:["{col_ids}"]) {{ id text }}
        }} }} }}'''
        page = _gql(q2)["next_items_page"]
        items.extend(page["items"])
        cursor = page.get("cursor")
    by_col = {v: k for k, v in COLS.items()}
    out_items = []
    for it in items:
        cols = {by_col.get(c["id"], c["id"]): (c.get("text") or "") for c in it.get("column_values", [])}
        out_items.append({
            "id": it["id"], "name": it["name"],
            "group_id": (it.get("group") or {}).get("id"),
            "group": (it.get("group") or {}).get("title"),
            "created_at": it.get("created_at"), "updated_at": it.get("updated_at"),
            **{k: cols.get(k, "") for k in COLS},
        })
    result = {"available": True, "board_url": board.get("url"),
              "groups": board.get("groups") or [],
              "statuses": STATUS_LABELS, "items": out_items,
              "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _cache["data"], _cache["at"] = result, time.time()
    return result


def set_status(item_id, label: str) -> dict:
    if label not in STATUS_LABELS:
        raise ValueError("סטטוס לא מוכר")
    q = f'''mutation {{ change_simple_column_value(
        board_id:{BOARD_ID}, item_id:{int(item_id)},
        column_id:"{COLS['status']}", value:"{label}") {{ id }} }}'''
    _gql(q)
    _cache["at"] = 0   # bust cache
    return {"ok": True}
