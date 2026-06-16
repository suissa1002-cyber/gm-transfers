"""מחירון תיקוני מעבדה — נקרא מ-Google Sheet שפורסם כ-CSV (REPAIR_PRICES_CSV_URL).

הגיליון אנושי ומבולגן: כמה סקשנים (iPhone/Samsung/...), והמחיר בתא יכול להיות
מספר יחיד, `a/b` (זול=חילופי / יקר=מקורי), תוויות מסומנות לפני/אחרי המספר
(`540 מקורי חדש / 180 חלופי`, `מקורי180/290חלופי`), הערות (`200 ללא פייס`),
הפניות (`כמו אייפון 8`), או `-`. הפרסר דטרמיניסטי — המחירים תמיד מהתא עצמו.
"""
import csv
import io
import os
import re

import requests

CSV_URL = os.getenv("REPAIR_PRICES_CSV_URL", "")
# תוויות איכות מוכרות (הארוכות קודם — כדי ש'מקורי חדש' לא יתפס כ'מקורי')
_TIERS = ["מקורי חדש", "מקורי פירוק", "אולד", "OLED", "מקורי", "חילופי", "חלופי",
          "ללא פייס", "כולל פייס", "כולל face", "ללא face"]


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def parse_cell(text) -> list:
    """תא מחיר → [{tier, price}] (או [{ref}] להפניה, [] לריק/לא-זמין).
    שומר את **כל** הטקסט ליד כל מחיר כתווית (כולל הערות כמו 'עם מסגרת'/'בלי
    מסגרת'/'ללא פייס'), לא רק מילת-מפתח — כדי לא לאבד מידע."""
    t = _norm(text).replace("\\", "/")
    # הערה מכירתית פנימית ("ניתן למכור מקורי במחיר עד חלופי") — לחתוך, לא להציג ללקוח
    t = _norm(re.sub(r"ניתן\s+למכור[^\d]*", " ", t))
    if not t or t in ("-", "—"):
        return []
    if "כמו" in t and not re.search(r"\d", t.split("כמו")[0]):
        return [{"ref": _norm(t.split("כמו", 1)[1])}]
    items = []                           # [(price, label), ...]
    for pc in (p for p in t.split("/") if p.strip()):
        nums = list(re.finditer(r"\d+", pc))
        if not nums:
            continue
        if len(nums) == 1:               # מספר אחד בקטע → התווית = כל שאר הטקסט
            items.append((int(nums[0].group()), _norm(re.sub(r"\d+", " ", pc))))
        else:                            # כמה מספרים בקטע אחד → טקסט שאחרי כל מספר
            for i, m in enumerate(nums):
                end = nums[i + 1].start() if i + 1 < len(nums) else len(pc)
                items.append((int(m.group()), _norm(re.sub(r"\d+", " ", pc[m.end():end]))))
    if not items:
        return []
    if all(not lbl for _p, lbl in items):    # ללא תוויות: a/b → זול=חילופי / יקר=מקורי
        if len(items) == 2:
            lo, hi = sorted(p for p, _ in items)
            return [{"tier": "חילופי", "price": lo}, {"tier": "מקורי", "price": hi}]
        return [{"tier": "", "price": items[0][0]}]
    return [{"tier": lbl, "price": p} for p, lbl in items]


def _is_matrix_header(row) -> bool:
    cells = {_norm(c) for c in row}
    return "מסך" in cells and "סוללה" in cells


def parse(csv_text: str) -> dict:
    """CSV → {devices: {model_lower: {display, repairs: {repair: [tiers]}}}, services: [...]}."""
    rows = list(csv.reader(io.StringIO(csv_text)))
    devices, services = {}, []
    cols = None                          # מיפוי index→שם תיקון (מהכותרת הפעילה)
    for row in rows:
        if not any(_norm(c) for c in row):
            continue
        if _is_matrix_header(row):
            cols = {i: _norm(c) for i, c in enumerate(row) if i > 0 and _norm(c)}
            continue
        model = _norm(row[0])
        # גבול סקשן לא-מטריצה (כותרת כמו AirPods/Watch) = 2+ תאי-טקסט בלי ספרות → איפוס
        if cols and sum(1 for c in row[1:] if _norm(c) and not re.search(r"\d", c)) >= 2:
            cols = None
            continue
        if cols and model:                   # שורת דגם תחת מטריצה (כולל UPPERCASE כמו IPHONE 15)
            repairs = {}
            for i, rep in cols.items():
                if i < len(row):
                    tiers = parse_cell(row[i])
                    if tiers:
                        repairs[rep] = tiers
            if repairs:
                devices[model.lower()] = {"display": model, "repairs": repairs}
            continue
        # סקשן שירותים כלליים: שם שירות + מחיר (לרוב בעמודה 4)
        if not cols:
            price_cell = next((c for c in row[1:] if re.fullmatch(r"\s*\d+\s*", _norm(c))), "")
            name = model or next((_norm(c) for c in row[1:] if _norm(c) and not _norm(c).isdigit()), "")
            if name and price_cell:
                services.append({"name": name, "price": int(_norm(price_cell))})
    # פתרון הפניות ("כמו אייפון 8") — נרמול שמות מותג עברית→אנגלית להתאמת מפתח
    _he = {"אייפון": "iphone", "גלקסי": "galaxy", "סמסונג": "samsung", "אייפד": "ipad"}
    for d in devices.values():
        for rep, tiers in list(d["repairs"].items()):
            if tiers and tiers[0].get("ref"):
                ref = tiers[0]["ref"].lower()
                for he, en in _he.items():
                    ref = ref.replace(he, en)
                ref = _norm(ref)
                tgt = next((v for k, v in devices.items() if ref and (ref in k or k in ref)), None)
                d["repairs"][rep] = (tgt["repairs"].get(rep, []) if tgt else [])
    return {"devices": devices, "services": services}


def fetch_and_parse() -> dict:
    if not CSV_URL:
        return {"devices": {}, "services": []}
    r = requests.get(CSV_URL, timeout=20)
    r.raise_for_status()
    r.encoding = "utf-8"               # Google CSV לא מצהיר charset → requests מנחש Latin-1
    return parse(r.text)
