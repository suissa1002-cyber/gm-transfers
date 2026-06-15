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


def configured() -> bool:
    return bool(IMAP_USER and IMAP_PASS)


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


def _parse(pdf_bytes: bytes) -> dict:
    """מחלץ מזהים מה-PDF (best-effort). חוזר dict עם מה שנמצא."""
    t = _pdf_text(pdf_bytes)
    out = {"doc_type": "", "doc_number": "", "total": "", "issued_date": "",
           "customer_name": "", "customer_phone": ""}
    if not t:
        return out
    # סוג + מספר מסמך
    if re.search(r"חשבונית\s*(?:מס\s*)?זיכוי", t):
        out["doc_type"] = "חשבונית זיכוי"
    elif re.search(r"חשבונית\s*מס", t):
        out["doc_type"] = "חשבונית מס"
    elif re.search(r"קבלה", t):
        out["doc_type"] = "קבלה"
    m = re.search(r"חשבונית\s*(?:מס\s*)?(?:זיכוי\s*)?(\d{3,})", t) \
        or re.search(r"קבלה\s*(?:מס['׳]?\s*)?(\d{3,})", t) \
        or re.search(r"מסמך\D{0,8}(\d{4,})", t)
    if m:
        out["doc_number"] = m.group(1)
    # סכום (סה"כ לתשלום / סה"כ כולל מע"מ)
    mt = re.search(r"(?:סה[\"״]?כ\s*(?:לתשלום|כולל)?[^\d]{0,12})([\d,]+\.\d{2})", t)
    if mt:
        out["total"] = mt.group(1).replace(",", "")
    # תאריך
    md = re.search(r"(\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4})", t)
    if md:
        out["issued_date"] = md.group(1)
    # טלפון ישראלי של הלקוח
    mp = re.search(r"\b(0(?:5\d|7\d|[2-489])[-\s]?\d{3}[-\s]?\d{4})\b", t)
    if mp:
        out["customer_phone"] = re.sub(r"[-\s]", "", mp.group(1))
    # שם לקוח — אחרי "לכבוד" / "שם הלקוח"
    mn = re.search(r"(?:לכבוד|שם\s*הלקוח|לקוח)\s*[:\-]?\s*(.+)", t)
    if mn:
        out["customer_name"] = mn.group(1).strip()[:80]
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
        M.select("INBOX", readonly=True)        # readonly — לא נוגעים בדגלי Itzik
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
            parsed = _parse(pdf_bytes)
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
