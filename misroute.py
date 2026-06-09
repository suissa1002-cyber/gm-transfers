"""
זיהוי "מכשיר לא במקום": כשנסרק סריאל שלא תואם להעברה נכנסת לסניף הסורק,
בודקים חי בקופה לאיזה סניף הסריאל רשום כרגע.

זרימה:
  1. אינדקס סריאל→מוצר (db.serial_product) — מיפוי קבוע (נבנה ב-serial_sync).
  2. קריאה חיה get_product_serials(product_id) → הסניף + הסטטוס העדכניים של הסריאל.
  3. אם הסניף ≠ הסניף הסורק → חריגה (open_misroute) + מחזיר אזהרה.
"""

import logging

import config as cfg
import db
import poller

logger = logging.getLogger("transfers.misroute")


def check(serial: str, scanned_branch_id: int, scanned_by: str = "") -> dict:
    """
    נקרא רק כשהסריקה לא תאמה להעברה נכנסת. מחזיר dict לתצוגה:
      {type: 'misroute'|'sold'|'here'|'unknown', message, expected_branch_id?, expected_branch_name?, product_name?}
    או None אם אין מה לדווח.
    """
    serial = (serial or "").strip()
    if not serial:
        return None

    rec = db.serial_product(serial)
    if not rec:
        return {"type": "unknown", "message": "מכשיר לא מזוהה — לא נמצא בקופה"}

    pid = rec.get("product_id")
    name = rec.get("product_name") or ""

    # בדיקה חיה: היכן הסריאל רשום כרגע + סטטוס
    try:
        serials = poller.client().get_product_serials(pid) or []
    except Exception as e:  # noqa: BLE001
        logger.warning("live serials lookup failed for %s: %s", pid, e)
        return {"type": "unknown", "message": "לא ניתן לאמת מול הקופה כרגע"}

    entry = next((s for s in serials if str(s.get("serial")) == serial), None)
    if not entry:
        # הסריאל לא בין הסריאלים הפעילים של המוצר → כנראה נמכר/יצא ממלאי
        return {"type": "sold", "product_name": name,
                "message": f"המכשיר ({name}) מסומן כנמכר/לא במלאי בקופה"}

    reg_branch = entry.get("branchId")
    try:
        reg_branch = int(reg_branch)
    except (TypeError, ValueError):
        reg_branch = None

    if reg_branch is None:
        return {"type": "unknown", "message": "לא ידוע לאיזה סניף המכשיר משויך"}

    if reg_branch == int(scanned_branch_id):
        # רשום כאן אבל לא על העברה פתוחה — לא חריגה
        return {"type": "here", "product_name": name,
                "message": "המכשיר כבר רשום בסניף זה (לא על העברה פתוחה)"}

    # חריגה אמיתית — שייך לסניף אחר
    db.open_misroute(serial, name, reg_branch, int(scanned_branch_id), scanned_by)
    return {"type": "misroute", "product_name": name,
            "expected_branch_id": reg_branch,
            "expected_branch_name": cfg.branch_name(reg_branch),
            "message": f"⚠️ המכשיר שייך לסניף {cfg.branch_name(reg_branch)}"}
