"""
connectop_cold_export — גיבוי גולמי קר וחד-פעמי של *כל* מאגר שיחות ה-WhatsApp
מ-ConnectOp/ChatRace, לפני ניתוק המנוי (30/06/2026).

בניגוד ל-wa_backfill (ששואב + מפענח + שומר ל-Postgres שלנו), כאן אנחנו שומרים את
ה-rows **הגולמיים בדיוק כפי שהדשבורד מחזיר אותם** לקובץ JSONL קר. ככה גם אם מתישהו
יתגלה ניואנס פענוח — המקור המלא בידינו, בלי תלות ב-ConnectOp החי.

פלט:
  exports/connectop_raw_export.jsonl  — שורה אחת לכל שיחה: {phone,name,archived,channel,n,messages:[raw...]}
  exports/connectop_raw_export.done   — רשימת טלפונים שהושלמו (resume)
  exports/connectop_raw_export.meta.json — סיכום רץ

resumable: ריצה חוזרת מדלגת על טלפונים שכבר בקובץ ה-.done.
read-only מול ConnectOp. לא נוגע בשום DB.
"""
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent  # green-woo/
sys.path.insert(0, str(ROOT / "agents" / "shared"))

# load root .env
env = ROOT / ".env"
if env.exists():
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

from chatrace_dashboard_client import ChatRaceDashboardClient, ChatRaceDashboardError  # noqa: E402

OUT_DIR = HERE / "exports"
OUT_DIR.mkdir(exist_ok=True)
JSONL = OUT_DIR / "connectop_raw_export.jsonl"
DONE = OUT_DIR / "connectop_raw_export.done"
META = OUT_DIR / "connectop_raw_export.meta.json"

ENUM_SLEEP = 0.3   # between conversation-list pages
MSG_SLEEP = 0.3    # between message pages of one conversation
PER_CONV_MAX = 5000
BATCH = 50


def _load_done() -> set:
    if not DONE.exists():
        return set()
    return {l.strip() for l in DONE.read_text(encoding="utf-8").splitlines() if l.strip()}


def _list_all_conversations(client, batch=100, max_total=30000):
    """כל שיחות ה-WhatsApp (channel 5) דרך offset pagination. [{phone,name,archived}]."""
    out, seen, off = [], set(), 0
    while len(out) < max_total:
        try:
            resp = client._post_user_php(
                {"op": "conversations", "op1": "get", "offset": off, "limit": batch})
        except ChatRaceDashboardError as e:
            print(f"[enum] page off={off} failed: {e}", flush=True)
            time.sleep(2)
            break
        rows = resp.get("data", []) if isinstance(resp, dict) else []
        if not rows:
            break
        for r in rows:
            if str(r.get("channel")) != "5":
                continue
            ph = r.get("ms_id")
            if not ph or ph in seen:
                continue
            seen.add(ph)
            out.append({"phone": str(ph),
                        "name": r.get("full_name") or r.get("first_name") or str(ph),
                        "archived": str(r.get("archived", "0")) == "1"})
        if len(rows) < batch:
            break
        off += batch
        if off % 1000 == 0:
            print(f"[enum] …{len(out)} WA conversations so far (off={off})", flush=True)
        time.sleep(ENUM_SLEEP)
    return out


def _fetch_raw(client, phone):
    """כל ההודעות הגולמיות של שיחה אחת (rows כפי שהדשבורד מחזיר), עם pagination."""
    out, off = [], 0
    while len(out) < PER_CONV_MAX:
        rows = client.get_conversation_raw(phone, limit=BATCH, offset=off)
        if not rows:
            break
        out.extend(rows)
        if len(rows) < BATCH:
            break
        off += BATCH
        time.sleep(MSG_SLEEP)
    return out


def main():
    client = ChatRaceDashboardClient.from_env()
    print("[start] enumerating all WhatsApp conversations…", flush=True)
    convs = _list_all_conversations(client)
    total = len(convs)
    done = _load_done()
    print(f"[start] {total} WA conversations; {len(done)} already exported", flush=True)

    processed = 0
    msgs_total = 0
    with JSONL.open("a", encoding="utf-8") as fout, DONE.open("a", encoding="utf-8") as fdone:
        for i, c in enumerate(convs):
            ph = c["phone"]
            if ph in done:
                continue
            try:
                raw = _fetch_raw(client, ph)
            except ChatRaceDashboardError as e:
                print(f"[conv {ph}] FAILED: {e}", flush=True)
                time.sleep(2)
                continue
            rec = {"phone": ph, "name": c["name"], "archived": c["archived"],
                   "channel": "5", "n": len(raw), "messages": raw}
            fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fout.flush()
            fdone.write(ph + "\n")
            fdone.flush()
            processed += 1
            msgs_total += len(raw)
            if processed % 25 == 0:
                meta = {"total_convs": total, "exported": len(done) + processed,
                        "msgs_this_run": msgs_total, "running": True}
                META.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"[progress] {len(done)+processed}/{total} convs, "
                      f"{msgs_total} msgs this run", flush=True)
            time.sleep(MSG_SLEEP)

    meta = {"total_convs": total, "exported": len(done) + processed,
            "msgs_this_run": msgs_total, "running": False, "finished": True}
    META.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[done] exported {processed} new convs this run ({len(done)+processed}/{total}), "
          f"{msgs_total} msgs this run. File: {JSONL}", flush=True)


if __name__ == "__main__":
    main()
