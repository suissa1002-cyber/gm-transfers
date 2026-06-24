"""לקוח ל-REST API של מרכזיית 1com (proxyapi.php).

נטוויל סיפקו API key (₪80/חודש, התקבל 24/06/2026). זה המקור הסמכותי
ל-CDR — נענה/לא-נענה/עסוק + משך + שיחות יוצאות — מה ש-curl-בזרימה לא נתן
(ושבר צלצול). המפתח סוד: נקרא מ-env בלבד.

Env:
    PBX_API_KEY  — מפתח ה-API של 1com (חובה; בלעדיו הלקוח "כבוי")
    PBX_TENANT   — קוד הדייר (ברירת מחדל "greenmobile")

תיעוד: https://1com-api-doco-site-bwnu.vercel.app/
Base:  https://pbx6webserver.1com.co.il/pbx/proxyapi.php
"""
import os
import time
import logging
from datetime import date, datetime, timedelta
from typing import Optional

import requests

logger = logging.getLogger("onecom")

BASE_URL = "https://pbx6webserver.1com.co.il/pbx/proxyapi.php"


def _key() -> str:
    return (os.getenv("PBX_API_KEY") or "").strip()


def _tenant() -> str:
    return (os.getenv("PBX_TENANT") or "greenmobile").strip()


def is_configured() -> bool:
    """האם יש מפתח API מוגדר (אחרת אין טעם לקרוא ל-1com)."""
    return bool(_key())


def _get(params: dict, timeout: int = 30) -> requests.Response:
    p = {"key": _key(), "tenant": _tenant(), **params}
    return requests.get(BASE_URL, params=p, timeout=timeout)


# ── זיכרון-מטמון קצר ל-CDR (טווח תאריכים → (זמן, שורות)) ──
_CDR_CACHE: dict = {}
_CDR_TTL = 300  # 5 דק' — הדשבורד טרי מספיק, ולא מציף את 1com


def fetch_simple_cdrs(start: str, end: str, calleridnum: str = "",
                      use_cache: bool = True) -> list:
    """שולף CDR (SIMPLECDRS) לטווח [start, end] (YYYY-MM-DD), מחזיר רשומות מנורמלות.

    כל רשומה: {ts(datetime|None), ts_str, direction(in/out), from, to,
               name, disposition(answered/no_answer/busy/abandoned/other),
               duration(int sec), uid, answered_by}.
    מחזיר [] אם אין מפתח או בכשל (לא זורק — הדשבורד לא נופל בגלל 1com).
    """
    if not is_configured():
        return []
    ck = (start, end, calleridnum)
    now = time.time()
    if use_cache:
        hit = _CDR_CACHE.get(ck)
        if hit and (now - hit[0]) < _CDR_TTL:
            return hit[1]
    params = {"reqtype": "INFO", "info": "SIMPLECDRS",
              "start": start, "end": end, "format": "json"}
    if calleridnum:
        params["calleridnum"] = calleridnum
    try:
        r = _get(params)
        if r.status_code != 200:
            logger.warning("1com CDR http %s", r.status_code)
            return []
        body = (r.text or "").strip()
        if not body:
            rows = []
        elif "mistaken the security api key" in body.lower():
            logger.error("1com CDR: bad api key / tenant")
            return []
        else:
            data = r.json()
            rows = [_normalize(rec) for rec in data] if isinstance(data, list) else []
    except Exception as e:  # noqa: BLE001
        logger.warning("1com CDR fetch failed: %s", e)
        return []
    _CDR_CACHE[ck] = (now, rows)
    return rows


_DISPO_MAP = {
    "ANSWERED": "answered",
    "NO ANSWER": "no_answer",
    "NOANSWER": "no_answer",
    "BUSY": "busy",
    "ABANDONED": "abandoned",
    "FAILED": "failed",
    "CONGESTION": "failed",
}


def _normalize(rec: dict) -> dict:
    ts_str = str(rec.get("sc_start") or "").strip()
    ts = None
    if ts_str:
        try:
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        except Exception:  # noqa: BLE001
            ts = None
    direction = "out" if str(rec.get("sc_direction") or "").upper() == "OUT" else "in"
    dispo_raw = str(rec.get("sc_disposition") or "").strip().upper()
    try:
        duration = int(str(rec.get("sc_duration") or "0").strip() or "0")
    except Exception:  # noqa: BLE001
        duration = 0
    return {
        "ts": ts,
        "ts_str": ts_str,
        "direction": direction,
        "from": str(rec.get("sc_calleridnum") or "").strip(),
        "to": str(rec.get("sc_dialednum") or "").strip(),
        "name": str(rec.get("sc_calleridname") or "").strip(),
        "disposition": _DISPO_MAP.get(dispo_raw, "other" if dispo_raw else "other"),
        "disposition_raw": dispo_raw,
        "duration": duration,
        "uid": str(rec.get("sc_uniqueid") or "").strip(),
        "answered_by": str(rec.get("sc_whoanswered") or "").strip(),
    }


_DISPO_LABELS = [
    ("answered", "נענו"),
    ("no_answer", "לא נענו"),
    ("busy", "עסוק"),
    ("abandoned", "ננטשו"),
    ("failed", "נכשלו"),
    ("other", "אחר"),
]
_MISSED = {"no_answer", "busy", "abandoned", "failed"}


def aggregate_cdrs(rows: list, start: str = "", end: str = "") -> dict:
    """מקבץ רשומות CDR מנורמלות לאנליטיקה: תוצאות (נענה/לא-נענה/עסוק),
    שיחות נכנסות/יוצאות, משכים, סדרה יומית (נענו מול שלא-נענו) ושעות עומס."""
    totals = {"total": 0, "in": 0, "out": 0,
              "answered": 0, "no_answer": 0, "busy": 0,
              "abandoned": 0, "failed": 0, "other": 0}
    per_day = {}          # date -> {answered, missed, out}
    per_hour = {h: 0 for h in range(24)}
    talk_sec = 0          # סך משך שיחות שנענו (כל הכיוונים)
    talk_n = 0
    by_name = {}          # שם ניתוב (sc_calleridname) לנכנסות -> ספירה
    for r in rows:
        d = r.get("disposition") or "other"
        totals["total"] += 1
        totals[d] = totals.get(d, 0) + 1
        totals[r["direction"]] = totals.get(r["direction"], 0) + 1
        if d == "answered" and r.get("duration"):
            talk_sec += int(r["duration"])
            talk_n += 1
        ts = r.get("ts")
        if ts:
            dk = ts.date().isoformat()
            slot = per_day.setdefault(dk, {"answered": 0, "missed": 0, "out": 0})
            if r["direction"] == "out":
                slot["out"] += 1
            elif d == "answered":
                slot["answered"] += 1
            elif d in _MISSED:
                slot["missed"] += 1
            per_hour[ts.hour] = per_hour.get(ts.hour, 0) + 1
        if r["direction"] == "in":
            nm = (r.get("name") or "").strip()
            if nm:
                by_name[nm] = by_name.get(nm, 0) + 1
    # שיעור מענה: מתוך נכנסות שהגיעו ליעד (נענו + לא-נענו + עסוק), כמה נענו
    reachable = totals["answered"] + totals["no_answer"] + totals["busy"]
    answer_rate = round(100 * totals["answered"] / reachable) if reachable else 0
    # סדרה יומית רציפה על פני החלון
    series = []
    if start and end:
        try:
            sd = datetime.strptime(start, "%Y-%m-%d").date()
            ed = datetime.strptime(end, "%Y-%m-%d").date()
            cur = sd
            while cur <= ed:
                k = cur.isoformat()
                slot = per_day.get(k, {"answered": 0, "missed": 0, "out": 0})
                series.append({"date": k, **slot})
                cur += timedelta(days=1)
        except Exception:  # noqa: BLE001
            series = [{"date": k, **v} for k, v in sorted(per_day.items())]
    else:
        series = [{"date": k, **v} for k, v in sorted(per_day.items())]
    dispo = [{"key": k, "label": lbl, "count": totals.get(k, 0)}
             for k, lbl in _DISPO_LABELS if totals.get(k, 0)]
    return {
        "configured": True,
        "totals": totals,
        "answer_rate": answer_rate,
        "talk_total_sec": talk_sec,
        "talk_avg_sec": round(talk_sec / talk_n) if talk_n else 0,
        "series": series,
        "dispo": dispo,
        "hours": [{"hour": h, "count": per_hour.get(h, 0)} for h in range(24)],
        "by_name": sorted([{"name": k, "count": v} for k, v in by_name.items()],
                          key=lambda x: -x["count"]),
        "from": start, "to": end,
    }


def cdr_stats(start: str, end: str) -> dict:
    """שליפת CDR לטווח + קיבוץ — לשימוש ה-endpoint. מחזיר {configured:false}
    אם אין מפתח (כדי שה-frontend ידע להסתיר את הסעיף)."""
    if not is_configured():
        return {"configured": False}
    rows = fetch_simple_cdrs(start, end)
    return aggregate_cdrs(rows, start, end)


def dial(source_ext: str, dest: str, account: str = "source",
         var: str = "") -> dict:
    """Click2call: מצלצל קודם ל-source_ext ואז מחייג ל-dest.
    מחזיר {ok, originate_id, raw} (לא זורק)."""
    if not is_configured():
        return {"ok": False, "originate_id": "", "raw": "no api key"}
    params = {"reqtype": "DIAL", "source": source_ext, "dest": dest}
    if account:
        params["account"] = account
    if var:
        params["var"] = var
    try:
        r = _get(params, timeout=20)
        raw = (r.text or "").strip()
        parts = raw.split("|")
        ok = raw.lower().startswith("success")
        oid = parts[2] if ok and len(parts) > 2 else ""
        return {"ok": ok, "originate_id": oid, "raw": raw}
    except Exception as e:  # noqa: BLE001
        logger.warning("1com dial failed: %s", e)
        return {"ok": False, "originate_id": "", "raw": str(e)}


if __name__ == "__main__":  # בדיקה ידנית מהירה: PBX_API_KEY=... python onecom_client.py
    logging.basicConfig(level=logging.INFO)
    today = date.today()
    wk = today - timedelta(days=4)
    cdrs = fetch_simple_cdrs(wk.isoformat(), today.isoformat(), use_cache=False)
    print(f"configured={is_configured()} tenant={_tenant()} rows={len(cdrs)}")
    from collections import Counter
    print("dispositions:", Counter(c["disposition"] for c in cdrs))
    print("directions:", Counter(c["direction"] for c in cdrs))
    for c in cdrs[:5]:
        print(c["ts_str"], c["direction"], c["from"], "->", c["to"],
              c["disposition"], f"{c['duration']}s", c["name"])
