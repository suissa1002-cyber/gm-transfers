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
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

import config as cfg


def _norm_serial(s) -> str:
    """נרמול סריאל להשוואה: אותיות+ספרות בלבד, אותיות גדולות.
    מטפל בסריאלים עם לוכסן/מקף/רווח (למשל '35785/AGAAJF2SU03507')."""
    return re.sub(r"[^A-Za-z0-9]", "", str(s or "")).upper()


def _serial_tokens(s) -> list:
    return [_norm_serial(t) for t in re.split(r"[^A-Za-z0-9]+", str(s or "")) if _norm_serial(t)]


def _is_subsequence(small: str, big: str) -> bool:
    """האם small הוא תת-רצף של big (כל התווים מופיעים בסדר, לא בהכרח רצוף)."""
    it = iter(big)
    return all(ch in it for ch in small)


def _serial_loose_eq(scanned, stored) -> bool:
    """התאמה דטרמיניסטית-חזקה: מנורמל זהה, או אותם מקטעים בסדר הפוך
    (ברקודים עם לוכסן). לא כולל תת-רצף — זה נבדק רק במצב חד-משמעי."""
    ns, nt = _norm_serial(scanned), _norm_serial(stored)
    if not ns or not nt:
        return False
    if ns == nt:
        return True
    st, sc = _serial_tokens(stored), _serial_tokens(scanned)
    if len(st) >= 2 and sorted(st) == sorted(sc):
        return True
    if len(st) == 2 and ns in (st[0] + st[1], st[1] + st[0]):
        return True
    short, lng = (ns, nt) if len(ns) <= len(nt) else (nt, ns)
    return len(short) >= 8 and (lng.endswith(short) or lng.startswith(short))


def _serial_subseq_match(scanned, stored) -> bool:
    """התאמת תת-רצף — לסורק מצלמה שמשמיט תווים אך שומר על הסדר
    (למשל 'L1729000001F7P00YU3B' נקרא כ-'1729000001700U3').
    גייטים: סרוק ≥10 תווים ו-≥55% מאורך המאוחסן. בטוח רק במצב חד-משמעי
    (מתאים לבדיוק מכשיר ממתין אחד) — נאכף ב-receive_scan."""
    ns, nt = _norm_serial(scanned), _norm_serial(stored)
    if len(ns) < 10 or not nt or len(ns) < 0.55 * len(nt):
        return False
    return _is_subsequence(ns, nt)

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
    """
    CREATE TABLE IF NOT EXISTS transfer_plan (
        id          {pk},
        product_id  TEXT,
        name        TEXT,
        from_branch INTEGER,
        to_branch   INTEGER,
        qty         INTEGER DEFAULT 1,
        created_at  TEXT
    )
    """.format(pk=_PK),
    """
    CREATE TABLE IF NOT EXISTS broadcasts (
        branch_id    INTEGER PRIMARY KEY,
        broadcast_at TEXT
    )
    """,
    # ── מאגר מכירות (Sales Cache) — נבנה מ-/api/Documents/line-items ──
    # qty חתום: מכירה (docType 0) חיובי, החזרה/זיכוי (docType 5) שלילי → SUM(qty)=מכירה נטו
    """
    CREATE TABLE IF NOT EXISTS sales (
        id          {pk},
        doc_id      TEXT,
        line_no     INTEGER,
        product_id  TEXT,
        name        TEXT,
        qty         REAL,
        price       REAL,
        serial      TEXT,
        branch_id   INTEGER,
        doc_type    INTEGER,
        sale_date   TEXT,
        UNIQUE(doc_id, line_no)
    )
    """.format(pk=_PK),
    # מצב איסוף מצטבר (cursor): מפתח→ערך
    """
    CREATE TABLE IF NOT EXISTS sales_ingest_state (
        k  TEXT PRIMARY KEY,
        v  TEXT
    )
    """,
    # הורדות מלאי בסניף מרלוג (operationType=2). חצי-קופה — הורדה = מכירה בפועל.
    """
    CREATE TABLE IF NOT EXISTS removals (
        id          {pk},
        op_id       TEXT,
        line_no     INTEGER,
        product_id  TEXT,
        name        TEXT,
        qty         REAL,
        serials     TEXT,
        employee    TEXT,
        note        TEXT,
        branch_id   INTEGER,
        removed_at  TEXT,
        UNIQUE(op_id, line_no)
    )
    """.format(pk=_PK),
    # מכשירים מאושרים — device allowlist (אבטחת גישה לאפליקציה)
    """
    CREATE TABLE IF NOT EXISTS devices (
        token       TEXT PRIMARY KEY,
        name        TEXT,
        status      TEXT DEFAULT 'pending',   -- pending / approved / denied
        auto        INTEGER DEFAULT 0,        -- 1 = אושר אוטומטית (מכשיר קיים)
        ua          TEXT,
        ip          TEXT,
        branch_hint TEXT,
        created_at  TEXT,
        approved_at TEXT,
        last_seen   TEXT
    )
    """,
    # קטלוג מוצרים (cache ב-DB) — שורד restart; נבנה ע"י job מתוזמן, לא בכל בקשה.
    # כך המלצות ההזמנה קוראות מ-DB (מיידי, 0 קריאות NewOrder) ולא מפוצצות את הטוקן המשותף.
    """
    CREATE TABLE IF NOT EXISTS catalog (
        product_id  TEXT PRIMARY KEY,
        name        TEXT,
        stock       REAL,
        supplier    TEXT,
        category    TEXT,
        kind        TEXT,
        barcode     TEXT,
        active      INTEGER,
        updated_at  TEXT
    )
    """,
    # טיוטת הזמנה (רשימה שטוחה; מקביל ל-transfer_plan)
    """
    CREATE TABLE IF NOT EXISTS order_plan (
        id          {pk},
        product_id  TEXT,
        name        TEXT,
        qty         INTEGER DEFAULT 1,
        supplier    TEXT,
        category    TEXT,
        kind        TEXT,
        created_at  TEXT
    )
    """.format(pk=_PK),
    # 💬 וואטסאפ: שכבת מטא משלנו מעל ConnectOp (מעקב/הערות) — "מתקדם יותר מקונקטופ"
    """
    CREATE TABLE IF NOT EXISTS wa_meta (
        phone       TEXT PRIMARY KEY,
        star        INTEGER DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS wa_notes (
        id          {pk},
        phone       TEXT,
        text        TEXT,
        author      TEXT,
        created_at  TEXT
    )
    """.format(pk=_PK),
    "CREATE INDEX IF NOT EXISTS idx_wa_notes_phone ON wa_notes(phone)",
    "CREATE INDEX IF NOT EXISTS idx_misroutes_serial ON misroutes(serial)",
    "CREATE INDEX IF NOT EXISTS idx_misroutes_status ON misroutes(status)",
    "CREATE INDEX IF NOT EXISTS idx_transfers_to ON transfers(to_branch_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_sales_product ON sales(product_id, sale_date)",
    "CREATE INDEX IF NOT EXISTS idx_sales_date ON sales(sale_date)",
    "CREATE INDEX IF NOT EXISTS idx_removals_product ON removals(product_id, removed_at)",
    "CREATE INDEX IF NOT EXISTS idx_removals_date ON removals(removed_at)",
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
        ("removals",       "note", "TEXT"),
        # בקשות העברה: serial = יחידה ספציפית (להתאמה אוטומטית מול העברות);
        # bcast = מצב שידור למסך הסניף (0=לא שודר, 1=פעיל/מוצג, 2=נסגר ע"י הסניף)
        ("transfer_plan",  "serial", "TEXT"),
        ("transfer_plan",  "bcast", "INTEGER"),
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

        # fallback: הברקוד הפיזי לא תמיד זהה לסריאל המאוחסן —
        #   (א) היפוך מקטעים סביב לוכסן  (ב) סורק מצלמה שמשמיט תווים (תת-רצף).
        # נטען את הפריטים הסריאליים הפתוחים ומתאימים. תת-רצף מתקבל רק אם
        # הוא חד-משמעי (בדיוק מועמד אחד) — אחרת מבקשים סריקה חוזרת.
        if item is None and len(_norm_serial(code)) >= 4:
            fb_sql = """
                SELECT ti.* FROM transfer_items ti
                JOIN transfers t ON t.op_id = ti.op_id
                WHERE t.to_branch_id = ? AND ti.received = 0
                  AND ti.serial IS NOT NULL AND ti.serial != ?
            """
            fb_params = [branch_id, ""]
            if op_id:
                fb_sql += " AND ti.op_id = ?"
                fb_params.append(str(op_id))
            fb_sql += " ORDER BY t.created_at ASC"
            cur.execute(_q(fb_sql), tuple(fb_params))
            cands = [_row_to_dict(r) for r in cur.fetchall()]
            # 1) התאמה דטרמיניסטית חזקה (זהה-מנורמל / היפוך מקטעים)
            strong = [c for c in cands if _serial_loose_eq(code, c.get("serial"))]
            if len(strong) == 1:
                item = strong[0]
            elif not strong:
                # 2) תת-רצף (סורק מצלמה) — רק אם חד-משמעי
                subq = [c for c in cands if _serial_subseq_match(code, c.get("serial"))]
                if len(subq) == 1:
                    item = subq[0]

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


def serial_search(q: str, limit: int = 20) -> list:
    """חיפוש סריאל חלקי (substring) באינדקס — לטאב מלאי חי."""
    q = (q or "").strip()
    if not q:
        return []
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("""
            SELECT serial, product_id, product_name FROM serial_index
            WHERE serial LIKE ? ORDER BY serial LIMIT ?
        """), ("%" + q + "%", limit))
        return [dict(r) for r in cur.fetchall()]


def serial_dynamic_status(serials: list) -> dict:
    """סטטוס דינמי לכל סריאל לפי ה-DB שלנו (בלי NewOrder):
      transit  — יחידה בהעברה פתוחה שטרם נקלטה (→ סניף יעד)
      reserved — יחידה בבקשת העברה משודרת (→ סניף יעד)
    מחזיר {serial: {kind, to_branch}}; סריאל ללא רשומה לא יופיע (=זמין)."""
    serials = [str(s) for s in serials if s]
    if not serials:
        return {}
    ph = ",".join("?" * len(serials))
    out = {}
    with _conn() as c:
        cur = c.cursor()
        # בהעברה פתוחה (טרם נקלט: received=0) — הסטטוס החזק יותר, נכתב אחרון
        cur.execute(_q(f"""
            SELECT i.serial AS serial, t.to_branch_id AS to_branch
            FROM transfer_items i JOIN transfers t ON t.op_id = i.op_id
            WHERE i.serial IN ({ph}) AND i.received = 0
              AND t.status IN ('in_transit','partial')
        """), tuple(serials))
        transit = {str(r["serial"]): r["to_branch"] for r in cur.fetchall()}
        # משוריין בבקשת העברה
        cur.execute(_q(f"""
            SELECT serial, to_branch FROM transfer_plan WHERE serial IN ({ph})
        """), tuple(serials))
        for r in cur.fetchall():
            out[str(r["serial"])] = {"kind": "reserved", "to_branch": r["to_branch"]}
        for sn, tb in transit.items():
            out[sn] = {"kind": "transit", "to_branch": tb}   # גובר על reserved
    return out


def product_branch_status(product_id: str) -> dict:
    """לכל סניף שמחזיק יחידה של המוצר שמשוריינת/בהעברה — {branch_id: {kind, to_branch, n}}.
    נגזר מ-transfer_plan (משוריין) ו-transfer_items פתוחים (בהעברה). transit גובר."""
    pid = str(product_id)
    out = {}
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("""SELECT from_branch AS fb, to_branch AS tb, COUNT(*) AS n
                          FROM transfer_plan WHERE product_id = ? GROUP BY from_branch, to_branch"""), (pid,))
        for r in cur.fetchall():
            if r["fb"] is not None:
                out[int(r["fb"])] = {"kind": "reserved", "to_branch": r["tb"], "n": r["n"]}
        cur.execute(_q("""SELECT t.from_branch_id AS fb, t.to_branch_id AS tb, COUNT(*) AS n
                          FROM transfer_items i JOIN transfers t ON t.op_id = i.op_id
                          WHERE i.product_id = ? AND i.received = 0
                            AND t.status IN ('in_transit','partial')
                          GROUP BY t.from_branch_id, t.to_branch_id"""), (pid,))
        for r in cur.fetchall():
            if r["fb"] is not None:
                out[int(r["fb"])] = {"kind": "transit", "to_branch": r["tb"], "n": r["n"]}
    return out


def serial_index_count() -> int:
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT COUNT(*) AS n FROM serial_index"))
        return cur.fetchone()["n"]


def serial_index_last_sync() -> str:
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT MAX(synced_at) AS m FROM serial_index")
        row = cur.fetchone()
        return (row["m"] if row else None) or None


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


def transfers_overview_aggregates(op_ids: list) -> dict:
    """כל אגרגטי ה-transfer_items עבור רשימת op_ids בשאילתה אחת (במקום N+1 = 4 שאילתות לכל העברה).
    מחזיר {op_id: {receivers[], manual_count, redirected, missing, search_text}}."""
    if not op_ids:
        return {}
    ids = [str(x) for x in op_ids]
    ph = ",".join("?" for _ in ids)
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q(f"""
            SELECT op_id, received, received_method, received_by, serial, barcode, name
            FROM transfer_items WHERE op_id IN ({ph})
        """), tuple(ids))
        rows = cur.fetchall()
    acc = {op: {"receivers": set(), "manual_count": 0, "redirected": 0, "missing": 0, "parts": []}
           for op in ids}
    for r in rows:
        op = str(r["op_id"])
        d = acc.get(op)
        if d is None:
            d = acc[op] = {"receivers": set(), "manual_count": 0, "redirected": 0, "missing": 0, "parts": []}
        rec = r["received"]
        if rec == 1:
            rb = r["received_by"]
            if rb and not str(rb).startswith("branch:"):
                d["receivers"].add(rb)
            if r["received_method"] in ("manual", "paste"):
                d["manual_count"] += 1
        elif rec == 2:
            d["redirected"] += 1
        elif rec == 3:
            d["missing"] += 1
        for k in ("serial", "barcode", "name"):
            v = r[k]
            if v:
                d["parts"].append(str(v))
    return {op: {"receivers": list(d["receivers"]), "manual_count": d["manual_count"],
                 "redirected": d["redirected"], "missing": d["missing"],
                 "search_text": " ".join(d["parts"])}
            for op, d in acc.items()}


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


def plan_add(lines: list) -> list:
    """מוסיף שורות לתוכנית ההעברות. line: {product_id,name,from_branch,to_branch,qty,serial?}.
    מחזיר את ה-ids של השורות שנוספו (לשידור ממוקד)."""
    ids = []
    with _conn() as c:
        cur = c.cursor()
        for ln in lines:
            vals = (str(ln.get("product_id") or ""), ln.get("name") or "",
                    int(ln.get("from_branch")), int(ln.get("to_branch")),
                    int(ln.get("qty") or 1), (ln.get("serial") or "").strip(), now_iso())
            if _USE_PG:
                cur.execute(_q("""
                    INSERT INTO transfer_plan (product_id, name, from_branch, to_branch, qty, serial, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id
                """), vals)
                ids.append(cur.fetchone()["id"])
            else:
                cur.execute(_q("""
                    INSERT INTO transfer_plan (product_id, name, from_branch, to_branch, qty, serial, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """), vals)
                ids.append(cur.lastrowid)
    return ids


def plan_replace_product(product_id, lines: list) -> int:
    """מחליף את כל שורות התוכנית למוצר אחד (מחיקה + הוספה). lines ריק = הסרת הבקשה."""
    pid = str(product_id)
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("DELETE FROM transfer_plan WHERE product_id = ?"), (pid,))
        for ln in lines:
            cur.execute(_q("""
                INSERT INTO transfer_plan (product_id, name, from_branch, to_branch, qty, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """), (pid, ln.get("name") or "", int(ln.get("from_branch")),
                   int(ln.get("to_branch")), int(ln.get("qty") or 1), now_iso()))
        return len(lines)


def plan_list() -> list:
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT * FROM transfer_plan ORDER BY from_branch, name")
        return [dict(r) for r in cur.fetchall()]


def plan_delete(pid) -> int:
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("DELETE FROM transfer_plan WHERE id = ?"), (pid,))
        return cur.rowcount if hasattr(cur, "rowcount") else 0


def plan_clear(from_branch=None):
    with _conn() as c:
        cur = c.cursor()
        if from_branch is None:
            cur.execute("DELETE FROM transfer_plan")
        else:
            cur.execute(_q("DELETE FROM transfer_plan WHERE from_branch = ?"), (int(from_branch),))


def plan_count() -> int:
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM transfer_plan")
        return cur.fetchone()["n"]


# ── שידור בקשות העברה למסך הסניף ──────────────────────────────────
# מודל: כל שורת תוכנית נושאת bcast (0/NULL=לא שודר, 1=מוצג במסך הסניף, 2=נסגר ע"י הסניף).
# במסך הסניף השורות הפעילות מקובצות לפי סניף יעד → טייל אחד לכל יעד; בקשה חדשה
# לאותו יעד מצטרפת לטייל הקיים. קליטת העברה תואמת בקופה מוחקת את השורה (plan_match_transfer).

def plan_mark_broadcast(branch_id, line_ids=None) -> int:
    """משדר: מסמן bcast=1. עם line_ids — רק שורות אלה; בלעדיהם — כל שורות הסניף (כולל שידור חוזר)."""
    with _conn() as c:
        cur = c.cursor()
        if line_ids:
            ph = ",".join("?" * len(line_ids))
            cur.execute(_q(f"UPDATE transfer_plan SET bcast = 1 WHERE id IN ({ph})"),
                        tuple(int(i) for i in line_ids))
        else:
            cur.execute(_q("UPDATE transfer_plan SET bcast = 1 WHERE from_branch = ?"), (int(branch_id),))
        n = cur.rowcount if hasattr(cur, "rowcount") else 0
        return max(n or 0, 0)


def broadcast_groups(branch_id) -> list:
    """הבקשות הפעילות לסניף, מקובצות לפי סניף יעד: [{to_branch, latest, lines:[...]}]."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("""
            SELECT * FROM transfer_plan WHERE from_branch = ? AND bcast = 1
            ORDER BY to_branch, name
        """), (int(branch_id),))
        rows = [dict(r) for r in cur.fetchall()]
    groups = {}
    for r in rows:
        g = groups.setdefault(int(r["to_branch"]), {"to_branch": int(r["to_branch"]),
                                                    "latest": "", "lines": []})
        g["lines"].append(r)
        g["latest"] = max(g["latest"], r.get("created_at") or "")
    return sorted(groups.values(), key=lambda g: g["latest"], reverse=True)


def broadcast_dismiss_group(branch_id, to_branch=None):
    """הסניף סגר טייל: bcast=2 (השורות נשארות בתוכנית). בלי to_branch — סוגר הכל."""
    with _conn() as c:
        cur = c.cursor()
        if to_branch is None:
            cur.execute(_q("UPDATE transfer_plan SET bcast = 2 WHERE from_branch = ? AND bcast = 1"),
                        (int(branch_id),))
        else:
            cur.execute(_q("""UPDATE transfer_plan SET bcast = 2
                              WHERE from_branch = ? AND to_branch = ? AND bcast = 1"""),
                        (int(branch_id), int(to_branch)))


def broadcast_branches() -> list:
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT DISTINCT from_branch AS b FROM transfer_plan WHERE bcast = 1")
        return [r["b"] for r in cur.fetchall()]


def plan_match_transfer(from_branch, to_branch, items) -> int:
    """ניקוי אוטומטי: העברה אמיתית בקופה מ-from ל-to מוחקת שורות בקשה תואמות.
    item: {product_id, serials:[...], qty}. סריאל תואם → מחיקת השורה הסריאלית;
    מוצר לא-סריאלי → הפחתת qty משורת המוצר (מחיקה כשמגיע ל-0). מחזיר כמה שורות נוקו."""
    cleaned = 0
    fb, tb = int(from_branch or 0), int(to_branch or 0)
    if not fb or not tb:
        return 0
    with _conn() as c:
        cur = c.cursor()
        for it in items or []:
            pid = str(it.get("product_id") or "")
            serials = [s for s in (it.get("serials") or []) if s]
            if serials:
                for sn in serials:
                    cur.execute(_q("""DELETE FROM transfer_plan
                                      WHERE from_branch = ? AND to_branch = ? AND serial = ?"""),
                                (fb, tb, str(sn)))
                    hit = (cur.rowcount or 0) if hasattr(cur, "rowcount") else 0
                    if hit:
                        cleaned += hit
                    else:
                        # אין בקשה על הסריאל הספציפי — יחידה סריאלית שהועברה מספקת גם בקשה כמותית על המוצר
                        cleaned += _plan_decrement(cur, fb, tb, pid, 1)
            else:
                qty = int(it.get("qty") or 0)
                if qty > 0 and pid:
                    cleaned += _plan_decrement(cur, fb, tb, pid, qty)
    return cleaned


def _plan_decrement(cur, fb, tb, pid, qty) -> int:
    """מפחית כמות משורת בקשה כמותית (ללא סריאל) של המוצר; מוחק כשמתאפסת."""
    cur.execute(_q("""SELECT id, qty FROM transfer_plan
                      WHERE from_branch = ? AND to_branch = ? AND product_id = ?
                        AND (serial IS NULL OR serial = '') ORDER BY id"""), (fb, tb, str(pid)))
    rows = [dict(r) for r in cur.fetchall()]
    cleaned = 0
    for r in rows:
        if qty <= 0:
            break
        take = min(qty, int(r["qty"] or 1))
        left = int(r["qty"] or 1) - take
        qty -= take
        if left <= 0:
            cur.execute(_q("DELETE FROM transfer_plan WHERE id = ?"), (r["id"],))
            cleaned += 1
        else:
            cur.execute(_q("UPDATE transfer_plan SET qty = ? WHERE id = ?"), (left, r["id"]))
    return cleaned


# legacy (טבלת broadcasts הישנה) — נשמר לקריאת מצב ישן בלבד בזמן מעבר גרסה
def broadcast_get(branch_id):
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT broadcast_at FROM broadcasts WHERE branch_id = ?"), (int(branch_id),))
        r = cur.fetchone()
        return r["broadcast_at"] if r else None


def broadcast_clear(branch_id):
    with _conn() as c:
        c.cursor().execute(_q("DELETE FROM broadcasts WHERE branch_id = ?"), (int(branch_id),))


def plan_for_branch(branch_id) -> list:
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT * FROM transfer_plan WHERE from_branch = ? ORDER BY name"), (int(branch_id),))
        return [dict(r) for r in cur.fetchall()]


# ── מאגר מכירות (Sales Cache) ─────────────────────────────────────
def sales_insert(rows: list) -> int:
    """הוספת שורות מכירה (idempotent לפי doc_id+line_no). row: {doc_id,line_no,product_id,name,qty,price,serial,branch_id,doc_type,sale_date}."""
    if not rows:
        return 0
    n = 0
    with _conn() as c:
        cur = c.cursor()
        for r in rows:
            cur.execute(_q("""
                INSERT INTO sales (doc_id, line_no, product_id, name, qty, price, serial, branch_id, doc_type, sale_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(doc_id, line_no) DO NOTHING
            """), (str(r.get("doc_id")), int(r.get("line_no") or 0), str(r.get("product_id") or ""),
                   r.get("name") or "", float(r.get("qty") or 0), float(r.get("price") or 0),
                   (r.get("serial") or "").strip(),
                   int(r["branch_id"]) if r.get("branch_id") not in (None, "") else None,
                   int(r.get("doc_type") or 0), r.get("sale_date") or ""))
            n += 1
    return n


def sales_state_get(k: str, default=None):
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT v FROM sales_ingest_state WHERE k = ?"), (k,))
        r = cur.fetchone()
        return r["v"] if r else default


def sales_state_set(k: str, v: str):
    with _conn() as c:
        c.cursor().execute(_q("""
            INSERT INTO sales_ingest_state (k, v) VALUES (?, ?)
            ON CONFLICT(k) DO UPDATE SET v=excluded.v
        """), (k, str(v)))


# ── 💬 וואטסאפ: מטא משלנו (מעקב/הערות) ──
def wa_star_set(phone: str, star: bool):
    with _conn() as c:
        c.cursor().execute(_q("""
            INSERT INTO wa_meta (phone, star) VALUES (?, ?)
            ON CONFLICT(phone) DO UPDATE SET star=excluded.star
        """), (phone, 1 if star else 0))


def wa_stars() -> dict:
    """{phone: 1} לכל השיחות המסומנות במעקב."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT phone, star FROM wa_meta WHERE star = 1"))
        return {r["phone"]: 1 for r in cur.fetchall()}


def wa_note_add(phone: str, text: str, author: str = "") -> int:
    with _conn() as c:
        cur = c.cursor()
        vals = (phone, text, author, now_iso())
        if _USE_PG:
            cur.execute(_q("""
                INSERT INTO wa_notes (phone, text, author, created_at)
                VALUES (?, ?, ?, ?) RETURNING id
            """), vals)
            return cur.fetchone()["id"]
        cur.execute(_q("""
            INSERT INTO wa_notes (phone, text, author, created_at)
            VALUES (?, ?, ?, ?)
        """), vals)
        return cur.lastrowid


def wa_notes_list(phone: str) -> list:
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("""
            SELECT id, text, author, created_at FROM wa_notes
            WHERE phone = ? ORDER BY id DESC
        """), (phone,))
        return [dict(r) for r in cur.fetchall()]


def wa_note_delete(nid: int) -> bool:
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("DELETE FROM wa_notes WHERE id = ?"), (nid,))
        return cur.rowcount > 0


def sales_docids_since(since_prefix: str) -> set:
    """doc_ids שכבר נקלטו עם sale_date >= since_prefix (לדילוג באיסוף)."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT DISTINCT doc_id FROM sales WHERE sale_date >= ?"), (since_prefix,))
        return {str(r["doc_id"]) for r in cur.fetchall()}


def sales_summary() -> dict:
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT COUNT(*) AS lines, COUNT(DISTINCT doc_id) AS docs, MIN(sale_date) AS min_d, MAX(sale_date) AS max_d FROM sales")
        r = dict(cur.fetchone())
        return {"lines": r.get("lines") or 0, "docs": r.get("docs") or 0,
                "min_date": r.get("min_d"), "max_date": r.get("max_d")}


def sales_aggregate(since_date: str, branch_id=None) -> dict:
    """סך מכירה נטו לכל מוצר מאז תאריך (כולל). מחזיר {product_id: {qty, last_date, branches:{b:qty}}}.
    qty חתום (החזרות שליליות) → נטו. since_date בפורמט ISO (משווים על prefix sale_date)."""
    out = {}
    with _conn() as c:
        cur = c.cursor()
        sql = "SELECT product_id, branch_id, qty, sale_date FROM sales WHERE sale_date >= ?"
        params = [since_date]
        if branch_id not in (None, "", "all"):
            sql += " AND branch_id = ?"
            params.append(int(branch_id))
        cur.execute(_q(sql), tuple(params))
        for r in cur.fetchall():
            pid = str(r["product_id"])
            d = out.setdefault(pid, {"qty": 0.0, "last_date": "", "branches": {}})
            q = float(r["qty"] or 0)
            d["qty"] += q
            b = r["branch_id"]
            if b is not None:
                d["branches"][int(b)] = d["branches"].get(int(b), 0.0) + q
            sd = r["sale_date"] or ""
            if sd > d["last_date"]:
                d["last_date"] = sd
    return out


# ── הורדות מלאי מרלוג (removals) ───────────────────────────────────
def removals_insert(rows: list) -> int:
    """row: {op_id,line_no,product_id,name,qty,serials,employee,branch_id,removed_at}. idempotent."""
    if not rows:
        return 0
    n = 0
    with _conn() as c:
        cur = c.cursor()
        for r in rows:
            cur.execute(_q("""
                INSERT INTO removals (op_id, line_no, product_id, name, qty, serials, employee, note, branch_id, removed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(op_id, line_no) DO NOTHING
            """), (str(r.get("op_id")), int(r.get("line_no") or 0), str(r.get("product_id") or ""),
                   r.get("name") or "", float(r.get("qty") or 0), r.get("serials") or "",
                   r.get("employee") or "", r.get("note") or "",
                   int(r["branch_id"]) if r.get("branch_id") not in (None, "") else None,
                   r.get("removed_at") or ""))
            n += 1
    return n


def removals_opids_since(since_prefix: str) -> set:
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT DISTINCT op_id FROM removals WHERE removed_at >= ?"), (since_prefix,))
        return {str(r["op_id"]) for r in cur.fetchall()}


def removals_aggregate(since_date: str, branch_id=None) -> dict:
    """סך כמות שהורדה לכל מוצר מאז תאריך. {product_id: qty}. (מרלוג בלבד = מכירות בפועל)."""
    out = {}
    with _conn() as c:
        cur = c.cursor()
        sql = "SELECT product_id, qty FROM removals WHERE removed_at >= ?"
        params = [since_date]
        if branch_id not in (None, "", "all"):
            sql += " AND branch_id = ?"
            params.append(int(branch_id))
        cur.execute(_q(sql), tuple(params))
        for r in cur.fetchall():
            pid = str(r["product_id"])
            out[pid] = out.get(pid, 0.0) + float(r["qty"] or 0)
    return out


def removals_list(from_date: str, to_date: str = None) -> list:
    """שורות הורדה בטווח (removed_at בין from..to). מוחזר ממוין מהחדש לישן."""
    with _conn() as c:
        cur = c.cursor()
        if to_date:
            cur.execute(_q("""SELECT * FROM removals WHERE removed_at >= ? AND removed_at <= ?
                              ORDER BY removed_at DESC"""), (from_date, to_date + "T99"))
        else:
            cur.execute(_q("SELECT * FROM removals WHERE removed_at >= ? ORDER BY removed_at DESC"),
                        (from_date,))
        return [dict(r) for r in cur.fetchall()]


def removals_summary() -> dict:
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT COUNT(*) AS lines, COUNT(DISTINCT op_id) AS ops, MIN(removed_at) AS mn, MAX(removed_at) AS mx FROM removals")
        r = dict(cur.fetchone())
        return {"lines": r.get("lines") or 0, "ops": r.get("ops") or 0,
                "min_date": r.get("mn"), "max_date": r.get("mx")}


# ── טיוטת הזמנה (order_plan) — רשימה שטוחה ─────────────────────────
def order_add(lines: list) -> int:
    with _conn() as c:
        cur = c.cursor()
        for ln in lines:
            cur.execute(_q("""
                INSERT INTO order_plan (product_id, name, qty, supplier, category, kind, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """), (str(ln.get("product_id") or ""), ln.get("name") or "", int(ln.get("qty") or 1),
                   ln.get("supplier") or "", ln.get("category") or "", ln.get("kind") or "", now_iso()))
        return len(lines)


def order_replace_product(product_id, lines: list) -> int:
    """מחליף את שורת ההזמנה למוצר (מחיקה+הוספה). lines ריק = הסרה."""
    pid = str(product_id)
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("DELETE FROM order_plan WHERE product_id = ?"), (pid,))
        for ln in lines:
            cur.execute(_q("""
                INSERT INTO order_plan (product_id, name, qty, supplier, category, kind, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """), (pid, ln.get("name") or "", int(ln.get("qty") or 1),
                   ln.get("supplier") or "", ln.get("category") or "", ln.get("kind") or "", now_iso()))
        return len(lines)


def order_list() -> list:
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT * FROM order_plan ORDER BY supplier, name")
        return [dict(r) for r in cur.fetchall()]


def order_delete(pid_row) -> int:
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("DELETE FROM order_plan WHERE id = ?"), (pid_row,))
        return cur.rowcount if hasattr(cur, "rowcount") else 0


def order_clear():
    with _conn() as c:
        c.cursor().execute("DELETE FROM order_plan")


def order_count() -> int:
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM order_plan")
        return cur.fetchone()["n"]


def order_product_ids() -> set:
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT DISTINCT product_id FROM order_plan")
        return {str(r["product_id"]) for r in cur.fetchall()}


# ── קטלוג מוצרים (cache ב-DB) ──────────────────────────────────────
def catalog_replace(rows: list):
    """מחליף את כל הקטלוג. row: {product_id,name,stock,supplier,category,kind,barcode,active}."""
    ts = now_iso()
    with _conn() as c:
        cur = c.cursor()
        cur.execute("DELETE FROM catalog")
        for r in rows:
            cur.execute(_q("""
                INSERT INTO catalog (product_id, name, stock, supplier, category, kind, barcode, active, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """), (str(r.get("product_id")), r.get("name") or "", float(r.get("stock") or 0),
                   r.get("supplier") or "", r.get("category") or "", r.get("kind") or "",
                   r.get("barcode") or "", 1 if r.get("active") else 0, ts))


def catalog_load() -> dict:
    """{pid: {name,stock,supplier,category,kind,barcode,active}}."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT * FROM catalog")
        out = {}
        for r in cur.fetchall():
            d = dict(r)
            out[str(d["product_id"])] = {
                "name": d["name"], "stock": d["stock"], "supplier": d["supplier"],
                "category": d["category"], "kind": d["kind"], "barcode": d["barcode"],
                "active": bool(d["active"]),
            }
        return out


def catalog_meta() -> dict:
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT COUNT(*) AS n, MAX(updated_at) AS u FROM catalog")
        r = dict(cur.fetchone())
        return {"count": r.get("n") or 0, "updated_at": r.get("u")}


# ── מכשירים מאושרים (device allowlist) ─────────────────────────────
def device_get(token: str):
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT * FROM devices WHERE token = ?"), (str(token),))
        return _row_to_dict(cur.fetchone())


def device_register(token, name, ua, ip, branch_hint, status, auto=False):
    with _conn() as c:
        c.cursor().execute(_q("""
            INSERT INTO devices (token, name, status, auto, ua, ip, branch_hint, created_at, last_seen, approved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(token) DO NOTHING
        """), (str(token), name or "", status, 1 if auto else 0, ua or "", ip or "",
               str(branch_hint or ""), now_iso(), now_iso(),
               now_iso() if status == "approved" else None))


def device_set_status(token: str, status: str) -> bool:
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("""UPDATE devices SET status = ?, approved_at = ?
                          WHERE token = ?"""),
                    (status, now_iso() if status == "approved" else None, str(token)))
        return bool(cur.rowcount)


def device_touch(token: str, branch_id=None):
    """מעדכן last_seen, ואם סופק branch_id — גם את הסניף הנוכחי (כדי שלוח
    ניהול המכשירים יראה מאיפה המכשיר עובד עכשיו, לא איפה שנרשם לראשונה)."""
    with _conn() as c:
        cur = c.cursor()
        if branch_id:
            cur.execute(_q("UPDATE devices SET last_seen = ?, branch_hint = ? WHERE token = ?"),
                        (now_iso(), str(branch_id), str(token)))
        else:
            cur.execute(_q("UPDATE devices SET last_seen = ? WHERE token = ?"),
                        (now_iso(), str(token)))


def device_list() -> list:
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT * FROM devices ORDER BY created_at DESC")
        return [dict(r) for r in cur.fetchall()]


def catalog_light() -> list:
    """קטלוג מצומצם לחיפוש בצד הלקוח (טאב מלאי חי).
    `stock` (סך כל הסניפים, מהרענון האחרון) משמש רק לסינון/מיון "רק עם מלאי" —
    הכמויות המוצגות עצמן תמיד נקראות חי."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT product_id, name, barcode, kind, category, supplier, active, stock FROM catalog")
        return [{"product_id": r["product_id"], "name": r["name"], "barcode": r["barcode"],
                 "kind": r["kind"], "category": r["category"], "supplier": r["supplier"],
                 "active": bool(r["active"]), "stock": r["stock"] or 0} for r in cur.fetchall()]


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
