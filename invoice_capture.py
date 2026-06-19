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


def file_to_folder(M) -> dict:
    """מעביר חשבוניות לקוח (FROM=greenmobile.eshop, עם PDF) מ-INBOX לתווית 'חשבוניות
    לקוחות' ומסיר מ-INBOX (ארכוב: +X-GM-LABELS תווית, -X-GM-LABELS \\Inbox — ההודעה
    נשארת ב-All Mail תחת התווית, ללא סיכון Trash).
    בטוח לאיציק (שמטפל רק ב-from:hclickapp) — מסנן בדיוק לפי שולח חשבוניות הלקוח.
    משתמש ב-PEEK כדי לא לגעת בדגלי \\Seen."""
    res = {"checked": 0, "filed": 0, "remaining": None}
    try:
        M.select("INBOX", readonly=False)
        lbl = '"%s"' % _imap_utf7(FILE_LABEL)
        # ⚠️ לארכוב סורקים את **כל** חשבוניות הלקוח שב-INBOX לפי FROM בלבד — **בלי SINCE**.
        # ה-SINCE (חלון 14 יום) החריג חשבוניות ישנות יותר → הן נתקעו בנכנסות לנצח (זה
        # היה הבאג). מסונן ממילא לפי שולח + PDF, אז סריקת-הכל בטוחה. ה-DELETED מאחד גם
        # הודעות שסומנו \Deleted בריצות ישנות (Gmail מחריג אותן מ-SEARCH רגילה).
        uids = set()
        for crit in (("FROM", FILE_SENDER),
                     ("FROM", FILE_SENDER, "DELETED")):
            try:
                typ, data = M.uid("search", None, *crit)
                if data and data[0]:
                    uids.update(data[0].split())
            except Exception:  # noqa: BLE001
                pass
        for uid in uids:
            res["checked"] += 1
            typ, md = M.uid("fetch", uid, "(BODY.PEEK[])")
            if not md or not md[0]:
                continue
            msg = email.message_from_bytes(md[0][1])
            has_pdf = any((p.get_content_type() or "").lower() == "application/pdf"
                          or (_decode(p.get_filename() or "")).lower().endswith(".pdf")
                          for p in msg.walk())
            if not has_pdf:                  # לא חשבונית (PDF) → לא נוגעים
                continue
            M.uid("STORE", uid, "+X-GM-LABELS", lbl)          # מצמיד תווית ייעודית
            M.uid("STORE", uid, "-FLAGS", "\\Deleted")        # מנקה דגל מחיקה תקוע מריצות ישנות
            # ארכוב Gmail-בטוח: מסירים את התווית \Inbox (ההודעה נשארת ב-All Mail תחת
            # התווית). \Deleted+EXPUNGE לא אמין ב-Gmail — לכן נמנעים ממנו.
            M.uid("STORE", uid, "-X-GM-LABELS", "\\Inbox")
            res["filed"] += 1
        # דיאגנוסטיקה: כמה חשבוניות לקוח עדיין נשארו ב-INBOX (אחרי הניקוי אמור להיות ~0)
        try:
            typ, d2 = M.uid("search", None, "FROM", FILE_SENDER)
            res["remaining"] = len(d2[0].split()) if d2 and d2[0] else 0
        except Exception:  # noqa: BLE001
            pass
        if res["filed"]:
            logger.info("filed %d customer invoices to '%s'", res["filed"], FILE_LABEL)
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
