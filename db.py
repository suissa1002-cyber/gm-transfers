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

_pool = None
if _USE_PG:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
    # מאגר חיבורים — חיבורים נשמרים וממוחזרים במקום לפתוח חיבור חדש (TLS+auth+
    # search_path) בכל שאילתה. זה היה צוואר הבקבוק: טעינת הזמנות = עשרות שאילתות
    # קטנות × ~120ms חיבור כל אחת. עם pool — מילישניות. max_size קטן כי חולקים
    # instance עם stock_watcher.
    def _pg_configure(c):
        # מגדיר search_path פעם אחת לכל חיבור חדש ב-pool, ומקבע (commit) כדי
        # שלא יתאפס ב-rollback של טרנזקציה מאוחרת.
        c.execute(f"SET search_path TO {_PG_SCHEMA}")
        c.commit()

    # max_size מוגדר ב-env כי web ו-worker חולקים את ה-Postgres (+ stock_watcher);
    # מגבילים סך החיבורים. web=8, worker=4 (פחות מקביליות).
    _POOL_MAX = int(os.getenv("DB_POOL_MAX", "8"))
    _pool = ConnectionPool(
        cfg.DATABASE_URL, min_size=1, max_size=_POOL_MAX, timeout=20,
        kwargs={"row_factory": dict_row, "autocommit": False},
        configure=_pg_configure, open=True,
    )
else:
    import sqlite3


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _q(sql: str) -> str:
    """המרת placeholders: SQLite משתמש ב-? , Postgres ב-%s ."""
    return sql.replace("?", "%s") if _USE_PG else sql


@contextmanager
def _conn():
    """חיבור DB עם dict-rows. Postgres — מתוך ה-pool (חיבורים ממוחזרים, ללא נעילה
    גלובלית → web ו-jobs רצים במקביל). SQLite — חיבור-לכל-קריאה תחת נעילה (כותב יחיד)."""
    if _USE_PG:
        with _pool.connection() as conn:      # search_path מוגדר ב-configure ביצירת החיבור
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    else:
        with _lock:
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
        is_stock    INTEGER,
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
    # תשובות מוכנות (canned replies) לשליחה מהירה בוואטסאפ
    """
    CREATE TABLE IF NOT EXISTS wa_canned (
        id          {pk},
        title       TEXT,
        text        TEXT,
        created_at  TEXT
    )
    """.format(pk=_PK),
    # קישורי תשלום מהירים (ללא הזמנת WC) — קונסולת מעקב: סטטוס, אישור, 4 ספרות
    """
    CREATE TABLE IF NOT EXISTS pay_links (
        id          {pk},
        pru         TEXT,
        descr       TEXT,
        amount      REAL,
        name        TEXT,
        phone       TEXT,
        status      TEXT DEFAULT 'pending',
        tx          TEXT,
        approval    TEXT,
        four_digits TEXT,
        brand       TEXT,
        created_at  TEXT,
        paid_at     TEXT
    )
    """.format(pk=_PK),
    # יומן צל להודעות שנשלחו ישירות דרך Meta Cloud API (תבניות כפתור, reply) —
    # הן לא מופיעות בשיחה של ConnectOp, אז נשמרות כאן וממוזגות לתצוגת השיחה
    """
    CREATE TABLE IF NOT EXISTS wa_shadow (
        id            {pk},
        phone         TEXT,
        wamid         TEXT,
        text          TEXT,
        reply_to      TEXT,
        reply_preview TEXT,
        ts            BIGINT,
        created_at    TEXT
    )
    """.format(pk=_PK),
    "CREATE INDEX IF NOT EXISTS idx_wa_shadow_phone ON wa_shadow(phone)",
    # ── מערכת התראות משמרות: עובדים רשומים (שם ↔ telegram_id) לקבלת DM על שידור העברה ──
    """
    CREATE TABLE IF NOT EXISTS shift_employees (
        id            {pk},
        name          TEXT,
        telegram_id   TEXT UNIQUE,
        registered_at TEXT
    )
    """.format(pk=_PK),
    "CREATE INDEX IF NOT EXISTS idx_shift_emp_name ON shift_employees(name)",
    # ── סידור עבודה שבועי (מזין את התראות המשמרת) ──
    """
    CREATE TABLE IF NOT EXISTS shift_roster (
        id         {pk},
        branch_id  INTEGER,
        dow        INTEGER,
        employee   TEXT,
        hours      TEXT,
        updated_at TEXT
    )
    """.format(pk=_PK),
    "CREATE INDEX IF NOT EXISTS idx_shift_roster_bd ON shift_roster(branch_id, dow)",
    # ── התראות משמרת שנדחו (שודרו מחוץ לשעות) — נשלחות בבוקר הפתיחה ──
    """
    CREATE TABLE IF NOT EXISTS pending_shift_alerts (
        id         {pk},
        branch_id  INTEGER,
        text       TEXT,
        created_at TEXT
    )
    """.format(pk=_PK),
    # ── יומן שיחות טלפון ממרכזיית 1com (webhook) — זיהוי לקוח לפי מספר ──
    """
    CREATE TABLE IF NOT EXISTS pbx_calls (
        id           {pk},
        phone        TEXT,
        direction    TEXT,
        uid          TEXT,
        matched_name TEXT,
        orders       INTEGER,
        last_status  TEXT,
        ts           TEXT
    )
    """.format(pk=_PK),
    "CREATE INDEX IF NOT EXISTS idx_pbx_calls_phone ON pbx_calls(phone)",
    # נתיב שיחה חי שנלכד מ-CHANNELS (worker רקע) — מדויק לכל שיחה כולל שלא נענו.
    # מקור האמת לתיוג היסטוריה/אנליטיקה (CDR לבדו לא יכול לשחזר סניף לשיחה שלא נענתה).
    # handled_at = סומן 'טופל' במעקב מחמצות; answered=1 אם נציג ענה בפועל.
    """
    CREATE TABLE IF NOT EXISTS pbx_route (
        uid        TEXT PRIMARY KEY,
        phone      TEXT,
        route      TEXT,
        branch     TEXT,
        answered   INTEGER DEFAULT 0,
        first_ts   TEXT,
        last_ts    TEXT,
        handled_at TEXT,
        note       TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pbx_route_phone ON pbx_route(phone)",
    # ── WhatsApp עצמאי (פרויקט ניתוק קונקטופ) — חנות ההודעות שלנו ──
    # מתמלאת מ-webhook ישיר של מטא. כל הודעה (נכנסת/יוצאת) + מדיה + סטטוס מסירה.
    """
    CREATE TABLE IF NOT EXISTS wa_msg (
        id            {pk},
        wamid         TEXT,
        phone         TEXT,
        direction     TEXT,
        type          TEXT,
        text          TEXT,
        media_id      TEXT,
        media_mime    TEXT,
        media_name    TEXT,
        media_url     TEXT,
        reply_to      TEXT,
        ts            BIGINT,
        status        TEXT,
        raw           TEXT,
        created_at    TEXT
    )
    """.format(pk=_PK),
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_wa_msg_wamid ON wa_msg(wamid)",
    "CREATE INDEX IF NOT EXISTS idx_wa_msg_phone_ts ON wa_msg(phone, ts)",
    # אנשי קשר + מצב חלון 24ש (last_in_ts) + דגלים
    """
    CREATE TABLE IF NOT EXISTS wa_contact (
        phone        TEXT PRIMARY KEY,
        name         TEXT,
        wa_id        TEXT,
        last_in_ts   BIGINT,
        last_msg_ts  BIGINT,
        live_chat    INTEGER DEFAULT 0,
        unread       INTEGER DEFAULT 0,
        archived     INTEGER DEFAULT 0,
        updated_at   TEXT
    )
    """,
    # מעקב backfill — אילו שיחות כבר נשאבו (resumable: לדלג עליהן בריצה הבאה)
    "CREATE TABLE IF NOT EXISTS wa_backfill_done (phone TEXT PRIMARY KEY, done_at TEXT)",
    # שליחה מתוזמנת — "שלח בשעה X". רץ בצד שרת (GreenOS תמיד פעיל), לא תלוי בסשן/אורי.
    """
    CREATE TABLE IF NOT EXISTS wa_scheduled (
        id           {pk},
        phone        TEXT,
        text         TEXT,
        name         TEXT,
        order_number TEXT,
        total        TEXT,
        pru          TEXT,
        descr        TEXT,
        send_at      TEXT,
        status       TEXT DEFAULT 'pending',
        created_by   TEXT,
        created_at   TEXT,
        sent_at      TEXT,
        via          TEXT,
        err          TEXT
    )
    """.format(pk=_PK),
    # שינוי סטטוס הזמנה מתוזמן (רץ בשרת — לא תלוי ב-Claude/מחשב פתוח)
    """
    CREATE TABLE IF NOT EXISTS scheduled_status (
        id           {pk},
        order_id     TEXT,
        order_number TEXT,
        status       TEXT,
        status_label TEXT,
        run_at       TEXT,
        state        TEXT DEFAULT 'pending',
        created_by   TEXT,
        created_at   TEXT,
        done_at      TEXT,
        err          TEXT
    )
    """.format(pk=_PK),
    # שלב 6 — בוט native: מצב שיחה לכל לקוח (איפה הוא בעץ הזרימה)
    """
    CREATE TABLE IF NOT EXISTS wa_bot_session (
        phone       TEXT PRIMARY KEY,
        state       TEXT,
        data        TEXT,
        updated_at  TEXT
    )
    """,
    # חשבוניות לקוח שנקלטו ממייל (הקופה שולחת עותק מקור ל-greenmobile.eshop@gmail)
    # — לשליחה חוזרת ללקוח בוואטסאפ בלי להיכנס לקופה. ה-PDF נשמר base64.
    """
    CREATE TABLE IF NOT EXISTS customer_invoices (
        id            {pk},
        doc_number    TEXT,
        doc_type      TEXT,
        total         TEXT,
        issued_date   TEXT,
        customer_name TEXT,
        customer_phone TEXT,
        order_number  TEXT,
        filename      TEXT,
        subject       TEXT,
        email_uid     TEXT,
        pdf_b64       TEXT,
        captured_at   TEXT
    )
    """.format(pk=_PK),
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_cust_inv_uid ON customer_invoices(email_uid)",
    "CREATE INDEX IF NOT EXISTS idx_cust_inv_phone ON customer_invoices(customer_phone)",
    "CREATE INDEX IF NOT EXISTS idx_cust_inv_doc ON customer_invoices(doc_number)",
    # תור משימות לסוכן אורי (claude על המק של אסי, חיוב Max — לא API)
    """
    CREATE TABLE IF NOT EXISTS uri_jobs (
        id          {pk},
        phone       TEXT,
        question    TEXT,
        status      TEXT DEFAULT 'pending',
        answer      TEXT,
        created_at  TEXT,
        answered_at TEXT
    )
    """.format(pk=_PK),
    # מנויי Web Push (PWA באייפון/דסקטופ) להתראות וואטסאפ
    """
    CREATE TABLE IF NOT EXISTS wa_push_subs (
        id          {pk},
        endpoint    TEXT UNIQUE,
        sub         TEXT,
        ua          TEXT,
        created_at  TEXT
    )
    """.format(pk=_PK),
    "CREATE INDEX IF NOT EXISTS idx_misroutes_serial ON misroutes(serial)",
    "CREATE INDEX IF NOT EXISTS idx_misroutes_status ON misroutes(status)",
    "CREATE INDEX IF NOT EXISTS idx_transfers_to ON transfers(to_branch_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_sales_product ON sales(product_id, sale_date)",
    "CREATE INDEX IF NOT EXISTS idx_sales_date ON sales(sale_date)",
    "CREATE INDEX IF NOT EXISTS idx_removals_product ON removals(product_id, removed_at)",
    "CREATE INDEX IF NOT EXISTS idx_removals_date ON removals(removed_at)",
    # גיבוי מדיה נכנסת ממטא (תמונות/מסמכים) — base64, כדי שלא יאבד כשמטא ימחק (~30 יום)
    """
    CREATE TABLE IF NOT EXISTS wa_media_blob (
        wamid       TEXT PRIMARY KEY,
        mime        TEXT,
        b64         TEXT,
        created_at  TEXT
    )
    """,
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
        ("transfer_plan",  "created_by", "TEXT"),
        ("transfer_plan",  "note", "TEXT"),     # הערה חופשית לסניף (רשמי/eSIM וכו')
        ("wa_msg",         "err", "TEXT"),       # סיבת כשל מסירה ממטא (code · title)
        ("shift_roster",   "week_start", "TEXT"), # תאריך ראשון של השבוע (YYYY-MM-DD) — סידור מתוארך
        ("pbx_calls",      "route", "TEXT"),       # היעד בשיחה (סיטי/מעבדה/הזמנות...) מ-1com
        ("pbx_calls",      "order_number", "TEXT"),
        ("pbx_calls",      "items", "TEXT"),
        # נעילת סניף: הסניף המאושר של המכשיר — שינוי רק באישור מנהל (טלגרם)
        ("devices",        "branch_locked", "TEXT"),
        # is_stock=0 → מוצר דיגיטלי/לא-מנוהל-מלאי (גיפט קארד/קוד) — מדלגים על שידור/OOS
        ("catalog",        "is_stock", "INTEGER"),
        # מספר הזמנת אתר מתוך החשבונית — מקשר חשבונית→הזמנה→לקוח
        ("customer_invoices", "order_number", "TEXT"),
        # מקור משימת אורי: panel (טיוטה לאסי) / bot (תשובה אוטומטית ללקוח)
        ("uri_jobs", "source", "TEXT"),
        ("pbx_route", "note", "TEXT"),    # הערה פנימית על שיחה (תיעוד/איכות מענה)
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


def resolve_transfer_received(op_id, by: str = "") -> dict:
    """תיקון ידני (אדמין): כרטיס שנסגר-כחוסר אך המכשיר בפועל הגיע/תקין → כל הפריטים
    מסומנים נקלטו (received=1) וההעברה → 'received' (יוצאת מלוח 'נסגר חוסר')."""
    op_id = str(op_id)
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("UPDATE transfer_items SET received = 1, "
                       "received_at = COALESCE(received_at, ?) WHERE op_id = ?"),
                    (now_iso(), op_id))
        cur.execute(_q("SELECT COUNT(*) AS n FROM transfer_items WHERE op_id = ?"), (op_id,))
        n = cur.fetchone()["n"]
        cur.execute(_q("""UPDATE transfers SET status='received', received_units=?,
                       received_at=COALESCE(received_at, ?), closed_by=? WHERE op_id = ?"""),
                    (n, now_iso(), by, op_id))
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


def receive_item_manual(item_id, op_id, employee=None) -> dict:
    """קליטה ידנית של פריט **ללא סריאל וללא ברקוד** (אין מה לסרוק — למשל מארז Corsair).
    מסמן את השורה הספציפית כנקלטה (received=1, method=manual). ⚠️ גארד: רק פריט שאין לו
    serial וגם אין barcode — כדי שלא יעקפו סריקה של פריט סידורי. מחזיר {matched, transfer}."""
    received_by = (employee or "").strip() or "ידני"
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT * FROM transfer_items WHERE id = ? AND op_id = ?"),
                    (item_id, str(op_id)))
        item = _row_to_dict(cur.fetchone())
        if not item:
            return {"matched": False, "message": "הפריט לא נמצא בהעברה"}
        if int(item.get("received") or 0) != 0:
            return {"matched": False, "message": "הפריט כבר נקלט"}
        if (item.get("serial") or "").strip() or (item.get("barcode") or "").strip():
            return {"matched": False, "message": "לפריט יש סריאל/ברקוד — יש לסרוק, לא לקלוט ידנית"}
        cur.execute(_q("""UPDATE transfer_items SET received = 1, received_at = ?,
                          received_by = ?, received_method = 'manual' WHERE id = ?"""),
                    (now_iso(), received_by, item["id"]))
        _recount(cur, str(op_id))
    return {"matched": True, "transfer": get_transfer(str(op_id)), "message": "✓ נקלט ידנית"}


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


def _sale_near_or_after(sale_date: str, start: str, grace_min: int = 180) -> bool:
    """האם המכירה אירעה סביב/אחרי תחילת ההעברה (פרסור תאריכים עמיד, לא השוואת
    מחרוזות). חלון חסד לאחור (grace_min) — מכירה ויצירת העברה יכולות לקרות באותה
    דקה. תאריך לא-פריק → לא חוסם (עדיף לסגור מאשר להשאיר תקוע)."""
    from datetime import datetime, timedelta
    if not start:
        return True

    def _p(s):
        s = (s or "").strip()
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            try:
                return datetime.fromisoformat(s[:19])
            except Exception:  # noqa: BLE001
                return None
    sd, st = _p(sale_date), _p(start)
    if sd is None or st is None:
        return True
    if sd.tzinfo is None or st.tzinfo is None:   # נרמול: אם אחד naive — משווים בלי tz
        sd, st = sd.replace(tzinfo=None), st.replace(tzinfo=None)
    return sd >= st - timedelta(minutes=grace_min)


def reconcile_sold_transfer_items() -> list:
    """סוגר אוטומטית כרטיסי קליטה שהמכשיר שלהם נמכר לפני שבוצעה קליטה.
    עובר על פריטי-העברה פתוחים (received=0, עם סיריאלי, בהעברות in_transit/partial);
    אם הסיריאלי מופיע כמכירה ב-NewOrder (doc_type=0, qty>0) אחרי תחילת ההעברה —
    מסמן את הפריט כנקלט (received=1 אם נמכר ביעד, =2 אם נמכר בסניף אחר), מעדכן/סוגר
    את ההעברה, ומחזיר רשימה להתראה. שני המקרים מנקים את הכרטיס מהדשבורד."""
    out = []
    with _conn() as c:
        cur = c.cursor()
        # פריטים לריפוי: (א) פתוחים שלא נקלטו (received=0, in_transit/partial); וגם
        # (ב) פריטים שסומנו **חוסר** (received=3) בהעברה שנסגרה לאחרונה — race: ההעברה
        # נסגרה-כחוסר לפני שהמכירה נקלטה (כמו 13933, פער שניות). _sale_near_or_after
        # עדיין מוודא שהמכירה סביב תחילת ההעברה, אז גם חוסר ישן לא ייתפס בטעות.
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
        cur.execute(_q("""
            SELECT ti.id AS item_id, ti.op_id, ti.serial, ti.name, ti.received,
                   t.to_branch_id, t.from_branch_id,
                   COALESCE(t.first_seen, t.created_at) AS t_start
            FROM transfer_items ti
            JOIN transfers t ON t.op_id = ti.op_id
            WHERE ti.serial IS NOT NULL AND ti.serial <> ''
              AND ( (ti.received = 0 AND t.status IN ('in_transit', 'partial'))
                 OR (ti.received = 3 AND t.status = 'closed'
                     AND COALESCE(t.created_at, '') >= ?) )
        """), (cutoff,))
        items = [dict(r) for r in cur.fetchall()]
        for it in items:
            start = it.get("t_start") or ""
            # match = {when, branch_id, via} מאחד מכירה (sales) או הורדת-מלאי (removals).
            # לא מסננים תאריך ב-SQL (השוואת מחרוזות שבירה — פורמט/אזור-זמן שונים בין
            # createDate ל-first_seen); לוקחים את האחרונה ומסננים בזמן אמיתי עם _sale_near_or_after.
            match = None
            # (1) מכירה רגילה ב-NewOrder (מסמך מכירה, doc_type=0)
            cur.execute(_q("""
                SELECT branch_id, sale_date FROM sales
                WHERE serial = ? AND doc_type = 0 AND qty > 0
                ORDER BY sale_date DESC LIMIT 1
            """), (it["serial"],))
            row = cur.fetchone()
            if row:
                s = dict(row)
                # המכירה צריכה להיות סביב/אחרי תחילת ההעברה — חלון חסד, כי מכירה בקופה
                # ויצירת ההעברה יכולות לקרות באותה דקה (בכל סדר), כמו 13933 (פער 3 שניות).
                if _sale_near_or_after(s.get("sale_date"), start, grace_min=180):
                    match = {"when": s.get("sale_date"), "branch_id": s.get("branch_id"), "via": "נמכר"}
            # (2) הורדת-מלאי ידנית (טבלת removals; סניף 3 עובד בחצי-קופה → הורדה = מכירה
            #     בפועל). לא ב-sales/doc_type, אז (1) מפספס — בלי זה ההעברה נתקעת לנצח
            #     (op 14119, Apple Watch, 25/06). serials מאוחסן מופרד-פסיק → LIKE כפרה-פילטר
            #     + בדיקת-טוקן מדויקת (סיריאלי לא תת-מחרוזת של אחר).
            if not match:
                cur.execute(_q("""SELECT branch_id, removed_at, serials FROM removals
                                  WHERE serials LIKE ? ORDER BY removed_at DESC"""),
                            ("%" + it["serial"] + "%",))
                for rr in cur.fetchall():
                    r = dict(rr)
                    toks = [x.strip() for x in (r.get("serials") or "").split(",")]
                    if it["serial"] in toks and _sale_near_or_after(r.get("removed_at"), start, grace_min=180):
                        match = {"when": r.get("removed_at"), "branch_id": r.get("branch_id"), "via": "הורד מהמלאי"}
                        break
            if not match:
                continue
            sold_b = match["branch_id"]
            when = match["when"]
            via = match["via"]
            try:
                same = sold_b is not None and int(sold_b) == int(it["to_branch_id"])
            except (TypeError, ValueError):
                same = False
            recv = 1 if same else 2
            cur.execute(_q("""
                UPDATE transfer_items
                SET received = ?, received_at = ?, received_by = ?, received_method = ?
                WHERE id = ?
            """), (recv, when or now_iso(), f"{via} (אוטומטי)",
                   via if same else f"{via} בסניף אחר", it["item_id"]))
            rec, total, status = _recount(cur, it["op_id"])
            # אם ההעברה נסגרה-כחוסר ועכשיו **אין יותר חוסרים** (כל הפריטים נקלטו/נמכרו,
            # received∈{1,2}) — מקדמים ל-'received' כדי שתצא מ'באיחור' ומ'נסגר(חוסר)'.
            cur.execute(_q("""SELECT COUNT(*) AS n FROM transfer_items
                              WHERE op_id = ? AND received NOT IN (1, 2)"""), (it["op_id"],))
            if cur.fetchone()["n"] == 0:
                cur.execute(_q("""UPDATE transfers
                    SET status='received', received_at=COALESCE(received_at, ?)
                    WHERE op_id = ?"""), (when or now_iso(), it["op_id"]))
                status = "received"
            out.append({
                "serial": it["serial"], "name": it.get("name") or "", "op_id": it["op_id"],
                "to_branch_id": it["to_branch_id"], "sold_branch_id": sold_b,
                "same_branch": same, "sale_date": when, "via": via,
                "transfer_closed": status in ("received", "closed"),
                "was_missing": it.get("received") == 3,  # תוקן מחוסר-שקרי → נמכר/הורד
            })
    return out


def promote_resolved_closed_transfers() -> int:
    """העברות שנסגרו-כחוסר אך כעת **כל** פריטיהן נקלטו/נמכרו (received∈{1,2}) → 'received'
    (יוצאות מ'באיחור' ומתווית 'נסגר חוסר'). מטפל גם בהעברות שרוקנו לפני התיקון (כמו 13933)."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("""
            SELECT t.op_id FROM transfers t
            WHERE t.status = 'closed'
              AND EXISTS (SELECT 1 FROM transfer_items ti WHERE ti.op_id = t.op_id)
              AND NOT EXISTS (SELECT 1 FROM transfer_items ti
                              WHERE ti.op_id = t.op_id AND ti.received NOT IN (1, 2))
        """))
        ops = [r["op_id"] for r in cur.fetchall()]
        for op in ops:
            cur.execute(_q("""UPDATE transfers SET status='received',
                received_at = COALESCE(received_at, ?) WHERE op_id = ?"""), (now_iso(), op))
        return len(ops)


def promote_fully_resolved_transfers() -> int:
    """העברה **פתוחה** (in_transit/partial) שכל פריטיה נפתרו — received∈{1,2,4} (נקלט/נמכר/
    בומרנג) → 'received'. תופס את הפער הזמני: שורת בומרנג סומנה received=4 אך פריטים אחרים
    באותה העברה נקלטו רק מאוחר יותר, ו-_recount (שסופר רק 1,2) השאיר את הכרטיס 'partial'
    באיחור לנצח (op 13987: 6 נקלטו + iPad בומרנג=4, נתקע 6/7)."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("""
            SELECT t.op_id FROM transfers t
            WHERE t.status IN ('in_transit', 'partial')
              AND EXISTS (SELECT 1 FROM transfer_items ti WHERE ti.op_id = t.op_id)
              AND EXISTS (SELECT 1 FROM transfer_items ti
                          WHERE ti.op_id = t.op_id AND ti.received = 4)
              AND NOT EXISTS (SELECT 1 FROM transfer_items ti
                              WHERE ti.op_id = t.op_id AND ti.received NOT IN (1, 2, 4))
        """))
        ops = [r["op_id"] for r in cur.fetchall()]
        for op in ops:
            cur.execute(_q("""UPDATE transfers SET status='received',
                received_at = COALESCE(received_at, ?) WHERE op_id = ?"""), (now_iso(), op))
        return len(ops)


def reconcile_boomerang_transfers() -> list:
    """בומרנג: מכשיר שיצא בהעברה A (X→Y, received=0) ונשלח **בחזרה למקור** בהעברה פתוחה
    מאוחרת יותר B (Y→X, received=0) — מבלי שנקלט אי-פעם בדרך. כלומר חזר למקור בלי שנסרק
    בשום שלב, ולכן תקוע 'בהעברה' בשני הכיוונים. סוגר את שתי השורות (received=4 'בומרנג')
    ומקדם כל העברה ל-'received' אם כל פריטיה נפתרו (1/2/4).
    ⚠️ גארד כפול נגד false-positive: (א) שתי השורות חייבות received=0 — אם המכשיר נקלט/
    נמכר אי-שם, לא נוגעים; (ב) הענפים חייבים להיות **הפוכים מדויקים** (יעד B=מקור A
    ולהפך) ו-B מאוחר מ-A. סריאל ייחודי למכשיר → סיכון התנגשות זניח. מחזיר רשימה להתראה."""
    out = []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("""
            SELECT a.id AS a_item, a.op_id AS a_op, a.serial AS serial, a.name AS name,
                   ta.from_branch_id AS x, ta.to_branch_id AS y,
                   b.id AS b_item, b.op_id AS b_op
            FROM transfer_items a
            JOIN transfers ta ON ta.op_id = a.op_id
            JOIN transfer_items b ON b.serial = a.serial AND b.op_id <> a.op_id
            JOIN transfers tb ON tb.op_id = b.op_id
            WHERE a.serial IS NOT NULL AND a.serial <> ''
              AND a.received = 0 AND b.received = 0
              AND ta.status IN ('in_transit', 'partial')
              AND tb.status IN ('in_transit', 'partial')
              AND ta.from_branch_id = tb.to_branch_id
              AND ta.to_branch_id   = tb.from_branch_id
              AND ta.from_branch_id <> ta.to_branch_id
              AND COALESCE(tb.created_at, '') > COALESCE(ta.created_at, '')
              AND COALESCE(ta.created_at, '') >= ?
        """), (cutoff,))
        pairs = [dict(r) for r in cur.fetchall()]
        seen = set()
        for p in pairs:
            key = tuple(sorted((p["a_item"], p["b_item"])))
            if key in seen:
                continue
            seen.add(key)
            ts = now_iso()
            for item_id, op_id in ((p["a_item"], p["a_op"]), (p["b_item"], p["b_op"])):
                cur.execute(_q("""
                    UPDATE transfer_items
                    SET received = 4, received_at = ?, received_by = ?, received_method = ?
                    WHERE id = ? AND received = 0
                """), (ts, "בומרנג (אוטומטי)", "בומרנג / חזר למקור", item_id))
                cur.execute(_q("""SELECT COUNT(*) AS n FROM transfer_items
                                  WHERE op_id = ? AND received NOT IN (1, 2, 4)"""), (op_id,))
                if cur.fetchone()["n"] == 0:
                    cur.execute(_q("""UPDATE transfers SET status='received',
                        received_at = COALESCE(received_at, ?) WHERE op_id = ?"""), (ts, op_id))
            out.append({"serial": p["serial"], "name": p.get("name") or "",
                        "origin_branch_id": p["x"], "via_branch_id": p["y"],
                        "op_out": p["a_op"], "op_back": p["b_op"]})
    return out


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


def plan_add(lines: list, created_by: str = "") -> list:
    """מוסיף שורות לתוכנית ההעברות. line: {product_id,name,from_branch,to_branch,qty,serial?}.
    created_by — מי יצר (קונסולה/מכשיר סניף). מחזיר את ה-ids של השורות שנוספו."""
    ids = []
    with _conn() as c:
        cur = c.cursor()
        for ln in lines:
            vals = (str(ln.get("product_id") or ""), ln.get("name") or "",
                    int(ln.get("from_branch")), int(ln.get("to_branch")),
                    int(ln.get("qty") or 1), (ln.get("serial") or "").strip(),
                    created_by or "", now_iso())
            if _USE_PG:
                cur.execute(_q("""
                    INSERT INTO transfer_plan (product_id, name, from_branch, to_branch, qty, serial, created_by, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id
                """), vals)
                ids.append(cur.fetchone()["id"])
            else:
                cur.execute(_q("""
                    INSERT INTO transfer_plan (product_id, name, from_branch, to_branch, qty, serial, created_by, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """), vals)
                ids.append(cur.lastrowid)
    return ids


def plan_replace_product(product_id, lines: list, created_by: str = "") -> int:
    """מחליף את כל שורות התוכנית למוצר אחד (מחיקה + הוספה). lines ריק = הסרת הבקשה."""
    pid = str(product_id)
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("DELETE FROM transfer_plan WHERE product_id = ?"), (pid,))
        for ln in lines:
            cur.execute(_q("""
                INSERT INTO transfer_plan (product_id, name, from_branch, to_branch, qty, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """), (pid, ln.get("name") or "", int(ln.get("from_branch")),
                   int(ln.get("to_branch")), int(ln.get("qty") or 1),
                   created_by or "", now_iso()))
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


def plan_set_note(product_id, note, from_branch=None, to_branch=None, name="") -> dict:
    """הערה חופשית לסניף על פריט. אם הפריט כבר בבקשה — מעדכן את ההערה בכל שורותיו;
    אחרת מוסיף שורת בקשה (לפי המלצת עודף→חוסר, כמות 1) עם ההערה. מחזיר {note, added}."""
    pid = str(product_id)
    note = (note or "").strip()
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT COUNT(*) AS n FROM transfer_plan WHERE product_id = ?"), (pid,))
        exists = cur.fetchone()["n"]
        if exists:
            cur.execute(_q("UPDATE transfer_plan SET note = ? WHERE product_id = ?"), (note, pid))
            return {"product_id": pid, "note": note, "added": 0}
        cur.execute(_q("""INSERT INTO transfer_plan
            (product_id, name, from_branch, to_branch, qty, note, created_by, created_at)
            VALUES (?,?,?,?,?,?,?,?)"""),
            (pid, name or "", int(from_branch or 0), int(to_branch or 0), 1, note,
             "קונסולת ניהול", now_iso()))
        return {"product_id": pid, "note": note, "added": 1}


def plan_reroute(line_id, new_from_branch) -> bool:
    """שינוי שידור לסניף אחר: מעדכן את סניף-המקור של שורת הבקשה ומדליק שידור מחדש
    (bcast=1). היעד (to_branch) נשאר. מבטל בפועל את השידור בסניף הקודם (השורה עוברת)."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("UPDATE transfer_plan SET from_branch = ?, bcast = 1 WHERE id = ?"),
                    (int(new_from_branch), int(line_id)))
        return (cur.rowcount or 0) > 0 if hasattr(cur, "rowcount") else True


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


# ── 🔔 התראות משמרות: עובדים רשומים ──
def shift_employee_register(name: str, telegram_id) -> None:
    """רישום/עדכון עובד למערכת ההתראות (לפי telegram_id — upsert)."""
    with _conn() as c:
        c.cursor().execute(_q("""
            INSERT INTO shift_employees (name, telegram_id, registered_at)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET name=excluded.name
        """), ((name or "").strip(), str(telegram_id), now_iso()))


def shift_employees_all() -> list:
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT name, telegram_id, registered_at FROM shift_employees ORDER BY name"))
        return [dict(r) for r in cur.fetchall()]


def shift_telegram_ids_for_names(names: list) -> list:
    """telegram_ids של עובדים לפי שמות (להפניית DM לפי הסידור). התאמה לא-רגישה לרווחים."""
    norm = {(n or "").strip() for n in names if (n or "").strip()}
    if not norm:
        return []
    out = []
    for e in shift_employees_all():
        if (e.get("name") or "").strip() in norm and e.get("telegram_id"):
            out.append(e["telegram_id"])
    return out


def shift_roster_for_week(week_start: str) -> list:
    """שורות הסידור של שבוע נתון — [{branch_id, dow, employee, hours}]."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT branch_id, dow, employee, hours FROM shift_roster "
                       "WHERE week_start=? ORDER BY branch_id, dow, employee"), (str(week_start),))
        return [dict(r) for r in cur.fetchall()]


def shift_roster_replace(week_start: str, rows: list) -> int:
    """החלפה אטומית של סידור שבוע נתון. rows = [{branch_id, dow, employee, hours}]."""
    ts = now_iso()
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("DELETE FROM shift_roster WHERE week_start=?"), (str(week_start),))
        n = 0
        for r in (rows or []):
            emp = (r.get("employee") or "").strip()
            if not emp:
                continue
            cur.execute(_q("INSERT INTO shift_roster (branch_id, dow, employee, hours, week_start, updated_at) "
                           "VALUES (?,?,?,?,?,?)"),
                        (int(r.get("branch_id") or 0), int(r.get("dow") or 0), emp,
                         (r.get("hours") or "").strip(), str(week_start), ts))
            n += 1
        return n


def shift_roster_weeks() -> list:
    """רשימת השבועות שיש להם סידור (week_start), מהחדש לישן."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT week_start, COUNT(*) AS n FROM shift_roster "
                       "WHERE week_start IS NOT NULL AND week_start<>'' "
                       "GROUP BY week_start ORDER BY week_start DESC"))
        return [dict(r) for r in cur.fetchall()]


def shift_roster_range(date_from: str, date_to: str) -> list:
    """כל שורות הסידור בשבועות שתאריך-הראשון שלהם בטווח [date_from, date_to] (לסיכומים)."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT week_start, branch_id, dow, employee, hours FROM shift_roster "
                       "WHERE week_start>=? AND week_start<=? ORDER BY week_start, branch_id, dow"),
                    (str(date_from), str(date_to)))
        return [dict(r) for r in cur.fetchall()]


def shift_employees_on(branch_id: int, dow: int, week_start: str) -> list:
    """שמות העובדים המשובצים לסניף ביום נתון בשבוע נתון — [{employee, hours}]."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT employee, hours FROM shift_roster "
                       "WHERE branch_id=? AND dow=? AND week_start=?"),
                    (int(branch_id), int(dow), str(week_start)))
        return [dict(r) for r in cur.fetchall()]


def shift_alert_enqueue(branch_id: int, text: str) -> None:
    """דחיית התראת משמרת (שודרה מחוץ לשעות) — לשליחה בבוקר הפתיחה."""
    with _conn() as c:
        c.cursor().execute(_q("INSERT INTO pending_shift_alerts (branch_id, text, created_at) "
                              "VALUES (?,?,?)"), (int(branch_id), text, now_iso()))


def shift_alerts_pending() -> list:
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT id, branch_id, text FROM pending_shift_alerts ORDER BY id"))
        return [dict(r) for r in cur.fetchall()]


def shift_alerts_clear(ids: list) -> None:
    if not ids:
        return
    with _conn() as c:
        cur = c.cursor()
        for i in ids:
            cur.execute(_q("DELETE FROM pending_shift_alerts WHERE id=?"), (int(i),))


def pbx_call_log(phone, direction, uid, name, orders, last_status,
                 route="", order_number="", items="") -> int:
    """תיעוד שיחת טלפון ממרכזיית 1com. מחזיר את ה-id."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q(
            "INSERT INTO pbx_calls (phone, direction, uid, matched_name, orders, last_status, "
            "route, order_number, items, ts) VALUES (?,?,?,?,?,?,?,?,?,?)"),
            (str(phone or ""), str(direction or ""), str(uid or ""), str(name or ""),
             int(orders or 0), str(last_status or ""), str(route or ""),
             str(order_number or ""), str(items or ""), now_iso()))
        try:
            return int(cur.lastrowid)
        except Exception:  # noqa: BLE001
            return 0


def pbx_calls_recent(limit: int = 100) -> list:
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT id, phone, direction, uid, matched_name, orders, last_status, "
                       "route, order_number, items, ts FROM pbx_calls ORDER BY id DESC LIMIT ?"),
                    (int(limit),))
        return [dict(r) for r in cur.fetchall()]


def pbx_calls_since(after_id: int = 0) -> list:
    """שיחות שנכנסו אחרי id נתון (לפולינג הפופאפ ב-frontend). רק שיחות נכנסות אמיתיות."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT id, phone, direction, matched_name, orders, last_status, "
                       "route, order_number, items, ts FROM pbx_calls "
                       "WHERE id>? AND direction='in' ORDER BY id DESC LIMIT 20"), (int(after_id),))
        return [dict(r) for r in cur.fetchall()]


def pbx_call_upsert(phone, direction, uid, name, orders, last_status,
                    route="", order_number="", items="", window_sec: int = 120,
                    force_new: bool = False) -> tuple:
    """שיחה נכנסת מ-1com: אם קיימת שורה טרייה (תוך window_sec שניות) לאותו מספר+כיוון —
    *מעדכן* אותה (ממזג את ה-route שנבחר ב-IVR + הזיהוי) במקום ליצור שורה חדשה. כך שיחה אחת
    = שורה אחת = פופאפ אחד שמתעדכן כשהמתקשר בוחר שלוחה. מחזיר (id, is_new).
    force_new=True (curl הכניסה, `&new=1`) → תמיד שורה חדשה, כדי שניתוק+חיוג-חוזר מאותו
    מספר ייספר כשיחה נפרדת (ולא יתמזג לשיחה הקודמת שעדיין בחלון)."""
    direction = direction or "in"
    phone = str(phone or "")
    with _conn() as c:
        cur = c.cursor()
        row = None
        if not force_new:
            cur.execute(_q("SELECT id, route, matched_name, orders, last_status, order_number, items, ts "
                           "FROM pbx_calls WHERE phone=? AND direction=? ORDER BY id DESC LIMIT 1"),
                        (phone, direction))
            row = cur.fetchone()
        recent = False
        r = dict(row) if row else {}
        if row:
            try:
                ts = datetime.fromisoformat(str(r.get("ts")))
                recent = (datetime.now(timezone.utc).astimezone() - ts).total_seconds() <= window_sec
            except Exception:  # noqa: BLE001
                recent = False
        if row and recent:
            rid = int(r["id"])
            # route מצטבר היררכי: כל רמת הקשה ב-IVR מוסיפה מקטע לנתיב ("סיטי › מעבדה").
            # כניסה גנרית (route ריק) לא משנה; מקטע זהה לאחרון לא מוכפל.
            existing_route = str(r.get("route") or "")
            seg = str(route or "").strip()
            if seg:
                parts = [p for p in existing_route.split(" › ") if p]
                if not parts or parts[-1] != seg:
                    parts.append(seg)
                new_route = " › ".join(parts)
            else:
                new_route = existing_route
            # מיזוג שאר השדות: ערך חדש לא-ריק גובר, אחרת שומרים את הקיים (לא מוחקים זיהוי)
            cur.execute(_q("UPDATE pbx_calls SET route=?, matched_name=?, orders=?, last_status=?, "
                           "order_number=?, items=?, ts=? WHERE id=?"),
                        (new_route,
                         str(name or "") or str(r.get("matched_name") or ""),
                         int(orders or 0) or int(r.get("orders") or 0),
                         str(last_status or "") or str(r.get("last_status") or ""),
                         str(order_number or "") or str(r.get("order_number") or ""),
                         str(items or "") or str(r.get("items") or ""),
                         now_iso(), rid))
            return (rid, False)
        cur.execute(_q(
            "INSERT INTO pbx_calls (phone, direction, uid, matched_name, orders, last_status, "
            "route, order_number, items, ts) VALUES (?,?,?,?,?,?,?,?,?,?)"),
            (phone, direction, str(uid or ""), str(name or ""), int(orders or 0),
             str(last_status or ""), str(route or ""), str(order_number or ""),
             str(items or ""), now_iso()))
        try:
            return (int(cur.lastrowid), True)
        except Exception:  # noqa: BLE001
            return (0, True)


def pbx_calls_active(window_sec: int = 150) -> list:
    """שיחות נכנסות *פעילות* (ts בחלון האחרון) לפולינג הפופאפ — כולל ה-route העדכני.
    מאפשר ל-frontend לזהות גם שיחה חדשה וגם שינוי שלוחה (route) על אותה שיחה."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT id, phone, direction, matched_name, orders, last_status, "
                       "route, order_number, items, ts FROM pbx_calls "
                       "WHERE direction='in' ORDER BY id DESC LIMIT 30"))
        rows = [dict(r) for r in cur.fetchall()]
    nowt = datetime.now(timezone.utc).astimezone()
    out = []
    for r in rows:
        try:
            if (nowt - datetime.fromisoformat(str(r.get("ts")))).total_seconds() <= window_sec:
                out.append(r)
        except Exception:  # noqa: BLE001
            out.append(r)
    return out


def pbx_calls_by_phone(phone, limit: int = 50) -> list:
    """כל השיחות של מספר נתון (לכרטיס לקוח 360). אינדקס על phone → זול."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT id, phone, direction, matched_name, orders, last_status, "
                       "route, order_number, items, ts FROM pbx_calls "
                       "WHERE phone=? ORDER BY id DESC LIMIT ?"), (str(phone or ""), int(limit)))
        return [dict(r) for r in cur.fetchall()]


def pbx_stats(days: int = 30, route: str = "", date_from: str = "", date_to: str = "") -> dict:
    """אנליטיקת שיחות נכנסות לסעיף ה-CRM. מחושב מ-5000 השיחות האחרונות.
    חלון הזמן: date_from/date_to (YYYY-MM-DD) אם ניתנו, אחרת days אחרונים.
    `route` מסנן את המדדים/הסדרה/השעות לסניף/מחלקה. תמיד מוחזר פילוח מלא
    (branches/paths) למסננים. הסדרה היומית נפרשת על כל החלון (שבועי אם >92 ימים)."""
    route = (route or "").strip()
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT phone, matched_name, route, ts FROM pbx_calls "
                       "WHERE direction='in' ORDER BY id DESC LIMIT 5000"))
        rows = [dict(r) for r in cur.fetchall()]
    now = datetime.now(timezone.utc).astimezone()
    today = now.date()
    start = end = None
    if date_from and date_to:
        try:
            start = datetime.strptime(date_from, "%Y-%m-%d").date()
            end = datetime.strptime(date_to, "%Y-%m-%d").date()
        except Exception:  # noqa: BLE001
            start = end = None
    if not (start and end):
        end = today
        start = today - timedelta(days=max(0, int(days) - 1))
    if start > end:
        start, end = end, start
    span = (end - start).days + 1
    per_day = {}
    per_branch, per_path = {}, {}     # פילוח מלא (לא מסונן) — למסננים ולתצוגת נפח
    per_hour = {h: 0 for h in range(24)}
    seen = {}
    total = ident = today_n = week_n = 0
    for r in rows:
        try:
            ts = datetime.fromisoformat(str(r.get("ts")))
        except Exception:  # noqa: BLE001
            continue
        d = ts.date()
        if d < start or d > end:
            continue
        full = (str(r.get("route") or "")).strip()
        branch = (full.split(" › ")[0]).strip() or "—"
        per_branch[branch] = per_branch.get(branch, 0) + 1
        if full:
            per_path[full] = per_path.get(full, 0) + 1
        if route and not (branch == route or full == route or full.startswith(route + " › ")):
            continue
        total += 1
        seen[str(r.get("phone") or "")] = seen.get(str(r.get("phone") or ""), 0) + 1
        if str(r.get("matched_name") or "").strip():
            ident += 1
        if d == today:
            today_n += 1
        if (today - d).days < 7:
            week_n += 1
        per_day[d.isoformat()] = per_day.get(d.isoformat(), 0) + 1
        per_hour[ts.hour] = per_hour.get(ts.hour, 0) + 1
    # סדרה: יומית עד 92 ימים, אחרת מקבצים שבועיים
    series = []
    if span <= 92:
        for i in range(span):
            dd = (start + timedelta(days=i)).isoformat()
            series.append({"date": dd, "count": per_day.get(dd, 0)})
    else:
        cur_d = start
        while cur_d <= end:
            wk_end = min(cur_d + timedelta(days=6), end)
            cnt = sum(per_day.get((cur_d + timedelta(days=j)).isoformat(), 0)
                      for j in range((wk_end - cur_d).days + 1))
            series.append({"date": cur_d.isoformat(), "count": cnt})
            cur_d = wk_end + timedelta(days=1)
    branches = sorted([{"branch": k, "count": v} for k, v in per_branch.items()],
                      key=lambda x: -x["count"])
    paths = sorted([{"path": k, "count": v} for k, v in per_path.items()],
                   key=lambda x: -x["count"])
    hours = [{"hour": h, "count": per_hour.get(h, 0)} for h in range(24)]
    return {
        "total": total, "today": today_n, "week": week_n,
        "identified": ident, "identified_pct": round(100 * ident / total) if total else 0,
        "unique_callers": len(seen),
        "repeat_callers": sum(1 for n in seen.values() if n > 1),
        "series": series, "branches": branches, "paths": paths,
        "hours": hours, "days": days, "route": route,
        "from": start.isoformat(), "to": end.isoformat(), "span": span,
    }


# ── 📞 נתיב שיחה חי (נלכד מ-CHANNELS ע"י worker רקע) ──
def pbx_route_upsert(uid: str, phone: str, route: str, branch: str,
                     answered: bool, ts: str):
    """שומר/מעדכן נתיב שיחה. שומר את הנתיב הספציפי ביותר (הארוך), answered דביק."""
    if not uid:
        return
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT route, answered FROM pbx_route WHERE uid=?"), (uid,))
        row = cur.fetchone()
        if row:
            old_route = row["route"] or ""
            keep = route if len(route or "") >= len(old_route) else old_route
            new_ans = 1 if (answered or (row["answered"] or 0)) else 0
            cur.execute(_q("UPDATE pbx_route SET route=?, branch=?, answered=?, "
                           "last_ts=?, phone=? WHERE uid=?"),
                        (keep, branch or "", new_ans, ts, phone, uid))
        else:
            cur.execute(_q("INSERT INTO pbx_route(uid, phone, route, branch, answered, "
                           "first_ts, last_ts) VALUES(?,?,?,?,?,?,?)"),
                        (uid, phone, route or "", branch or "",
                         1 if answered else 0, ts, ts))


def pbx_routes_by_uids(uids: list) -> dict:
    """מיפוי uid→{route,branch,answered,handled_at} לצירוף עם CDR בהיסטוריה."""
    uids = [u for u in (uids or []) if u]
    if not uids:
        return {}
    out = {}
    with _conn() as c:
        cur = c.cursor()
        for i in range(0, len(uids), 400):
            chunk = uids[i:i + 400]
            ph = ",".join(["?"] * len(chunk))
            cur.execute(_q(f"SELECT uid, route, branch, answered, handled_at, note "
                           f"FROM pbx_route WHERE uid IN ({ph})"), tuple(chunk))
            for r in cur.fetchall():
                out[r["uid"]] = dict(r)
    return out


def pbx_routes_recent(limit: int = 20) -> list:
    """השורות האחרונות ב-pbx_route — לאבחון לכידת ה-worker/live."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT uid, phone, route, branch, answered, last_ts, handled_at "
                       "FROM pbx_route ORDER BY last_ts DESC LIMIT ?"), (int(limit),))
        return [dict(r) for r in cur.fetchall()]


def pbx_note_set(uid: str, note: str):
    """הערה פנימית על שיחה (תיעוד/איכות). upsert — עובד גם לשיחה שלא נלכדה ע"י ה-worker."""
    if not uid:
        return
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("UPDATE pbx_route SET note=? WHERE uid=?"), (note, uid))
        if not cur.rowcount:
            try:
                cur.execute(_q("INSERT INTO pbx_route(uid, note) VALUES(?,?)"), (uid, note))
            except Exception:  # noqa: BLE001
                pass


def pbx_route_mark_handled(uid: str, when: str):
    if not uid:
        return
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("UPDATE pbx_route SET handled_at=? WHERE uid=?"), (when, uid))
        # שיחה שלא נלכדה ע"י ה-worker (אין שורה) — יוצרים שורה רק לסימון הטיפול
        if not cur.rowcount:
            try:
                cur.execute(_q("INSERT INTO pbx_route(uid, handled_at) VALUES(?,?)"), (uid, when))
            except Exception:  # noqa: BLE001
                pass


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


def wa_canned_list() -> list:
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT id, title, text FROM wa_canned ORDER BY title"))
        return [dict(r) for r in cur.fetchall()]


def pay_link_add(pru, descr, amount, name, phone):
    with _conn() as c:
        c.cursor().execute(_q("""
            INSERT INTO pay_links (pru, descr, amount, name, phone, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
        """), (pru or "", descr or "", float(amount or 0), name or "", phone or "", now_iso()))


def pay_link_mark_paid(pru, tx="", approval="", four_digits="", brand=""):
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("""
            UPDATE pay_links SET status='paid', tx=?, approval=?, four_digits=?, brand=?, paid_at=?
            WHERE pru = ? AND status != 'paid'
        """), (tx or "", approval or "", four_digits or "", brand or "", now_iso(), pru or ""))
        return bool(cur.rowcount)


def pay_links_list(q="", limit=60) -> list:
    with _conn() as c:
        cur = c.cursor()
        if (q or "").strip():
            like = f"%{q.strip()}%"
            cur.execute(_q("""SELECT * FROM pay_links
                WHERE descr LIKE ? OR name LIKE ? OR phone LIKE ? OR approval LIKE ? OR four_digits LIKE ?
                ORDER BY id DESC LIMIT ?"""), (like, like, like, like, like, int(limit)))
        else:
            cur.execute(_q("SELECT * FROM pay_links ORDER BY id DESC LIMIT ?"), (int(limit),))
        return [dict(r) for r in cur.fetchall()]


def wa_shadow_add(phone: str, wamid: str, text: str, reply_to: str = "",
                  reply_preview: str = "", ts: int = 0):
    with _conn() as c:
        c.cursor().execute(_q("""
            INSERT INTO wa_shadow (phone, wamid, text, reply_to, reply_preview, ts, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """), (str(phone), wamid or "", text or "", reply_to or "",
               (reply_preview or "")[:120], int(ts or 0), now_iso()))


def wa_shadow_list(phone: str, limit: int = 80) -> list:
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("""SELECT wamid, text, reply_to, reply_preview, ts FROM wa_shadow
                          WHERE phone = ? ORDER BY ts DESC LIMIT ?"""), (str(phone), int(limit)))
        return [dict(r) for r in cur.fetchall()]


# ── WhatsApp עצמאי: חנות ההודעות (מ-webhook ישיר של מטא) ──
def wa_msg_upsert(wamid, phone, direction, mtype, text="", media_id="", media_mime="",
                  media_name="", media_url="", reply_to="", ts=0, status="", raw="") -> bool:
    """מוסיף הודעה (אידמפוטנטי לפי wamid — Meta עלול לשלוח שוב). מחזיר True אם חדשה."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("""
            INSERT INTO wa_msg (wamid, phone, direction, type, text, media_id, media_mime,
                                media_name, media_url, reply_to, ts, status, raw, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(wamid) DO NOTHING
        """), (wamid or "", str(phone), direction, mtype, text or "", media_id or "",
               media_mime or "", media_name or "", media_url or "", reply_to or "",
               int(ts or 0), status or "", (raw or "")[:8000], now_iso()))
        return bool(getattr(cur, "rowcount", 0))


def wa_msg_set_status(wamid: str, status: str, err: str = ""):
    if not wamid:
        return
    with _conn() as c:
        if err:
            c.cursor().execute(_q("UPDATE wa_msg SET status = ?, err = ? WHERE wamid = ?"),
                               (status, err[:300], wamid))
        else:
            c.cursor().execute(_q("UPDATE wa_msg SET status = ? WHERE wamid = ?"), (status, wamid))


def wa_failed_sends(days: int = 7, limit: int = 60) -> list:
    """הודעות יוצאות שנכשלו במסירה (status='failed') + סיבת מטא (err) — לניטור כשלים."""
    import time as _t
    cutoff = int(_t.time()) - int(days) * 86400
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("""SELECT phone, text, type, err, ts FROM wa_msg
                          WHERE direction = 'out' AND status = 'failed' AND ts >= ?
                          ORDER BY ts DESC LIMIT ?"""), (cutoff, int(limit)))
        return [dict(r) for r in cur.fetchall()]


def wa_msg_set_media_url(wamid: str, url: str):
    if not wamid:
        return
    with _conn() as c:
        c.cursor().execute(_q("UPDATE wa_msg SET media_url = ? WHERE wamid = ?"), (url, wamid))


def wa_media_blob_set(wamid: str, mime: str, data: bytes):
    """שומר גיבוי מדיה (base64) — אידמפוטנטי לפי wamid."""
    if not wamid or not data:
        return
    import base64
    from datetime import datetime, timezone
    b64 = base64.b64encode(data).decode("ascii")
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        cur = c.cursor()
        if _USE_PG:
            cur.execute(_q("""INSERT INTO wa_media_blob (wamid, mime, b64, created_at)
                              VALUES (?, ?, ?, ?)
                              ON CONFLICT (wamid) DO UPDATE SET mime = EXCLUDED.mime,
                                  b64 = EXCLUDED.b64"""), (wamid, mime or "", b64, now))
        else:
            cur.execute(_q("""INSERT OR REPLACE INTO wa_media_blob (wamid, mime, b64, created_at)
                              VALUES (?, ?, ?, ?)"""), (wamid, mime or "", b64, now))


def wa_media_blob_get(wamid: str):
    """מחזיר (mime, bytes) מהגיבוי, או None."""
    if not wamid:
        return None
    import base64
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT mime, b64 FROM wa_media_blob WHERE wamid = ?"), (wamid,))
        r = cur.fetchone()
        if not r or not r["b64"]:
            return None
        try:
            return r["mime"], base64.b64decode(r["b64"])
        except Exception:  # noqa: BLE001
            return None


def wa_media_blob_del(wamid: str):
    """מחיקת blob (למשל תמונת פרופיל שהועלתה ידנית)."""
    if not wamid:
        return
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("DELETE FROM wa_media_blob WHERE wamid = ?"), (wamid,))


def wa_contact_pic_phones() -> set:
    """קבוצת הטלפונים שיש להם תמונת פרופיל שהועלתה ידנית (מפתח 'cpic:<phone>')."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT wamid FROM wa_media_blob WHERE wamid LIKE 'cpic:%'"))
        return {str(r["wamid"])[5:] for r in cur.fetchall()}


def wa_media_pending(limit: int = 50):
    """הודעות מדיה נכנסות שיש להן media_id אך עדיין לא גובו (אין blob) — לגיבוי יזום."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("""SELECT m.wamid, m.media_id FROM wa_msg m
                          LEFT JOIN wa_media_blob b ON b.wamid = m.wamid
                          WHERE m.direction = 'in' AND m.media_id IS NOT NULL
                            AND m.media_id <> '' AND b.wamid IS NULL
                          ORDER BY m.ts DESC LIMIT ?"""), (limit,))
        return [{"wamid": r["wamid"], "media_id": r["media_id"]} for r in cur.fetchall()]


def wa_contact_upsert(phone, name=None, wa_id=None, in_ts: int = 0, out_ts: int = 0):
    """מעדכן/יוצר איש קשר + חותמות זמן (last_in_ts לחלון 24ש, last_msg_ts לכל הודעה)."""
    msg_ts = int(in_ts or out_ts or 0)
    with _conn() as c:
        c.cursor().execute(_q("""
            INSERT INTO wa_contact (phone, name, wa_id, last_in_ts, last_msg_ts, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(phone) DO UPDATE SET
                name        = COALESCE(excluded.name, wa_contact.name),
                wa_id       = COALESCE(excluded.wa_id, wa_contact.wa_id),
                last_in_ts  = CASE WHEN excluded.last_in_ts  > COALESCE(wa_contact.last_in_ts, 0)
                                   THEN excluded.last_in_ts  ELSE wa_contact.last_in_ts END,
                last_msg_ts = CASE WHEN excluded.last_msg_ts > COALESCE(wa_contact.last_msg_ts, 0)
                                   THEN excluded.last_msg_ts ELSE wa_contact.last_msg_ts END,
                -- הודעה נכנסת חדשה מבטלת ארכוב (השיחה חוזרת ל-inbox)
                archived    = CASE WHEN excluded.last_in_ts > 0 THEN 0 ELSE wa_contact.archived END,
                updated_at  = excluded.updated_at
        """), (str(phone), name, wa_id, int(in_ts or 0), msg_ts, now_iso()))


def bot_session_get(phone: str) -> dict:
    """מצב שיחת הבוט ללקוח. חוזר {state, data} (data כ-dict)."""
    import json as _j
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT state, data FROM wa_bot_session WHERE phone = ?"), (str(phone),))
        r = cur.fetchone()
        if not r:
            return {"state": None, "data": {}}
        try:
            data = _j.loads(r["data"]) if r["data"] else {}
        except Exception:  # noqa: BLE001
            data = {}
        return {"state": r["state"], "data": data}


def bot_session_set(phone: str, state: str, data: dict = None):
    import json as _j
    d = _j.dumps(data or {}, ensure_ascii=False)
    with _conn() as c:
        cur = c.cursor()
        if _USE_PG:
            cur.execute(_q("""INSERT INTO wa_bot_session (phone, state, data, updated_at)
                              VALUES (?,?,?,?)
                              ON CONFLICT(phone) DO UPDATE SET state=excluded.state,
                              data=excluded.data, updated_at=excluded.updated_at"""),
                        (str(phone), state, d, now_iso()))
        else:
            cur.execute(_q("INSERT OR REPLACE INTO wa_bot_session (phone, state, data, updated_at) VALUES (?,?,?,?)"),
                        (str(phone), state, d, now_iso()))


def bot_session_clear(phone: str):
    with _conn() as c:
        c.cursor().execute(_q("DELETE FROM wa_bot_session WHERE phone = ?"), (str(phone),))


def bot_handoff_phones(hours: int = 12) -> set:
    """phones שכרגע ב-handoff פעיל לנציג (state='agent', חותמת צעירה מ-hours).
    משמש לחיווי האייקון 'מצב אנושי' ברשימת השיחות."""
    import json as _j
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    out = set()
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT phone, data FROM wa_bot_session WHERE state = 'agent'"))
        for r in cur.fetchall():
            try:
                d = _j.loads(r["data"]) if r["data"] else {}
            except Exception:  # noqa: BLE001
                d = {}
            ts = d.get("ts")
            if not ts:
                out.add(r["phone"]); continue
            try:
                t = datetime.fromisoformat(str(ts)).astimezone(timezone.utc)
                if (now - t).total_seconds() < hours * 3600:
                    out.add(r["phone"])
            except Exception:  # noqa: BLE001
                out.add(r["phone"])
    return out


def bot_awaiting_agent_phones(hours: int = 12) -> set:
    """phones שביקשו נציג אנושי ו**טרם נענו ע"י אדם** (state='agent' בלי data.human,
    חותמת צעירה מ-hours). זה ה'ממתין-לנציג' האמיתי — בלי קשר אם הבוט שלח את הודעת
    ההעברה אחרונה (שמאפסת unread). משמש לחלון הצף + לבאדג' '💬'. ברגע שאדם עונה
    (`_bot_handoff_on` מסמן human=True) הלקוח נושר מכאן אוטומטית."""
    import json as _j
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    out = set()
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT phone, data FROM wa_bot_session WHERE state = 'agent'"))
        for r in cur.fetchall():
            try:
                d = _j.loads(r["data"]) if r["data"] else {}
            except Exception:  # noqa: BLE001
                d = {}
            if d.get("human"):                      # אדם כבר ענה → לא 'ממתין'
                continue
            ts = d.get("ts")
            if not ts:
                out.add(r["phone"]); continue
            try:
                t = datetime.fromisoformat(str(ts)).astimezone(timezone.utc)
                if (now - t).total_seconds() < hours * 3600:
                    out.add(r["phone"])
            except Exception:  # noqa: BLE001
                out.add(r["phone"])
    return out


def wa_set_archived(phone, archived: bool):
    """ארכוב/שחזור שיחה בחנות שלנו (מה שה-inbox ה-native קורא)."""
    with _conn() as c:
        c.cursor().execute(_q("UPDATE wa_contact SET archived = ? WHERE phone = ?"),
                           (1 if archived else 0, str(phone)))


def wa_flow_stats(top: int = 50) -> dict:
    """ניתוח כמותי של כל ההיסטוריה לבניית הבוט ה-native: צמתי בוט (פקודות יוצאות
    נפוצות), קלטי לקוח נפוצים, שיעור מעבר-לנציג, אורך שיחות, ואותות תסכול."""
    out = {}
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT COUNT(*) AS m, COUNT(DISTINCT phone) AS p FROM wa_msg")
        r = cur.fetchone(); out["messages"] = r["m"]; out["conversations"] = r["p"]
        # צמתי בוט — הודעות יוצאות נפוצות (התפריטים/פקודות). מנקים [interactive].
        cur.execute(_q("""
            SELECT REPLACE(text, '[interactive]', '') AS t, COUNT(*) AS n
            FROM wa_msg WHERE direction='out' AND text IS NOT NULL AND text <> ''
            GROUP BY REPLACE(text, '[interactive]', '') ORDER BY n DESC LIMIT ?"""), (top,))
        out["bot_prompts"] = [{"text": (r["t"] or "").strip()[:160], "n": r["n"]} for r in cur.fetchall()]
        # קלטי לקוח נפוצים (בחירות תפריט / תשובות)
        cur.execute(_q("""
            SELECT text AS t, COUNT(*) AS n FROM wa_msg
            WHERE direction='in' AND text IS NOT NULL AND text <> ''
            GROUP BY text ORDER BY n DESC LIMIT ?"""), (top,))
        out["customer_inputs"] = [{"text": (r["t"] or "").strip()[:120], "n": r["n"]} for r in cur.fetchall()]
        # מעבר לנציג — כמה שיחות הגיעו לזה
        cur.execute(_q("""SELECT COUNT(DISTINCT phone) AS n FROM wa_msg
                          WHERE direction='in' AND text LIKE '%נציג%'"""))
        out["handoff_convs"] = cur.fetchone()["n"]
        # אותות תסכול בקלט לקוח
        frus = {}
        for kw in ["לא הבנתי", "לא עובד", "נמאס", "אותו דבר", "כבר אמרתי", "מה זה",
                   "לא רוצה", "תפסיק", "אנושי", "בנאדם", "מישהו"]:
            cur.execute(_q("SELECT COUNT(*) AS n FROM wa_msg WHERE direction='in' AND text LIKE ?"),
                        (f"%{kw}%",))
            frus[kw] = cur.fetchone()["n"]
        out["frustration"] = frus
        # התפלגות אורך שיחות (כמה הודעות לכל שיחה)
        cur.execute(_q("""
            SELECT CASE WHEN n<=2 THEN '1-2' WHEN n<=5 THEN '3-5' WHEN n<=10 THEN '6-10'
                        WHEN n<=20 THEN '11-20' ELSE '20+' END AS bucket, COUNT(*) AS c
            FROM (SELECT phone, COUNT(*) AS n FROM wa_msg GROUP BY phone) q
            GROUP BY 1 ORDER BY MIN(n)"""))
        out["length_buckets"] = [{"bucket": r["bucket"], "convs": r["c"]} for r in cur.fetchall()]
    return out


def wa_sample_threads(limit: int = 30, min_msgs: int = 4, max_msgs: int = 30) -> list:
    """דגימת שיחות מלאות (קומפקטי: כיוון+טקסט) לקריאת זרימה איכותנית."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("""
            SELECT phone FROM (
                SELECT phone, COUNT(*) AS n FROM wa_msg GROUP BY phone
            ) q WHERE n >= ? AND n <= ? ORDER BY phone DESC LIMIT ?"""),
            (min_msgs, max_msgs, limit))
        phones = [r["phone"] for r in cur.fetchall()]
        threads = []
        for ph in phones:
            cur.execute(_q("""SELECT direction, text FROM wa_msg WHERE phone=?
                              ORDER BY ts ASC LIMIT 40"""), (ph,))
            msgs = [{"d": r["direction"], "t": (r["text"] or "").replace("[interactive]", "").strip()[:140]}
                    for r in cur.fetchall()]
            threads.append({"phone": ph, "msgs": msgs})
        return threads


def wa_archive_all_except(keep_phones: list) -> dict:
    """מעביר את כל השיחות לארכיון חוץ מ-keep_phones (שמשוחזרות). 2 שאילתות."""
    keep = [str(p) for p in keep_phones if p]
    with _conn() as c:
        cur = c.cursor()
        cur.execute("UPDATE wa_contact SET archived = 1")
        if keep:
            ph = ",".join(["?"] * len(keep))
            cur.execute(_q(f"UPDATE wa_contact SET archived = 0 WHERE phone IN ({ph})"), tuple(keep))
        cur.execute("SELECT COUNT(*) AS n FROM wa_contact WHERE archived = 1")
        arch = cur.fetchone()["n"]
    return {"archived": arch, "kept": len(keep)}


def wa_msg_thread(phone: str, limit: int = 80) -> list:
    """הודעות שיחה (ישן→חדש) מהחנות שלנו — לקריאת inbox עצמאית (שלב 3)."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("""SELECT wamid, phone, direction, type, text, media_id, media_mime,
                                 media_name, media_url, reply_to, ts, status, err
                          FROM wa_msg WHERE phone = ? ORDER BY ts DESC LIMIT ?"""),
                    (str(phone), int(limit)))
        rows = [dict(r) for r in cur.fetchall()]
        rows.reverse()
        return rows


def wa_msg_count() -> int:
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM wa_msg")
        return cur.fetchone()["n"]


def wa_msg_get(wamid: str):
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT * FROM wa_msg WHERE wamid = ?"), (str(wamid),))
        r = cur.fetchone()
        return dict(r) if r else None


def wa_contact_get(phone: str):
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT * FROM wa_contact WHERE phone = ?"), (str(phone),))
        r = cur.fetchone()
        return dict(r) if r else None


def wa_conversations(limit: int = 300) -> list:
    """רשימת שיחות מהחנות שלנו (איש קשר + הודעה אחרונה) — לקריאת inbox עצמאית."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("""
            SELECT c.phone, c.name, c.last_in_ts, c.last_msg_ts, c.live_chat, c.archived,
                   (SELECT m.text FROM wa_msg m WHERE m.phone = c.phone
                    ORDER BY m.ts DESC LIMIT 1) AS last_msg
            FROM wa_contact c
            WHERE c.last_msg_ts IS NOT NULL
            ORDER BY c.last_msg_ts DESC LIMIT ?
        """), (int(limit),))
        return [dict(r) for r in cur.fetchall()]


def wa_search(q: str, limit: int = 50) -> list:
    """חיפוש native באנשי הקשר (שם/טלפון) — מחליף את חיפוש ה-dashboard של קונקטופ.
    אותו פורמט כמו wa_conversations."""
    q = (q or "").strip()
    if not q:
        return []
    digits = "".join(ch for ch in q if ch.isdigit())
    like = "%" + q + "%"
    sel = ("SELECT c.phone, c.name, c.last_in_ts, c.last_msg_ts, c.live_chat, c.archived, "
           "(SELECT m.text FROM wa_msg m WHERE m.phone = c.phone ORDER BY m.ts DESC LIMIT 1) AS last_msg "
           "FROM wa_contact c WHERE ")
    with _conn() as c:
        cur = c.cursor()
        if digits and len(digits) >= 4:
            cur.execute(_q(sel + "(c.phone LIKE ? OR c.name LIKE ?) "
                           "ORDER BY c.last_msg_ts DESC LIMIT ?"),
                        ("%" + digits + "%", like, int(limit)))
        else:
            cur.execute(_q(sel + "c.name LIKE ? ORDER BY c.last_msg_ts DESC LIMIT ?"),
                        (like, int(limit)))
        return [dict(r) for r in cur.fetchall()]


# ── שליחה מתוזמנת ──
def wa_sched_add(phone, text, send_at, name="", order_number="", total="", pru="",
                 descr="", created_by="") -> int:
    vals = (str(phone), text or "", name or "", order_number or "", total or "", pru or "",
            descr or "", send_at, created_by or "", now_iso())
    cols = "(phone, text, name, order_number, total, pru, descr, send_at, status, created_by, created_at)"
    with _conn() as c:
        cur = c.cursor()
        if _USE_PG:
            cur.execute(_q(f"INSERT INTO wa_scheduled {cols} VALUES (?,?,?,?,?,?,?,?,'pending',?,?) RETURNING id"), vals)
            return cur.fetchone()["id"]
        cur.execute(_q(f"INSERT INTO wa_scheduled {cols} VALUES (?,?,?,?,?,?,?,?,'pending',?,?)"), vals)
        return cur.lastrowid


def wa_sched_due(now_iso_str: str) -> list:
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("""SELECT * FROM wa_scheduled
                          WHERE status = 'pending' AND send_at <= ? ORDER BY send_at"""),
                    (now_iso_str,))
        return [dict(r) for r in cur.fetchall()]


def wa_sched_pending() -> list:
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT * FROM wa_scheduled WHERE status = 'pending' ORDER BY send_at")
        return [dict(r) for r in cur.fetchall()]


def wa_sched_mark(sid, status, via="", err="", sent_at=""):
    with _conn() as c:
        c.cursor().execute(_q("""UPDATE wa_scheduled SET status=?, via=?, err=?, sent_at=?
                                 WHERE id=?"""), (status, via or "", (err or "")[:300],
                                                  sent_at or now_iso(), int(sid)))


def wa_sched_cancel(sid) -> int:
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("UPDATE wa_scheduled SET status='canceled' WHERE id=? AND status='pending'"),
                    (int(sid),))
        return cur.rowcount if hasattr(cur, "rowcount") else 0


# ── שינוי סטטוס הזמנה מתוזמן ──
def sched_status_add(order_id, status, run_at, order_number="", status_label="",
                     created_by="") -> int:
    vals = (str(order_id), str(order_number or ""), status, status_label or "",
            run_at, created_by or "", now_iso())
    cols = "(order_id, order_number, status, status_label, run_at, state, created_by, created_at)"
    with _conn() as c:
        cur = c.cursor()
        if _USE_PG:
            cur.execute(_q(f"INSERT INTO scheduled_status {cols} VALUES (?,?,?,?,?,'pending',?,?) RETURNING id"), vals)
            return cur.fetchone()["id"]
        cur.execute(_q(f"INSERT INTO scheduled_status {cols} VALUES (?,?,?,?,?,'pending',?,?)"), vals)
        return cur.lastrowid


def sched_status_due(now_iso_str: str) -> list:
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("""SELECT * FROM scheduled_status
                          WHERE state = 'pending' AND run_at <= ? ORDER BY run_at"""),
                    (now_iso_str,))
        return [dict(r) for r in cur.fetchall()]


def sched_status_pending() -> list:
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT * FROM scheduled_status WHERE state = 'pending' ORDER BY run_at")
        return [dict(r) for r in cur.fetchall()]


def sched_status_mark(sid, state, err=""):
    with _conn() as c:
        c.cursor().execute(_q("""UPDATE scheduled_status SET state=?, err=?, done_at=?
                                 WHERE id=?"""), (state, (err or "")[:300], now_iso(), int(sid)))


def sched_status_cancel(sid) -> int:
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("UPDATE scheduled_status SET state='canceled' WHERE id=? AND state='pending'"),
                    (int(sid),))
        return cur.rowcount if hasattr(cur, "rowcount") else 0


# ── חשבוניות לקוח (נקלטו ממייל הקופה) ──
def invoice_exists(email_uid: str) -> bool:
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT 1 FROM customer_invoices WHERE email_uid = ?"), (str(email_uid),))
        return cur.fetchone() is not None


def invoice_doc_exists(doc_number: str) -> bool:
    dn = str(doc_number or "").strip()
    if not dn:
        return False
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT 1 FROM customer_invoices WHERE doc_number = ?"), (dn,))
        return cur.fetchone() is not None


def invoices_reset() -> int:
    """מוחק את כל החשבוניות שנקלטו — לקליטה מחדש אחרי כיוונון פענוח."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute("DELETE FROM customer_invoices")
        return cur.rowcount if hasattr(cur, "rowcount") else 0


def invoice_add(email_uid, pdf_b64, doc_number="", doc_type="", total="",
                issued_date="", customer_name="", customer_phone="",
                order_number="", filename="", subject="") -> int:
    cols = ("(doc_number, doc_type, total, issued_date, customer_name, customer_phone, "
            "order_number, filename, subject, email_uid, pdf_b64, captured_at)")
    vals = (str(doc_number or ""), doc_type or "", str(total or ""), issued_date or "",
            customer_name or "", customer_phone or "", str(order_number or ""),
            filename or "", (subject or "")[:300], str(email_uid), pdf_b64 or "", now_iso())
    with _conn() as c:
        cur = c.cursor()
        if _USE_PG:
            cur.execute(_q(f"INSERT INTO customer_invoices {cols} VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                           "ON CONFLICT (email_uid) DO NOTHING RETURNING id"), vals)
            row = cur.fetchone()
            return row["id"] if row else 0
        try:
            cur.execute(_q(f"INSERT INTO customer_invoices {cols} VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"), vals)
            return cur.lastrowid
        except Exception:  # noqa: BLE001 — sqlite unique violation (dedup)
            return 0


def invoice_search(phone="", q="", order="", limit=40) -> list:
    """חיפוש חשבוניות לפי טלפון לקוח ו/או טקסט (מספר מסמך/שם/סכום). בלי ה-PDF."""
    fields = ("id, doc_number, doc_type, total, issued_date, customer_name, "
              "customer_phone, order_number, filename, subject, captured_at")
    where, args = [], []
    if phone:
        d = "".join(ch for ch in str(phone) if ch.isdigit())[-9:]
        where.append("customer_phone LIKE ?")
        args.append(f"%{d}%")
    if order:
        where.append("order_number = ?")
        args.append(str(order).strip())
    if q:
        where.append("(doc_number LIKE ? OR customer_name LIKE ? OR total LIKE ? "
                     "OR order_number LIKE ? OR subject LIKE ?)")
        args += [f"%{q}%"] * 5
    sql = f"SELECT {fields} FROM customer_invoices"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q(sql), tuple(args))
        return [dict(r) for r in cur.fetchall()]


def invoice_get(iid: int, with_pdf: bool = False):
    cols = "*" if with_pdf else ("id, doc_number, doc_type, total, issued_date, "
                                  "customer_name, customer_phone, filename, subject, captured_at")
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q(f"SELECT {cols} FROM customer_invoices WHERE id = ?"), (int(iid),))
        r = cur.fetchone()
        return dict(r) if r else None


def invoice_count() -> int:
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM customer_invoices")
        return cur.fetchone()["n"]


def wa_bf_done_set(phone: str):
    with _conn() as c:
        c.cursor().execute(_q("""INSERT INTO wa_backfill_done (phone, done_at) VALUES (?, ?)
                                 ON CONFLICT(phone) DO NOTHING"""), (str(phone), now_iso()))


def wa_bf_done_all() -> set:
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT phone FROM wa_backfill_done")
        return {r["phone"] for r in cur.fetchall()}


def wa_bf_done_count() -> int:
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM wa_backfill_done")
        return cur.fetchone()["n"]


def wa_canned_add(title: str, text: str):
    with _conn() as c:
        c.cursor().execute(_q(
            "INSERT INTO wa_canned (title, text, created_at) VALUES (?, ?, ?)"),
            (title, text, now_iso()))


def wa_canned_delete(cid: int) -> bool:
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("DELETE FROM wa_canned WHERE id = ?"), (cid,))
        return cur.rowcount > 0


# ── תור אורי ──
def uri_job_add(phone: str, question: str, source: str = "panel") -> int:
    """source='panel' → טיוטה לאסי בקונסולה. source='bot' → תשובה אוטומטית ללקוח."""
    with _conn() as c:
        cur = c.cursor()
        vals = (phone, question, source, now_iso())
        if _USE_PG:
            cur.execute(_q("""
                INSERT INTO uri_jobs (phone, question, source, created_at)
                VALUES (?, ?, ?, ?) RETURNING id"""), vals)
            return cur.fetchone()["id"]
        cur.execute(_q("""
            INSERT INTO uri_jobs (phone, question, source, created_at) VALUES (?, ?, ?, ?)"""), vals)
        return cur.lastrowid


def uri_job_get(jid: int):
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT * FROM uri_jobs WHERE id = ?"), (jid,))
        r = cur.fetchone()
        return dict(r) if r else None


def uri_history(phone: str, limit: int = 80) -> list:
    """היסטוריית השיחה עם אורי לטלפון נתון (להמשכיות בין מכשירים).
    מחזיר את ה-jobs שכבר נענו, מהישן לחדש; מדלג על warmup ועל ריקים."""
    phone = (phone or "").strip()
    if not phone:
        return []
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("""
            SELECT id, question, status, answer, created_at, answered_at
            FROM uri_jobs
            WHERE phone = ? AND status IN ('done','error')
                  AND question <> '[WARMUP]'
            ORDER BY id DESC LIMIT ?"""), (phone, int(limit)))
        rows = [dict(r) for r in cur.fetchall()]
    rows.reverse()
    return rows


def uri_jobs_pending(mark_running: bool = True) -> list:
    """משימות ממתינות לגשר; מסומנות running כדי שלא יימשכו פעמיים."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("""
            SELECT id, phone, question, source FROM uri_jobs
            WHERE status = 'pending' ORDER BY id LIMIT 5"""))
        jobs = [dict(r) for r in cur.fetchall()]
        if mark_running and jobs:
            ids = tuple(j["id"] for j in jobs)
            cur.execute(_q(
                f"UPDATE uri_jobs SET status='running' WHERE id IN ({','.join('?'*len(ids))})"),
                ids)
    return jobs


def uri_job_answer(jid: int, answer: str, status: str = "done"):
    with _conn() as c:
        c.cursor().execute(_q("""
            UPDATE uri_jobs SET answer = ?, status = ?, answered_at = ? WHERE id = ?"""),
            (answer, status, now_iso(), jid))


def uri_jobs_requeue_stuck(minutes: int = 10):
    """משימות שנתקעו ב-running (הגשר נפל באמצע) — חוזרות לתור."""
    cutoff = (datetime.now(timezone.utc).astimezone()
              - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    with _conn() as c:
        c.cursor().execute(_q("""
            UPDATE uri_jobs SET status='pending'
            WHERE status='running' AND created_at < ?"""), (cutoff,))


def wa_push_sub_add(endpoint: str, sub_json: str, ua: str = ""):
    with _conn() as c:
        c.cursor().execute(_q("""
            INSERT INTO wa_push_subs (endpoint, sub, ua, created_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(endpoint) DO UPDATE SET sub=excluded.sub, ua=excluded.ua
        """), (endpoint, sub_json, ua, now_iso()))


def wa_push_subs() -> list:
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT id, endpoint, sub, ua FROM wa_push_subs"))
        return [dict(r) for r in cur.fetchall()]


def wa_push_sub_delete(endpoint: str):
    with _conn() as c:
        c.cursor().execute(_q("DELETE FROM wa_push_subs WHERE endpoint = ?"), (endpoint,))


def sales_docids_since(since_prefix: str) -> set:
    """doc_ids שכבר נקלטו עם sale_date >= since_prefix (לדילוג באיסוף)."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("SELECT DISTINCT doc_id FROM sales WHERE sale_date >= ?"), (since_prefix,))
        return {str(r["doc_id"]) for r in cur.fetchall()}


def sales_by_serial(serial: str) -> list:
    """כל שורות המכירה לסריאל נתון (אבחון: doc_type/branch/qty/date) — למה ריפוי לא תפס."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("""SELECT doc_id, name, qty, price, serial, branch_id, doc_type, sale_date
                          FROM sales WHERE serial = ? ORDER BY sale_date DESC LIMIT 20"""), (serial,))
        return [dict(r) for r in cur.fetchall()]


def transfer_items_by_serial(serial: str) -> list:
    """פריטי-העברה לסריאל + סטטוס ההעברה והזמנים — לאבחון למה ריפוי-נמכר לא תפס."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("""
            SELECT ti.id AS item_id, ti.op_id, ti.serial, ti.received, ti.name,
                   t.status, t.to_branch_id, t.from_branch_id,
                   t.created_at, t.first_seen
            FROM transfer_items ti JOIN transfers t ON t.op_id = ti.op_id
            WHERE ti.serial = ? ORDER BY t.created_at DESC LIMIT 10"""), (serial,))
        return [dict(r) for r in cur.fetchall()]


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


def _il_today():
    """תאריך 'היום' בשעון ישראל (Asia/Jerusalem) — לא UTC, אחרת drift סביב חצות."""
    from datetime import datetime, timedelta
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Jerusalem"))
    except Exception:  # noqa: BLE001
        from datetime import timezone
        return datetime.now(timezone.utc) + timedelta(hours=3)   # fallback: IL קיץ (UTC+3)


# פריטי משלוח/איסוף אינם 'מכירה' — מסוננים מהמכירות/קטגוריות/אחרונות (אסי, 26/06).
# ⚠️ ה-LIKE דרך **פרמטרים** ('משלוח%'), לא % מילולי ב-SQL — אחרת psycopg2 שובר
# על Postgres (ה-% מתנגש עם placeholder ה-%s ש-_q מייצר). _SALES_SHIP_PARAMS נוסף לכל שאילתה.
_SALES_SHIP_FILTER = " AND {a}name NOT LIKE ? AND {a}name NOT LIKE ? "
_SALES_SHIP_PARAMS = ["משלוח%", "איסוף%"]


def sales_dashboard(branch_id=None, from_date=None, to_date=None, period=None) -> dict:
    """מכירות ללוח הבית (טבלת sales; qty חתום: מכירה +, זיכוי −). ברירת מחדל=היום (שעון IL).
    period='yesterday' → אתמול · from_date/to_date (YYYY-MM-DD) → טווח. by_branch/today/
    categories/recent מעל הטווח; weekly=14 ימים אחרונים (sparkline קבוע). פריטי משלוח/איסוף
    מסוננים (אינם 'מכירה'). recent מקובץ לפי מסמך (עסקה אחת = שורה אחת), לא לפי שורת-פריט."""
    from datetime import timedelta
    now = _il_today()
    today = now.strftime("%Y-%m-%d")
    if period == "yesterday":
        f = t = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    elif from_date:
        f = str(from_date)[:10]
        t = (str(to_date)[:10] if to_date else f)
    else:
        f = t = today
    t_end = t + "T99"   # כולל את כל שעות יום-הסיום (sale_date הוא ISO 'YYYY-MM-DDThh:mm')
    d14 = (now - timedelta(days=13)).strftime("%Y-%m-%d")
    bsel = None
    if branch_id not in (None, "", "all"):
        try:
            bsel = int(branch_id)
        except (TypeError, ValueError):
            bsel = None
    ship = _SALES_SHIP_FILTER.format(a="")        # בלי alias
    ship_s = _SALES_SHIP_FILTER.format(a="s.")    # עם alias s
    out = {"by_branch": {}, "today": {"revenue": 0, "credits": 0, "count": 0},
           "categories": [], "recent": [], "weekly": [], "from": f, "to": t, "date": f}
    with _conn() as c:
        cur = c.cursor()
        # סך לכל סניף בטווח (revenue=מכירה doc_type 0 · credits=זיכוי doc_type 5)
        # revenue = נטו: מכירות (0) + זיכויים/ביטולים (5, qty שלילי) — תואם "מכירות בקופה" של NewOrder.
        # כולל משלוח (NewOrder כולל אותו בסך). credits מוצג בנפרד (אינפורמטיבי).
        cur.execute(_q("""SELECT branch_id,
            SUM(CASE WHEN doc_type IN (0,5) THEN qty*price ELSE 0 END) AS revenue,
            SUM(CASE WHEN doc_type=5 THEN qty*price ELSE 0 END) AS credits,
            COUNT(DISTINCT CASE WHEN doc_type=0 THEN doc_id END) AS cnt
            FROM sales WHERE sale_date >= ? AND sale_date <= ?
            GROUP BY branch_id"""), (f, t_end))
        for r in cur.fetchall():
            b = r["branch_id"]
            if b is None:
                continue
            out["by_branch"][int(b)] = {"revenue": round(float(r["revenue"] or 0)),
                                        "credits": round(abs(float(r["credits"] or 0))),
                                        "count": int(r["cnt"] or 0)}
        if bsel is not None:
            out["today"] = dict(out["by_branch"].get(bsel, {"revenue": 0, "credits": 0, "count": 0}))
        else:
            out["today"] = {"revenue": round(sum(v["revenue"] for v in out["by_branch"].values())),
                            "credits": round(sum(v["credits"] for v in out["by_branch"].values())),
                            "count": sum(v["count"] for v in out["by_branch"].values())}
        bfilt = " AND s.branch_id = ?" if bsel is not None else ""
        bp = [bsel] if bsel is not None else []
        # פילוח קטגוריות (מכירות בלבד, ללא משלוחים)
        cur.execute(_q("""SELECT COALESCE(NULLIF(c.category,''),'אחר') AS cat, SUM(s.qty*s.price) AS rev
            FROM sales s LEFT JOIN catalog c ON s.product_id = c.product_id
            WHERE s.sale_date >= ? AND s.sale_date <= ? AND s.doc_type IN (0,5)""" + ship_s + bfilt + """
            GROUP BY cat ORDER BY rev DESC LIMIT 12"""), tuple([f, t_end] + _SALES_SHIP_PARAMS + bp))
        out["categories"] = [{"category": r["cat"], "revenue": round(float(r["rev"] or 0))}
                             for r in cur.fetchall() if float(r["rev"] or 0) > 0]
        # מכירות אחרונות — לפי מסמך (עסקה), לא לפי שורת-פריט. שם=הפריט היקר בעסקה.
        cur.execute(_q("""SELECT doc_id, branch_id, name, qty, price, sale_date FROM sales s
            WHERE s.sale_date >= ? AND s.sale_date <= ? AND s.doc_type=0""" + ship_s + bfilt + """
            ORDER BY sale_date DESC LIMIT 500"""), tuple([f, t_end] + _SALES_SHIP_PARAMS + bp))
        docs = {}
        for r in cur.fetchall():
            did = r["doc_id"]
            amt = float(r["qty"] or 0) * float(r["price"] or 0)
            d = docs.get(did)
            if d is None:
                d = docs[did] = {"doc_id": did, "branch_id": r["branch_id"],
                                 "ts": r["sale_date"], "amount": 0.0, "name": r["name"] or "",
                                 "_top": -1e18, "items": 0}
            d["amount"] += amt
            d["items"] += 1
            if (r["sale_date"] or "") > (d["ts"] or ""):
                d["ts"] = r["sale_date"]
            if amt > d["_top"]:
                d["_top"] = amt
                d["name"] = r["name"] or d["name"]
        recent = sorted(docs.values(), key=lambda x: x["ts"] or "", reverse=True)[:25]
        out["recent"] = [{"name": d["name"], "amount": round(d["amount"]), "items": d["items"],
                          "branch_id": d["branch_id"], "ts": d["ts"]} for d in recent]
        # הכנסה יומית 14 ימים (sparkline שבוע-מול-שבוע) — קבוע, לא תלוי בטווח הנבחר
        cur.execute(_q("""SELECT substr(sale_date,1,10) AS day, SUM(s.qty*s.price) AS rev FROM sales s
            WHERE s.sale_date >= ? AND s.doc_type IN (0,5)""" + bfilt + """
            GROUP BY day ORDER BY day"""), tuple([d14] + bp))
        daymap = {r["day"]: round(float(r["rev"] or 0)) for r in cur.fetchall()}
        days = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(13, -1, -1)]
        out["weekly"] = [{"day": d, "revenue": daymap.get(d, 0)} for d in days]
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
                INSERT INTO catalog (product_id, name, stock, supplier, category, kind, barcode, active, is_stock, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """), (str(r.get("product_id")), r.get("name") or "", float(r.get("stock") or 0),
                   r.get("supplier") or "", r.get("category") or "", r.get("kind") or "",
                   r.get("barcode") or "", 1 if r.get("active") else 0,
                   1 if r.get("is_stock", True) else 0, ts))


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
                # ברירת מחדל True לרשומות ישנות שעוד לא רועננו עם העמודה
                "is_stock": bool(d["is_stock"]) if d.get("is_stock") is not None else True,
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


def device_set_locked(token: str, branch_id) -> bool:
    """קובע את הסניף הנעול של המכשיר (אימוץ ראשוני או אחרי אישור מנהל)."""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q("UPDATE devices SET branch_locked = ? WHERE token = ?"),
                    (str(branch_id), str(token)))
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
