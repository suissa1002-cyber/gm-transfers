"""
שכבת DB לאפליקציית הקליטה.
תומך בשני מנועים: SQLite (פיתוח, ברירת מחדל) ו-PostgreSQL (Render, אם DATABASE_URL מוגדר).
ה-SQL נכתב עם placeholder אחיד `?` ומומר ל-`%s` עבור Postgres.

טבלאות:
  transfers       — פעולת העברה אחת (operationType=5): מקור/יעד/סטטוס/חותמות זמן/דגלי התראה
  transfer_items  — שורה לכל יחידה פיזית (פר-סריאל אם יש, אחרת פר-יחידת כמות)
  receive_scans   — לוג כל סריקה שבוצעה במסך הקליטה (audit)
"""

import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

import config as cfg

_USE_PG = bool(cfg.DATABASE_URL)
_lock = threading.RLock()
# סכמה ייעודית ב-Postgres כדי לבודד את הטבלאות שלנו (חולקים instance עם stock_watcher)
_PG_SCHEMA = os.getenv("PG_SCHEMA", "transfers_app")

if _USE_PG:
    import psycopg
    from psycopg.rows import dict_row
else:
    import sqlite3


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _q(sql: str) -> str:
    """המרת placeholders: SQLite משתמש ב-? , Postgres ב-%s ."""
    return sql.replace("?", "%s") if _USE_PG else sql


@contextmanager
def _conn():
    """חיבור DB עם dict-rows; thread-safe (נעילה גסה, מספיק לעומס הנמוך כאן)."""
    with _lock:
        if _USE_PG:
            conn = psycopg.connect(cfg.DATABASE_URL, row_factory=dict_row, autocommit=False)
            # בידוד בסכמה ייעודית (לא נוגעים בטבלאות של stock_watcher באותו instance)
            conn.execute(f"SET search_path TO {_PG_SCHEMA}")
        else:
            conn = sqlite3.connect(cfg.SQLITE_PATH)
            conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# Postgres רוצה SERIAL/BIGINT; SQLite רוצה INTEGER PRIMARY KEY AUTOINCREMENT.
_PK = "BIGSERIAL PRIMARY KEY" if _USE_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"

_SCHEMA = [
    f"""
    CREATE TABLE IF NOT EXISTS transfers (
        op_id          TEXT PRIMARY KEY,
        from_branch_id INTEGER,
        to_branch_id   INTEGER,
        op_type        INTEGER,
        employee       TEXT,
        created_at     TEXT,
        first_seen     TEXT,
        total_units    INTEGER DEFAULT 0,
        received_units INTEGER DEFAULT 0,
        status         TEXT DEFAULT 'in_transit',   -- in_transit | partial | received | closed
        received_at    TEXT,
        close_reason   TEXT,
        closed_by      TEXT,
        notified_new   INTEGER DEFAULT 0,
        reminded       INTEGER DEFAULT 0,
        escalated      INTEGER DEFAULT 0
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS transfer_items (
        id           {_PK},
        op_id        TEXT NOT NULL,
        product_id   TEXT,
        name         TEXT,
        serial       TEXT,            -- NULL/'' כשהמוצר לא מנוהל-סריאל
        barcode      TEXT,            -- ברקוד המוצר (למוצרים לא-סידוריים)
        line_idx     INTEGER DEFAULT 0,
        received     INTEGER DEFAULT 0,
        received_at  TEXT,
        received_by  TEXT,
        received_method TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS receive_scans (
        id           {pk},
        op_id        TEXT,
        branch_id    INTEGER,
        scanned_code TEXT,
        matched      INTEGER DEFAULT 0,   -- 1 אם הותאם לפריט צפוי
        item_id      BIGINT,
        scanned_at   TEXT,
        note         TEXT,
        method       TEXT                 -- scanner | manual | paste
    )
    """.format(pk=_PK),
    """
    CREATE TABLE IF NOT EXISTS serial_index (
        serial       TEXT PRIMARY KEY,
        product_id   TEXT,
        product_name TEXT,
        synced_at    TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS misroutes (
        id                 {pk},
        serial             TEXT,
        product_name       TEXT,
        expected_branch_id INTEGER,
        scanned_branch_id  INTEGER,
        scanned_by         TEXT,
        created_at         TEXT,
        status             TEXT DEFAULT 'open',   -- open | resolved
        resolved_at        TEXT,
        resolved_reason    TEXT
    )
    """.format(pk=_PK),
    "CREATE INDEX IF NOT EXISTS idx_items_op ON transfer_items(op_id)",
    "CREATE INDEX IF NOT EXISTS idx_items_serial ON transfer_items(serial)",
    "CREATE INDEX IF NOT EXISTS idx_items_barcode ON transfer_items(barcode)",
    """
    CREATE TABLE IF NOT EXISTS rebalance (
        id           {pk},
        product_id   TEXT,
        name         TEXT,
        kind         TEXT,          -- serial | barcode
        stock_json   TEXT,          -- JSON branch to qty
        needs_json   TEXT,          -- JSON list of branchIds with 0
        surplus_json TEXT,          -- JSON list of branchIds with 2 or more
        scanned_at   TEXT
    )
    """.format(pk=_PK),
    "CREATE INDEX IF NOT EXISTS idx_misroutes_serial ON misroutes(serial)",
    "CREATE INDEX IF NOT EXISTS idx_misroutes_status ON misroutes(status)",
    "CREATE INDEX IF NOT EXISTS idx_transfers_to ON transfers(to_branch_id, status)",
]


def init_db():
    with _conn() as c:
        cur = c.cursor()
        if _USE_PG:
            # יוצרים את הסכמה לפני הטבלאות (search_path כבר מצביע אליה)
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {_PG_SCHEMA}")
        for stmt in _SCHEMA:
            cur.execute(stmt)
    _migrate()


def _migrate():
    """מוסיף עמודות חדשות לטבלאות קיימות (idempotent)."""
    cols = [
        ("transfer_items", "received_method", "TEXT"),
        ("transfer_items", "barcode", "TEXT"),
        ("receive_scans",  "method", "TEXT"),
        ("transfers",      "close_reason", "TEXT"),
        ("transfers",      "closed_by", "TEXT"),
    ]
    for table, col, typ in cols:
        try:
            with _conn() as c:
                cur = c.cursor()
                if _USE_PG:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {typ}")
                else:
                    cur.execute(f"PRAGMA table_info({table})")
                    have = {r["name"] for r in cur.fetchall()}
                    if col not in have:
                        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        except Exception as e:  # noqa: BLE001
            import logging
            logging.getLogger("transfers.db").warning("migrate %s.%s: %s", table, col, e)


# ──────────────────────────────────────────────────────────────
# Poller upsert
# ──────────────────────────────────────────────────────────────
def transfer_exists(op_id: str) -> bool:
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT 1 FROM transfers WHERE op_id = ?"), (str(op_id),))
        return cur.fetchone() is not None


def upsert_transfer(op: dict) -> bool:
    """
    מכניס/מעדכן פעולת העברה ואת פריטיה. מחזיר True אם זו פעולה חדשה (לראשונה ב-DB).
    `op` הוא אובייקט תנועת מלאי גולמי מ-NewOrder (stock-operations).
    למוצרים לא-סידוריים, אם ה-poller צירף `it["barcode"]` — הוא יישמר להתאמת סריקה.
    """
    op_id = str(op.get("id"))
    if not op_id:
        return False
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT op_id FROM transfers WHERE op_id = ?"), (op_id,))
        exists = cur.fetchone() is not None
        if exists:
            return False  # כבר קיים — לא נוגעים (סטטוס הקליטה מנוהל אצלנו, לא בקופה)

        # בניית שורות הפריטים: פר-סריאל אם יש, אחרת פר-יחידת כמות (לפי ברקוד)
        items = []
        for idx, it in enumerate(op.get("stockItems", []) or []):
            pid = str(it.get("id") or "")
            name = it.get("name") or ""
            barcode = (it.get("barcode") or "").strip() or None
            serials = [s for s in (it.get("serials") or []) if s]
            if serials:
                for s in serials:
                    items.append((pid, name, str(s), barcode, idx))
            else:
                qty = int(abs(it.get("quantity") or 0)) or 1
                for _ in range(qty):
                    items.append((pid, name, None, barcode, idx))

        total_units = len(items)
        cur.execute(_q("""
            INSERT INTO transfers
              (op_id, from_branch_id, to_branch_id, op_type, employee,
               created_at, first_seen, total_units, received_units, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'in_transit')
        """), (
            op_id,
            op.get("branchId"),
            op.get("receivingBranchId"),
            op.get("operationType"),
            op.get("employee") or "",
            op.get("createDate") or "",
            now_iso(),
            total_units,
        ))
        for (pid, name, serial, barcode, idx) in items:
            cur.execute(_q("""
                INSERT INTO transfer_items (op_id, product_id, name, serial, barcode, line_idx)
                VALUES (?, ?, ?, ?, ?, ?)
            """), (op_id, pid, name, serial, barcode, idx))
        return True


# ──────────────────────────────────────────────────────────────
# Queries לתצוגה
# ──────────────────────────────────────────────────────────────
def _row_to_dict(row) -> dict:
    return dict(row) if row is not None else None


def list_in_transit(to_branch_id: int) -> list[dict]:
    """העברות שטרם נקלטו במלואן, שמיועדות לסניף הנתון. כולל מונה התקדמות."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("""
            SELECT * FROM transfers
            WHERE to_branch_id = ? AND status IN ('in_transit','partial')
            ORDER BY created_at DESC
        """), (to_branch_id,))
        return [dict(r) for r in cur.fetchall()]


def close_transfer(op_id: str, reason: str = "", by: str = "") -> dict:
    """סגירה ידנית: פריטים שלא נסרקו → חוסר (3); ההעברה יוצאת מהלוח עם סיבה."""
    op_id = str(op_id)
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("""
            UPDATE transfer_items SET received = 3, received_at = ?
            WHERE op_id = ? AND received = 0
        """), (now_iso(), op_id))
        cur.execute(_q("SELECT COUNT(*) AS n FROM transfer_items WHERE op_id = ? AND received IN (1,2)"), (op_id,))
        rec = cur.fetchone()["n"]
        cur.execute(_q("""
            UPDATE transfers SET status='closed', received_units=?,
                   received_at=COALESCE(received_at, ?), close_reason=?, closed_by=?
            WHERE op_id = ?
        """), (rec, now_iso(), reason, by, op_id))
    return get_transfer(op_id)


def get_transfer(op_id: str) -> dict:
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT * FROM transfers WHERE op_id = ?"), (str(op_id),))
        t = _row_to_dict(cur.fetchone())
        if not t:
            return None
        cur.execute(_q("""
            SELECT * FROM transfer_items WHERE op_id = ?
            ORDER BY line_idx, id
        """), (str(op_id),))
        t["items"] = [dict(r) for r in cur.fetchall()]
        return t


def _recount(cur, op_id: str):
    """
    מעדכן received_units/status. "נקלט" לצורך השלמה = received בערך 1 (כאן) או 2 (הופנה).
    received=3 = חוסר (נסגר ידנית). העברה שנסגרה ('closed') לא משנה סטטוס.
    """
    cur.execute(_q("SELECT status FROM transfers WHERE op_id = ?"), (op_id,))
    row = cur.fetchone()
    cur_status = row["status"] if row else None
    cur.execute(_q("SELECT COUNT(*) AS n FROM transfer_items WHERE op_id = ?"), (op_id,))
    total = cur.fetchone()["n"]
    cur.execute(_q("SELECT COUNT(*) AS n FROM transfer_items WHERE op_id = ? AND received IN (1,2)"), (op_id,))
    rec = cur.fetchone()["n"]
    if cur_status == "closed":
        cur.execute(_q("UPDATE transfers SET received_units = ? WHERE op_id = ?"), (rec, op_id))
        return rec, total, "closed"
    if rec == 0:
        status = "in_transit"
    elif rec < total:
        status = "partial"
    else:
        status = "received"
    received_at = now_iso() if status == "received" else None
    cur.execute(_q("""
        UPDATE transfers SET received_units = ?, status = ?,
               received_at = COALESCE(received_at, ?)
        WHERE op_id = ?
    """), (rec, status, received_at, op_id))
    return rec, total, status


def receive_scan(branch_id: int, code: str, op_id: str = None, employee: str = None,
                 method: str = None) -> dict:
    """
    מטפל בסריקה בודדת. מתאים לפי **סריאל או ברקוד** (מוצרים לא-סידוריים).
    אם op_id סופק — מחפש בתוך אותה העברה; אחרת בכל ההעברות הפתוחות של הסניף.
    `employee` — שם הנציג (received_by). `method` — אופן הקלט (scanner|manual|paste).
    מחזיר {matched, item, transfer, message}.
    """
    code = (code or "").strip()
    received_by = (employee or "").strip() or f"branch:{branch_id}"
    method = (method or "").strip() or "unknown"
    with _conn() as c:
        cur = c.cursor()
        # פריט צפוי שלא נקלט: סריאל מדויק, או ברקוד תואם (לא-סידורי)
        params = [code, code, branch_id]
        sql = """
            SELECT ti.* FROM transfer_items ti
            JOIN transfers t ON t.op_id = ti.op_id
            WHERE (ti.serial = ? OR ti.barcode = ?) AND t.to_branch_id = ? AND ti.received = 0
        """
        if op_id:
            sql += " AND ti.op_id = ?"
            params.append(str(op_id))
        # סריאל קודם (מדויק), ואז הוותיק ביותר
        sql += " ORDER BY (CASE WHEN ti.serial = ? THEN 0 ELSE 1 END), t.created_at ASC LIMIT 1"
        params.append(code)
        cur.execute(_q(sql), tuple(params))
        item = _row_to_dict(cur.fetchone())

        matched = item is not None
        target_op = item["op_id"] if matched else op_id

        if matched:
            cur.execute(_q("""
                UPDATE transfer_items SET received = 1, received_at = ?, received_by = ?,
                       received_method = ?
                WHERE id = ?
            """), (now_iso(), received_by, method, item["id"]))
            _recount(cur, item["op_id"])
            msg = "✓ נקלט"
            # התאמה חוצת-העברות: אותו סריאל ממתין בהעברות פתוחות אחרות → "הופנה"
            sn = item.get("serial")
            if sn:
                cur.execute(_q("""
                    SELECT id, op_id FROM transfer_items
                    WHERE serial = ? AND id != ? AND received = 0
                """), (sn, item["id"]))
                others = [dict(r) for r in cur.fetchall()]
                note = f"↪ {cfg.branch_name(branch_id)}"
                for o in others:
                    cur.execute(_q("""
                        UPDATE transfer_items SET received = 2, received_at = ?, received_by = ?,
                               received_method = 'redirected'
                        WHERE id = ?
                    """), (now_iso(), note, o["id"]))
                    _recount(cur, o["op_id"])
        else:
            # אולי כבר נקלט קודם? בדיקה לשיפור ההודעה
            cur.execute(_q("""
                SELECT received FROM transfer_items
                WHERE serial = ? OR barcode = ? ORDER BY received DESC LIMIT 1
            """), (code, code))
            prev = _row_to_dict(cur.fetchone())
            if prev and prev["received"]:
                msg = "כבר נסרק קודם"
            else:
                msg = "לא שייך להעברה נכנסת"

        cur.execute(_q("""
            INSERT INTO receive_scans (op_id, branch_id, scanned_code, matched, item_id, scanned_at, note, method)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """), (target_op, branch_id, code, 1 if matched else 0,
               item["id"] if matched else None, now_iso(), received_by, method))

    # קוראים את ההעברה אחרי commit (חיבור נפרד לא רואה שינוי לא-מחויב)
    transfer = get_transfer(target_op) if target_op else None
    return {"matched": matched, "message": msg, "item": item, "transfer": transfer}


def mark_transfer_flag(op_id: str, flag: str):
    """מסמן דגל התראה (notified_new/reminded/escalated) כדי לא לשלוח כפול."""
    assert flag in ("notified_new", "reminded", "escalated")
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q(f"UPDATE transfers SET {flag} = 1 WHERE op_id = ?"), (str(op_id),))


def open_transfers_for_alerts() -> list[dict]:
    """כל ההעברות הפתוחות (לא נקלטו) — לשירות ההתראות/הסלמה."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT * FROM transfers WHERE status != 'received' ORDER BY created_at ASC"))
        return [dict(r) for r in cur.fetchall()]


def list_all_transfers(include_received_days: int = 7) -> list[dict]:
    """
    כל ההעברות לתצוגת ניהול: כל הפתוחות (in_transit/partial) + שנקלטו ב-N הימים האחרונים.
    ממוין לפי created_at יורד.
    """
    with _conn() as c:
        cur = c.cursor()
        cutoff = (datetime.now(timezone.utc).astimezone()
                  - timedelta(days=include_received_days)).isoformat(timespec="seconds")
        cur.execute(_q("""
            SELECT * FROM transfers
            WHERE status IN ('in_transit','partial')
               OR (status IN ('received','closed') AND received_at IS NOT NULL AND received_at >= ?)
            ORDER BY created_at DESC
        """), (cutoff,))
        return [dict(r) for r in cur.fetchall()]


# ──────────────────────────────────────────────────────────────
# אינדקס סריאל→מוצר (מיפוי קבוע; הסניף נבדק חי בזמן הסריקה)
# ──────────────────────────────────────────────────────────────
def serial_index_upsert_many(rows: list):
    """rows = [(serial, product_id, product_name), ...]."""
    if not rows:
        return 0
    ts = now_iso()
    with _conn() as c:
        cur = c.cursor()
        for serial, pid, name in rows:
            if not serial:
                continue
            cur.execute(_q("""
                INSERT INTO serial_index (serial, product_id, product_name, synced_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(serial) DO UPDATE SET
                    product_id=excluded.product_id,
                    product_name=excluded.product_name,
                    synced_at=excluded.synced_at
            """), (str(serial), str(pid) if pid is not None else None, name, ts))
    return len(rows)


def serial_product(serial: str) -> dict:
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT * FROM serial_index WHERE serial = ?"), (str(serial).strip(),))
        return _row_to_dict(cur.fetchone())


def serial_index_count() -> int:
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT COUNT(*) AS n FROM serial_index"))
        return cur.fetchone()["n"]


# ──────────────────────────────────────────────────────────────
# חריגות "מכשיר לא במקום"
# ──────────────────────────────────────────────────────────────
def open_misroute(serial: str, product_name: str, expected_branch_id,
                  scanned_branch_id, scanned_by: str) -> bool:
    """פותח חריגה אם אין כבר אחת פתוחה לאותו סריאל. מחזיר True אם נפתחה חדשה."""
    serial = str(serial).strip()
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT id FROM misroutes WHERE serial = ? AND status = 'open'"), (serial,))
        if cur.fetchone():
            return False
        cur.execute(_q("""
            INSERT INTO misroutes (serial, product_name, expected_branch_id, scanned_branch_id,
                                   scanned_by, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?, 'open')
        """), (serial, product_name, expected_branch_id, scanned_branch_id,
               scanned_by, now_iso()))
        return True


def resolve_misroutes(serial: str, reason: str = "נקלט בסניף הנכון") -> int:
    serial = str(serial).strip()
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("""
            UPDATE misroutes SET status='resolved', resolved_at=?, resolved_reason=?
            WHERE serial = ? AND status='open'
        """), (now_iso(), reason, serial))
        return cur.rowcount if hasattr(cur, "rowcount") else 0


def relabel_receiver(op_id: str, name: str) -> int:
    """תיקון ידני של שם הקולט בהעברה (למשל אם נסרק סריאל לשדה השם)."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("""
            UPDATE transfer_items SET received_by = ?
            WHERE op_id = ? AND received IN (1,2)
        """), (name, str(op_id)))
        return cur.rowcount if hasattr(cur, "rowcount") else 0


def numeric_receivers() -> list:
    """רשומות שבהן הקולט נראה כמו מספר/סריאל (באג ישן) — להצגה/תיקון."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("""
            SELECT op_id, received_by, COUNT(*) AS n FROM transfer_items
            WHERE received = 1 AND received_by ~ '^[0-9]+$'
            GROUP BY op_id, received_by ORDER BY op_id
        """) if _USE_PG else
        "SELECT op_id, received_by, COUNT(*) AS n FROM transfer_items "
        "WHERE received=1 AND received_by GLOB '[0-9]*' AND received_by NOT GLOB '*[^0-9]*' "
        "GROUP BY op_id, received_by ORDER BY op_id")
        return [dict(r) for r in cur.fetchall()]


def resolve_misroute_by_id(mid, reason: str = "טופל ידנית") -> int:
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("""
            UPDATE misroutes SET status='resolved', resolved_at=?, resolved_reason=?
            WHERE id = ? AND status='open'
        """), (now_iso(), reason, mid))
        return cur.rowcount if hasattr(cur, "rowcount") else 0


def list_open_misroutes() -> list:
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT * FROM misroutes WHERE status='open' ORDER BY created_at DESC"))
        return [dict(r) for r in cur.fetchall()]


def transfer_manual_count(op_id: str) -> int:
    """כמה פריטים בהעברה נקלטו ידנית/בהדבקה (לא בסורק) — דגל אנטי-הונאה לניהול."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("""
            SELECT COUNT(*) AS n FROM transfer_items
            WHERE op_id = ? AND received = 1
              AND received_method IS NOT NULL AND received_method IN ('manual','paste')
        """), (str(op_id),))
        return cur.fetchone()["n"]


def transfer_search_text(op_id: str) -> str:
    """טקסט חיפוש להעברה — כל הסריאלים/ברקודים/שמות הפריטים (לחיפוש בלוח הניהול)."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT serial, barcode, name FROM transfer_items WHERE op_id = ?"),
                    (str(op_id),))
        parts = []
        for r in cur.fetchall():
            for k in ("serial", "barcode", "name"):
                v = r[k]
                if v:
                    parts.append(str(v))
        return " ".join(parts)


def transfer_state_counts(op_id: str) -> dict:
    """{redirected, missing} — פריטים שהופנו לסניף אחר / סומנו כחוסר."""
    with _conn() as c:
        cur = c.cursor()
        out = {}
        for key, val in (("redirected", 2), ("missing", 3)):
            cur.execute(_q("SELECT COUNT(*) AS n FROM transfer_items WHERE op_id = ? AND received = ?"),
                        (str(op_id), val))
            out[key] = cur.fetchone()["n"]
        return out


def transfer_receivers(op_id: str) -> list:
    """שמות הנציגים שקלטו פריטים בהעברה (distinct), ללא placeholder 'branch:'."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("""
            SELECT DISTINCT received_by FROM transfer_items
            WHERE op_id = ? AND received = 1 AND received_by IS NOT NULL
        """), (str(op_id),))
        names = [r["received_by"] for r in cur.fetchall()]
        return [n for n in names if n and not str(n).startswith("branch:")]


def rebalance_replace(rows: list, scanned_at: str):
    """מחליף את כל רשימת האיזון בתוצאות סריקה חדשות."""
    import json as _json
    with _conn() as c:
        cur = c.cursor()
        cur.execute("DELETE FROM rebalance")
        for r in rows:
            cur.execute(_q("""
                INSERT INTO rebalance (product_id, name, kind, stock_json, needs_json, surplus_json, scanned_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """), (str(r["product_id"]), r.get("name") or "", r.get("kind"),
                   _json.dumps(r.get("stock") or {}), _json.dumps(r.get("needs") or []),
                   _json.dumps(r.get("surplus") or []), scanned_at))


def rebalance_list() -> list:
    import json as _json
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT * FROM rebalance ORDER BY name")
        out = []
        for r in cur.fetchall():
            d = dict(r)
            for k in ("stock_json", "needs_json", "surplus_json"):
                try: d[k.replace("_json", "")] = _json.loads(d.pop(k) or ("[]" if k != "stock_json" else "{}"))
                except Exception: d[k.replace("_json", "")] = {} if k == "stock_json" else []
            out.append(d)
        return out


def rebalance_last_scan() -> str:
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT MAX(scanned_at) AS m FROM rebalance")
        row = cur.fetchone()
        return (row["m"] if row else None) or None


def stats() -> dict:
    with _conn() as c:
        cur = c.cursor()
        out = {}
        for st in ("in_transit", "partial", "received"):
            cur.execute(_q("SELECT COUNT(*) AS n FROM transfers WHERE status = ?"), (st,))
            out[st] = cur.fetchone()["n"]
        return out
