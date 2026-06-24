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
              "start": start, "end": _end_exclusive(end), "format": "json"}
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


def _end_exclusive(end: str) -> str:
    """1com מתייחס ל-`end` כבלעדי (חצות תחילת יום הסיום) → מוסיפים יום
    כדי שיום הסיום ייכלל. הקוראים מעבירים תאריך כולל; כאן מתורגם לשאילתה."""
    try:
        return (datetime.strptime(end, "%Y-%m-%d").date() + timedelta(days=1)).isoformat()
    except Exception:  # noqa: BLE001
        return end


def _digits(s: str) -> str:
    return "".join(ch for ch in str(s or "") if ch.isdigit())


def fetch_cdrs_by_phone(phone: str, start: str, end: str) -> list:
    """היסטוריית שיחות למספר נתון דרך simplecdrs (info=cdrs מחזיר JSON שבור!).
    `phone` מסנן calleridnum/dialednum/whoanswered. מחזיר רשומות מנורמלות
    עם uniqueid (להקלטה), משך, תוצאה, כיוון ודגל הקלטה."""
    if not is_configured() or not phone:
        return []
    params = {"reqtype": "INFO", "info": "simplecdrs", "phone": phone,
              "start": start, "end": _end_exclusive(end), "format": "json"}
    try:
        r = _get(params)
        if r.status_code != 200:
            return []
        body = (r.text or "").strip()
        if not body or body[0] != "[" or "mistaken the security api key" in body.lower():
            return []
        data = r.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("1com cdrs-by-phone failed: %s", e)
        return []
    out = []
    for rec in (data if isinstance(data, list) else []):
        n = _normalize(rec)
        n["has_rec"] = bool(n["uid"]) and n["disposition"] == "answered" and n["duration"] > 0
        out.append(n)
    return out


# סיומת uniqueid חוקית (נגד הזרקה ב-proxy ההקלטה)
import re as _re  # noqa: E402
_UID_RE = _re.compile(r"^[A-Za-z0-9._-]{6,80}$")


def get_recording(uid: str):
    """שולף הקלטת MP3 לפי uniqueid. מחזיר (bytes, content_type) או None."""
    if not is_configured() or not uid or not _UID_RE.match(uid):
        return None
    try:
        r = _get({"reqtype": "INFO", "info": "recording", "id": uid}, timeout=45)
        ctype = r.headers.get("Content-Type", "")
        if r.status_code == 200 and "audio" in ctype and r.content:
            return (r.content, ctype)
    except Exception as e:  # noqa: BLE001
        logger.warning("1com recording fetch failed: %s", e)
    return None


_TAG_CACHE: dict = {}


def recent_call_tag(phone_local: str, today: str, tomorrow: str) -> dict:
    """שם-הקו (sc_calleridname = תת-מחלקה, מה-CID-alter) של השיחה הנכנסת
    האחרונה למתקשר — לתיוג היררכי בפופאפ החי. מטמון 8ש'. {name, who}."""
    if not is_configured() or not phone_local:
        return {"name": "", "who": ""}
    now = time.time()
    hit = _TAG_CACHE.get(phone_local)
    if hit and (now - hit[0]) < 2:   # cache קצר → מעבר תת-מחלקה מהיר בפופאפ
        return hit[1]
    res = {"name": "", "who": ""}
    try:
        r = _get({"reqtype": "INFO", "info": "simplecdrs", "phone": phone_local,
                  "start": today, "end": tomorrow, "format": "json"}, timeout=12)
        data = r.json() if r.status_code == 200 and (r.text or "").strip()[:1] == "[" else []
        ins = [x for x in data if str(x.get("sc_direction", "")).upper() == "IN"]
        if ins:
            last = max(ins, key=lambda x: str(x.get("sc_start") or ""))
            res = {"name": str(last.get("sc_calleridname") or "").strip(),
                   "who": str(last.get("sc_whoanswered") or "").strip()}
    except Exception as e:  # noqa: BLE001
        logger.warning("1com recent_call_tag failed: %s", e)
    _TAG_CACHE[phone_local] = (now, res)
    return res


def active_channels() -> list:
    """שיחות פעילות עכשיו (reqtype=CHANNELS) — לפולינג הפופאפ, מחוץ לזרימה.
    מחזיר את ה-JSON הגולמי (list) או [] (כולל כשאין שיחות)."""
    if not is_configured():
        return []
    try:
        # ⚡ nodename מגביל לשרת שלנו → CHANNELS צונח מ~7ש' ל~0.35ש' (קריטי לפופאפ חי)
        node = (os.getenv("PBX_NODE") or "pbx21").strip()
        r = _get({"reqtype": "CHANNELS", "nodename": node, "format": "json"}, timeout=12)
        if r.status_code != 200:
            return []
        body = (r.text or "").strip()
        if not body or body[0] not in "[{":
            return []
        data = r.json()
        return data if isinstance(data, list) else [data]
    except Exception as e:  # noqa: BLE001
        logger.warning("1com channels failed: %s", e)
        return []


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
