"""קליטת חשבוניות לקוח ממייל הקופה.

הקופה (NewOrder) מוגדרת לשלוח עותק מקור של כל מסמך שמופק גם לאימייל השולח
(greenmobile.eshop@gmail.com — הגדרת "שלח מסמך מקור גם לאימייל השולח"). המודול
הזה קורא את התיבה ב-IMAP, מושך את ה-PDF, מפענח מזהים (מספר מסמך/סכום/תאריך/לקוח),
ושומר ב-DB — כדי שנשלח את החשבונית חזרה ללקוח בוואטסאפ בלי להיכנס לקופה.

עצמאי לגמרי מרפי/NewOrder API — מסתמך רק על המייל שהקופה כבר שולחת.

הפעלה: דורש INVOICE_IMAP_USER + INVOICE_IMAP_PASS (App Password של Gmail) ב-env.
רץ כ-cron ב-main.py. dedup לפי IMAP UID.
"""
from __future__ import annotations

import base64
import email
import imaplib
import logging
import os
import re
from email.header import decode_header

import db

logger = logging.getLogger("invoice_capture")

IMAP_HOST = os.getenv("INVOICE_IMAP_HOST", "imap.gmail.com")
IMAP_USER = os.getenv("INVOICE_IMAP_USER", "")
IMAP_PASS = os.getenv("INVOICE_IMAP_PASS", "")
# שולח עותקי המסמכים (ברירת מחדל = התיבה עצמה; הקופה שולחת "גם לשולח")
INVOICE_FROM = os.getenv("INVOICE_FROM", "greenmobile.eshop@gmail.com")
SCAN_DAYS = int(os.getenv("INVOICE_SCAN_DAYS", "14"))
# העברת חשבוניות לקוח שנקלטו לתווית ייעודית והוצאה מ-INBOX (ארכוב). מסנן **רק**
# לפי שולח חשבוניות הלקוח (greenmobile.eshop) — לא נוגע בחשבוניות ספק (איציק = hclickapp).
FILE_SENDER = os.getenv("INVOICE_FILE_SENDER", "greenmobile.eshop@gmail.com")
FILE_LABEL = os.getenv("INVOICE_FILE_LABEL", "חשבוניות לקוחות")
FILE_ENABLE = os.getenv("INVOICE_FILE", "1") == "1"
# זיהוי חשבונית לקוח לפי **שם העוסק שלנו בנושא** (שני העוסקים בקופה). מופיע בכל הודעה
# של חשבונית לקוח — self-copy, bounce (mailer-daemon "לא נמסרה"), ותגובת לקוח — אבל
# **לא** בחשבוניות ספקים (LiveDns/סטלר/קונקטופ). כך הלקוחות יוצאים מהנכנסות, הספקים
# נשארים (אסי רוצה לראות אותם כדי להדפיס). ניתן לעדכון דרך env.
FILE_MARKERS = [m.strip() for m in os.getenv(
    "INVOICE_CUSTOMER_MARKERS", "ג.א.א.ל,מובילים בדיגיטל").split(",") if m.strip()]


def configured() -> bool:
    return bool(IMAP_USER and IMAP_PASS)


def _imap_utf7(s: str) -> str:
    """קידוד modified UTF-7 (RFC 3501) לשמות תוויות/תיקיות בעברית ב-IMAP."""
    import base64
    res, buf = [], ""

    def _enc(u):
        return base64.b64encode(u.encode("utf-16-be")).decode("ascii").replace("/", ",").rstrip("=")
    for ch in s:
        if 0x20 <= ord(ch) <= 0x7e:
            if buf:
                res.append("&" + _enc(buf) + "-"); buf = ""
            res.append("&-" if ch == "&" else ch)
        else:
            buf += ch
    if buf:
        res.append("&" + _enc(buf) + "-")
    return "".join(res)


def _inbox_msg_map(M) -> dict:
    """מחזיר {uid(str): (subject, thrid)} לכל הודעות ה-INBOX. שולף **באצוות (chunks)** —
    fetch ענק יחיד החזיר רק חלק מההודעות (זה היה באג: חשבוניות ישנות לא נסרקו). מצרף
    X-GM-THRID כדי לארכב שרשור-שלם (bounce/תגובה באותו thread). כולל גם \\Deleted."""
    uids = []
    seen = set()
    for crit in (("ALL",), ("DELETED",)):
        try:
            typ, d = M.uid("search", None, *crit)
            for x in (d[0].split() if d and d[0] else []):
                if x not in seen:
                    seen.add(x); uids.append(x)
        except Exception:  # noqa: BLE001
            pass
    out = {}
    CHUNK = 100
    for i in range(0, len(uids), CHUNK):
        chunk = uids[i:i + CHUNK]
        try:
            uid_csv = b",".join(chunk).decode()
            typ, md = M.uid("fetch", uid_csv,
                            "(X-GM-THRID BODY.PEEK[HEADER.FIELDS (SUBJECT)])")
            for part in (md or []):
                if isinstance(part, tuple) and part[0]:
                    mu = re.search(rb"UID (\d+)", part[0])
                    if not mu:
                        continue
                    mt = re.search(rb"X-GM-THRID (\d+)", part[0])
                    hdr = email.message_from_bytes(part[1] or b"")
                    out[mu.group(1).decode()] = (_decode(hdr.get("Subject") or ""),
                                                 mt.group(1).decode() if mt else None)
        except Exception as e:  # noqa: BLE001
            logger.warning("inbox msg fetch chunk failed: %s", e)
    return out


def _is_customer_invoice(subject: str) -> bool:
    """חשבונית לקוח = הטקסט מכיל 'חשבונית' **וגם** שם עוסק שלנו (FILE_MARKERS). תופס
    self-copy + bounce + תגובה; **לא** תופס חשבוניות ספקים (שם ספק אחר). פועל גם על
    גוף הודעה (לזיהוי DSN לפי החשבונית המקורית בתוכו)."""
    s = subject or ""
    return ("חשבונית" in s) and any(mk in s for mk in FILE_MARKERS)


def _searchable(raw: bytes) -> str:
    """טקסט בר-חיפוש מהודעה גולמית: כותרות מפוענחות (Subject/From/To) + חלקי טקסט +
    הודעה מצורפת (message/rfc822). נחוץ כדי לזהות בתוך DSN את החשבונית המקורית, שכותרתה
    מקודדת MIME (חיפוש בייטים גולמי על עברית נכשל)."""
    try:
        m = email.message_from_bytes(raw or b"")
    except Exception:  # noqa: BLE001
        return ""
    parts = []

    def _add_headers(msg):
        for h in ("Subject", "From", "To"):
            v = msg.get(h)
            if v:
                parts.append(_decode(v))

    for p in m.walk():
        _add_headers(p)
        ct = (p.get_content_type() or "")
        if "rfc822-headers" in ct:
            # ההודעה המקורית מצורפת ככותרות גולמיות (Subject מקודד MIME) — מפענחים
            try:
                payload = p.get_payload(decode=True) or b""
                _add_headers(email.message_from_bytes(payload))
            except Exception:  # noqa: BLE001
                pass
        elif ct.startswith("text") or "rfc822" in ct:
            try:
                payload = p.get_payload(decode=True)
                if payload:
                    parts.append(payload.decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                pass
    return " ".join(parts)


def file_to_folder(M) -> dict:
    """מארכב את **כל** הודעות חשבונית-הלקוח מה-INBOX (self-copy + bounce + תגובות) לתווית
    'חשבוניות לקוחות' (+X-GM-LABELS), ומסיר מ-INBOX (-X-GM-LABELS \\Inbox — נשאר ב-All
    Mail תחת התווית, בלי סיכון Trash). זיהוי לפי שם העוסק בנושא (ראה _is_customer_invoice)
    כדי לתפוס גם bounces מ-mailer-daemon ותגובות לקוח, ולהשאיר חשבוניות ספקים בנכנסות.
    בטוח לאיציק (from:hclickapp — אין בנושא את שם העוסק שלנו)."""
    res = {"checked": 0, "filed": 0, "remaining": None}
    try:
        M.select("INBOX", readonly=False)
        lbl = '"%s"' % _imap_utf7(FILE_LABEL)
        mm = _inbox_msg_map(M)  # {uid: (subject, thrid)}
        # זיהוי שרשורי חשבונית-לקוח (לפי שם עוסק בנושא של *הודעה כלשהי* בשרשור), ואז
        # ארכוב **כל** הודעות אותם שרשורים — תופס גם את ה-bounce שכותרתו שונה (DSN).
        cust_thrids = {th for (s, th) in mm.values() if th and _is_customer_invoice(s)}
        to_file = [u for u, (s, th) in mm.items()
                   if (th and th in cust_thrids) or _is_customer_invoice(s)]
        # הודעות כשל-מסירה (DSN) של חשבונית לקוח: ה-Subject באנגלית (בלי שם עוסק), אז
        # מזהים לפי נושא DSN ואז מאשרים דרך **הגוף** (מכיל את החשבונית המקורית עם שם העוסק).
        DSN_HINT = ("delivery status notification", "mail delivery", "undeliver",
                    "failure notice", "returned mail", "mail delivery failed")
        in_file = set(to_file)
        dsn_checked = 0
        res["dsn_samples"] = []
        for u, (s, th) in mm.items():
            if u in in_file or not any(h in (s or "").lower() for h in DSN_HINT):
                continue
            dsn_checked += 1
            try:
                typ, md = M.uid("fetch", u, "(BODY.PEEK[])")
                txt = _searchable(md[0][1]) if md and md[0] else ""
                # חשבונית לקוח = שם עוסק+חשבונית, או כשל מסירה של מייל **שאנחנו** שלחנו
                # (greenmobile.eshop) שהוא חשבונית — כי ספקים לא נשלחים מאיתנו.
                match = _is_customer_invoice(txt) or \
                    (INVOICE_FROM in txt and "חשבונית" in txt)
                if len(res["dsn_samples"]) < 8:
                    res["dsn_samples"].append({
                        "match": match, "has_invoice": "חשבונית" in txt,
                        "markers": [mk for mk in FILE_MARKERS if mk in txt],
                        "snip": txt[:120]})
                if match:
                    to_file.append(u); in_file.add(u)
            except Exception as e:  # noqa: BLE001
                res["dsn_samples"].append({"err": str(e)[:80]})
        res["dsn_checked"] = dsn_checked
        res["checked"] = len(to_file)
        res["scanned"] = len(mm)
        # ⚠️ ארכוב ב-Gmail: -X-GM-LABELS (\Inbox) **לא עובד** (Gmail מחזיר OK אך לא מסיר;
        # \Inbox אפילו לא מופיע ב-X-GM-LABELS). הדרך האמינה: +FLAGS \Deleted ואז EXPUNGE.
        # ההודעה גם ב-All Mail → EXPUNGE מ-INBOX רק מוריד את תווית Inbox (ארכוב), לא מוחק.
        for uid in to_file:
            try:
                M.uid("STORE", uid, "+X-GM-LABELS", "(%s)" % lbl)   # תווית ייעודית (findability)
                M.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
                res["filed"] += 1
            except Exception as e:  # noqa: BLE001
                res.setdefault("errs", []).append(str(e)[:120])
        if res["filed"]:
            try:
                M.expunge()
            except Exception as e:  # noqa: BLE001
                res["expunge_err"] = str(e)[:120]
        # בדיקת אמת: כמה מה-to_file עדיין ב-INBOX אחרי הארכוב (0 = הצלחה)
        try:
            typ, d = M.uid("search", None, "ALL")
            still = set(x.decode() for x in (d[0].split() if d and d[0] else []))
            res["still_in_inbox"] = sum(1 for u in to_file if u in still)
        except Exception:  # noqa: BLE001
            pass
        res["remaining"] = res["checked"] - res["filed"]
        if res["filed"]:
            logger.info("filed %d customer-invoice messages to '%s'", res["filed"], FILE_LABEL)
    except Exception as e:  # noqa: BLE001
        logger.warning("file_to_folder failed: %s", e)
        res["error"] = str(e)
    return res


def _all_mail_folder(M) -> str:
    """מאתר את תיקיית "כל המיילים" של Gmail (special-use \\All) — כי מייל שנשלח
    מהחשבון לעצמו מדלג על INBOX ויושב רק שם. עמיד לשפה (לא תלוי בשם המתורגם)."""
    try:
        typ, boxes = M.list()
        for b in (boxes or []):
            line = b.decode() if isinstance(b, bytes) else str(b)
            if "\\All" in line:
                m = re.search(r'"([^"]+)"\s*$', line) or re.search(r'([^"\s]+)\s*$', line)
                if m:
                    return m.group(1)
    except Exception as e:  # noqa: BLE001
        logger.warning("all-mail folder lookup failed: %s", e)
    return "INBOX"


def _decode(s) -> str:
    if not s:
        return ""
    out = []
    for part, enc in decode_header(s):
        if isinstance(part, bytes):
            try:
                out.append(part.decode(enc or "utf-8", "replace"))
            except Exception:  # noqa: BLE001
                out.append(part.decode("utf-8", "replace"))
        else:
            out.append(part)
    return "".join(out)


# ── פענוח PDF (PyMuPDF — אותו דפוס כמו invoice-manager) ──
def _pdf_text(pdf_bytes: bytes) -> str:
    try:
        import fitz  # PyMuPDF
    except Exception:  # noqa: BLE001
        return ""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        return "\n".join(page.get_text() for page in doc)
    except Exception as e:  # noqa: BLE001
        logger.warning("pdf text extract failed: %s", e)
        return ""


def _parse(pdf_bytes: bytes, subject: str = "") -> dict:
    """מחלץ מזהים מהחשבונית. מקורות אמינים: מספר מסמך — מנושא המייל; שם לקוח /
    מספר הזמנת אתר / טלפון — מהבלוק המובנה בתחתית ה-PDF (* שם לקוח: ... / מספר
    הזמנה באתר / טל' לקוח). best-effort עם נפילה רכה."""
    out = {"doc_type": "", "doc_number": "", "total": "", "issued_date": "",
           "customer_name": "", "customer_phone": "", "order_number": ""}
    # מספר מסמך — מנושא המייל ("חשבונית מס/קבלה מספר 1064")
    ms = re.search(r"מספר\s*(\d+)", subject or "")
    if ms:
        out["doc_number"] = ms.group(1)
    t = _pdf_text(pdf_bytes)
    if not t:
        return out
    # סוג מסמך
    if re.search(r"חשבונית\s*(?:מס\s*)?זיכוי", t):
        out["doc_type"] = "חשבונית זיכוי"
    elif re.search(r"חשבונית[\s\\/]*מס", t):
        out["doc_type"] = "חשבונית מס"
    elif "קבלה" in t:
        out["doc_type"] = "קבלה"
    # מספר מסמך — נפילה מה-PDF אם לא היה בנושא ("חשבונית מס\קבלה05-001064")
    if not out["doc_number"]:
        md = re.search(r"חשבונית[\s\\/א-ת]*?(\d{2}-\d{4,}|\d{4,})", t)
        if md:
            num = md.group(1)
            out["doc_number"] = (num.split("-")[-1].lstrip("0") or num) if "-" in num else num
    # בלוק מובנה בתחתית (אמין): מספר הזמנה / שם לקוח / טלפון
    mo = re.search(r"(\d{3,})\s*:\s*\*?\s*מספר\s*הזמנה\s*באתר", t)
    if mo:
        out["order_number"] = mo.group(1)
    mn = re.search(r"שם\s*לקוח\s*:\s*([^\n]+)", t)
    if mn:
        out["customer_name"] = mn.group(1).strip(" *:‏‎")[:80]
    # טלפון — מהבלוק המובנה ("0537174944 :* טל' לקוח") או כללי ראשון במסמך
    mp = re.search(r"(0\d{1,2}[-\s]?\d{6,8})\s*:\s*\*?\s*טל", t) \
        or re.search(r"\b(0(?:5\d|7\d|[2-489])[-\s]?\d{3}[-\s]?\d{4})\b", t)
    if mp:
        out["customer_phone"] = re.sub(r"[-\s]", "", mp.group(1))
    # תאריך — "תאריך חשבונית: 15/06/2026" או הראשון במסמך
    md2 = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", t)
    if md2:
        out["issued_date"] = md2.group(1)
    # סכום כולל מע"מ — ליד התווית, אחרת הסכום הגדול ביותר במסמך
    mt = re.search(r'כולל\s*מע[\"״]מ[^\d₪]{0,25}₪?\s*([\d,]+\.\d{2})', t)
    if mt:
        out["total"] = mt.group(1).replace(",", "")
    else:
        amts = [float(x.replace(",", "")) for x in re.findall(r"([\d,]+\.\d{2})", t)]
        if amts:
            out["total"] = f"{max(amts):.2f}"
    return out


def probe(days: int = 21, max_msgs: int = 40) -> dict:
    """אבחון בלבד (לא שומר): סורק את התיבה ומחזיר אילו מיילים עם PDF יש,
    מאיזה שולח ובאיזה נושא — כדי לכוון את הפילטר/פענוח. לא נוגע בדגלים."""
    if not configured():
        return {"ok": False, "reason": "imap-not-configured"}
    out = {"ok": True, "with_pdf": 0, "items": []}
    try:
        M = imaplib.IMAP4_SSL(IMAP_HOST)
        M.login(IMAP_USER, IMAP_PASS)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"login: {e}"}
    try:
        out["mailbox"] = _all_mail_folder(M)
        M.select(f'"{out["mailbox"]}"', readonly=True)
        import time as _t
        since = _t.strftime("%d-%b-%Y", _t.gmtime(_t.time() - days * 86400))
        typ, data = M.uid("search", None, "SINCE", since)
        uids = (data[0].split() if data and data[0] else [])[-max_msgs:]
        for uid in uids:
            typ, md = M.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
            if not md or not md[0]:
                continue
            hdr = email.message_from_bytes(md[0][1])
            frm = _decode(hdr.get("From"))
            subj = _decode(hdr.get("Subject"))
            # בודקים אם יש PDF בלי להוריד את כל הגוף (BODYSTRUCTURE)
            typ, bs = M.uid("fetch", uid, "(BODYSTRUCTURE)")
            has_pdf = bool(bs and bs[0] and b"PDF" in bs[0].upper())
            if has_pdf:
                out["with_pdf"] += 1
                out["items"].append({"from": frm[:80], "subject": subj[:80]})
        # שולחים ייחודיים (לזיהוי כתובת השולח של עותקי הקופה)
        out["senders"] = sorted({i["from"] for i in out["items"]})
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "reason": str(e)}
    finally:
        try:
            M.logout()
        except Exception:  # noqa: BLE001
            pass
    return out


def inbox_dump(limit: int = 50) -> dict:
    """דיאגנוסטיקה: סורק את ה-INBOX עצמו (לא All Mail) ומחזיר לכל הודעה From/Subject/
    flags/has_pdf — כדי להבין מה תקוע שם (bounce מ-mailer-daemon? \\Deleted נסתר?).
    מאחד ALL + DELETED כדי לתפוס גם הודעות מסומנות-מחיקה ש-Gmail מסתיר מ-SEARCH רגיל."""
    if not configured():
        return {"ok": False, "reason": "imap-not-configured"}
    out = {"ok": True, "exists": None, "all": 0, "deleted": 0, "items": []}
    try:
        M = imaplib.IMAP4_SSL(IMAP_HOST)
        M.login(IMAP_USER, IMAP_PASS)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"login: {e}"}
    try:
        typ, sel = M.select("INBOX", readonly=True)
        try:
            out["exists"] = int(sel[0])
        except Exception:  # noqa: BLE001
            pass
        uids = []
        seen = set()
        for crit in (("ALL",), ("DELETED",)):
            try:
                typ, d = M.uid("search", None, *crit)
                u = (d[0].split() if d and d[0] else [])
                out["all" if crit[0] == "ALL" else "deleted"] = len(u)
                for x in u:
                    if x not in seen:
                        seen.add(x); uids.append(x)
            except Exception:  # noqa: BLE001
                pass
        # סריקה **מלאה** באצוות — מחזירים רק הודעות שנראות קשורות לחשבונית (חיפוש מילים
        # בנושא) עם From/Subject/thrid האמיתיים, כדי לראות מה באמת בתיבה.
        NEEDLES = ("חשבונית", "1082", "53996", "1041", "53954", "מובילים", "ג.א.א.ל")
        CHUNK = 100
        for i in range(0, len(uids), CHUNK):
            chunk = uids[i:i + CHUNK]
            try:
                uid_csv = b",".join(chunk).decode()
                typ, md = M.uid("fetch", uid_csv,
                                "(X-GM-THRID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
                for part in (md or []):
                    if not (isinstance(part, tuple) and part[0]):
                        continue
                    env_s = part[0].decode("utf-8", "replace")
                    mt = re.search(r"X-GM-THRID (\d+)", env_s)
                    hdr = email.message_from_bytes(part[1] or b"")
                    frm = _decode(hdr.get("From"))
                    subj = _decode(hdr.get("Subject"))
                    if any(n in subj for n in NEEDLES) or any(n in frm for n in ("daemon", "mailer")):
                        out["items"].append({"from": frm[:50], "subject": subj[:70],
                                             "thrid": mt.group(1) if mt else None})
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "reason": str(e)}
    finally:
        try:
            M.logout()
        except Exception:  # noqa: BLE001
            pass
    return out


def capture(max_msgs: int = 80) -> dict:
    """קורא את התיבה, קולט עותקי מסמכים חדשים. חוזר סיכום."""
    if not configured():
        return {"ok": False, "reason": "imap-not-configured"}
    res = {"ok": True, "scanned": 0, "new": 0, "parsed": 0}
    try:
        M = imaplib.IMAP4_SSL(IMAP_HOST)
        M.login(IMAP_USER, IMAP_PASS)
    except Exception as e:  # noqa: BLE001
        logger.warning("imap login failed: %s", e)
        return {"ok": False, "reason": f"login: {e}"}
    try:
        box = _all_mail_folder(M)               # עותקי self-send מדלגים על INBOX
        M.select(f'"{box}"', readonly=True)     # readonly — לא נוגעים בדגלי Itzik
        import time as _t
        since = _t.strftime("%d-%b-%Y", _t.gmtime(_t.time() - SCAN_DAYS * 86400))
        crit = ["SINCE", since]
        if INVOICE_FROM:
            crit = ["FROM", INVOICE_FROM, "SINCE", since]
        typ, data = M.uid("search", None, *crit)
        uids = (data[0].split() if data and data[0] else [])[-max_msgs:]
        for uid in uids:
            uid_s = uid.decode() if isinstance(uid, bytes) else str(uid)
            res["scanned"] += 1
            if db.invoice_exists(uid_s):
                continue
            typ, md = M.uid("fetch", uid, "(RFC822)")
            if not md or not md[0]:
                continue
            msg = email.message_from_bytes(md[0][1])
            subject = _decode(msg.get("Subject"))
            # אוספים את ה-PDF הראשון
            pdf_bytes, fname = None, ""
            for part in msg.walk():
                ct = (part.get_content_type() or "").lower()
                fn = _decode(part.get_filename() or "")
                if ct == "application/pdf" or fn.lower().endswith(".pdf"):
                    try:
                        pdf_bytes = part.get_payload(decode=True)
                        fname = fn or "invoice.pdf"
                        break
                    except Exception:  # noqa: BLE001
                        continue
            if not pdf_bytes:
                continue
            parsed = _parse(pdf_bytes, subject=subject)
            # מניעת כפילות: הקופה שולחת לפעמים עותק כפול לאותו מסמך
            if parsed.get("doc_number") and db.invoice_doc_exists(parsed["doc_number"]):
                continue
            if any(parsed.values()):
                res["parsed"] += 1
            iid = db.invoice_add(
                email_uid=uid_s,
                pdf_b64=base64.b64encode(pdf_bytes).decode(),
                filename=fname, subject=subject, **parsed)
            if iid:
                res["new"] += 1
                logger.info("invoice captured #%s uid=%s doc=%s total=%s phone=%s",
                            iid, uid_s, parsed.get("doc_number"), parsed.get("total"),
                            parsed.get("customer_phone"))
        # אחרי הקליטה — מעבירים את חשבוניות הלקוח מה-INBOX לתווית ייעודית (ארכוב)
        if FILE_ENABLE:
            res["file"] = file_to_folder(M)
    except Exception as e:  # noqa: BLE001
        logger.warning("invoice capture error: %s", e)
        res["ok"] = False
        res["error"] = str(e)
    finally:
        try:
            M.logout()
        except Exception:  # noqa: BLE001
            pass
    return res
