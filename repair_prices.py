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
    """תא מחיר → [{tier, price}] (או [{ref}] להפניה, [] לריק/לא-זמין)."""
    t = _norm(text).replace("\\", "/")
    if not t or t in ("-", "—"):
        return []
    if "כמו" in t and not re.search(r"\d", t.split("כמו")[0]):
        return [{"ref": _norm(t.split("כמו", 1)[1])}]
    nums = [(int(m.group()), m.start()) for m in re.finditer(r"\d+", t)]
    if not nums:
        return []
    # מופעי תוויות מוכרות, ארוכות קודם, בלי כפילות חופפת (מקורי בתוך 'מקורי חדש')
    labels, covered = [], []
    for lab in sorted(_TIERS, key=len, reverse=True):
        for m in re.finditer(re.escape(lab), t, re.I):
            span = (m.start(), m.end())
            if any(span[0] >= c[0] and span[1] <= c[1] for c in covered):
                continue
            labels.append((lab, m.start()))
            covered.append(span)
    if not labels:                       # ללא תוויות: a/b → זול=חילופי / יקר=מקורי
        if len(nums) == 2:
            lo, hi = sorted(n for n, _ in nums)
            return [{"tier": "חילופי", "price": lo}, {"tier": "מקורי", "price": hi}]
        return [{"tier": "", "price": nums[0][0]}]
    out = []                             # כל מספר → התווית הקרובה אליו ביותר
    for n, pos in nums:
        lab = min(((abs(pos - lp), lab) for lab, lp in labels), default=(0, ""))[1]
        out.append({"tier": lab, "price": n})
    return out


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
