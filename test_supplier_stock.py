"""📦 GreenDay · סנכרון מלאי ספקים — פרסור דוח ישפאר, כללים, מיפוי, חסימה, payloads, span אספקה,
הגנת מלאי פיזי, סיכום חכם, השוואה שבועית, ריצה עם התקדמות ו-endpoints (WC מדומה, בלי רשת).

הרצה: python3 -m pytest -q test_supplier_stock.py   (או python3 test_supplier_stock.py)
"""
import base64
import copy
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ.update(TRANSFERS_DB_PATH=_tmp, DATABASE_URL="", ADMIN_PASSWORD="legacy-secret")

import db  # noqa: E402
import supplier_stock as ss  # noqa: E402

db.init_db()
FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests", "fixtures", "ishpar_dragon_20261019.xlsx")
CONTENT = open(FIX, "rb").read()
CFG = dict(ss.DEFAULT_CONFIG)
ROWS = ss.parse_report(CONTENT, "דוח זמינות מלאי ריהוט דרגון - לקוחות.xlsx", CFG)
BY_MODEL = {r["model"]: r for r in ROWS}
T = 3630


# ── WC מדומה ──
def _var(vid, sku, st="instock", desc="", ms=False, q=None, color="שחור"):
    return {"id": vid, "sku": sku, "stock_status": st, "description": desc, "manage_stock": ms, "stock_quantity": q,
            "attributes": [{"name": "בחירת צבע", "option": color}], "status": "publish"}


def catalog():
    return {
        46373: {"id": 46373, "name": "כיסא Dragon FLEX", "type": "variable", "status": "publish", "tags": [{"id": T}],
                "permalink": "https://greenmobile.co.il/p/flex", "variations": [
                    _var(46414, "7290114432368", st="outofstock", color="שחור / אפור"),
                    _var(46415, "7290114431842", st="outofstock", color="שחור")]},
        46337: {"id": 46337, "name": "כיסא Dragon Monster XXL", "type": "simple", "status": "publish", "sku": "",
                "stock_status": "instock", "manage_stock": False, "tags": [{"id": T}],
                "permalink": "https://greenmobile.co.il/p/monster", "meta_data": []},
        500: {"id": 500, "name": "שולחן/כיסאות מעורב", "type": "variable", "status": "publish", "tags": [{"id": T}],
              "permalink": "https://greenmobile.co.il/p/500", "variations": [
                  _var(501, BY_MODEL["GPDRA-TR-W"]["barcode"], desc="<p>שולחן לבן</p>", color="לבן"),
                  _var(502, BY_MODEL["GPDRC-SNIP-B"]["barcode"], desc='<p>כחול</p><span class="gm-lead" data-days="21"></span>',
                       color="כחול"),
                  _var(503, BY_MODEL["GPDRC-CYBER-B"]["barcode"], color="שחור")]},
        600: {"id": 600, "name": "כיסא ACE (מלאי בסניף)", "type": "variable", "status": "publish", "tags": [{"id": T}],
              "permalink": "https://greenmobile.co.il/p/600", "variations": [
                  _var(601, "520999", ms=True, q=3, color="שחור")]},
        700: {"id": 700, "name": "כיסא GT DLX שחור", "type": "simple", "status": "draft", "sku": "GPDRC-GT-BL",
              "stock_status": "instock", "manage_stock": False, "tags": [{"id": T}], "permalink": "", "meta_data": []},
        800: {"id": 800, "name": "כיסא R70", "type": "variable", "status": "publish", "tags": [{"id": T}],
              "permalink": "https://greenmobile.co.il/p/r70", "variations": [
                  _var(801, BY_MODEL["GPDRC-R70-B"]["barcode"], color="שחור")]},
    }


class FakeWC:
    def __init__(self):
        self.P = catalog()
        self.posts = []
        self.fail_pid = None

    def _p(self, p):
        out = {k: v for k, v in p.items() if k != "variations"}
        return out

    def get(self, path, params=None):
        params = params or {}
        if path == "/products":
            if "include" in params:
                ids = {int(x) for x in params["include"].split(",")}
                return [self._p(p) for i, p in self.P.items() if i in ids]
            if "sku" in params:
                skus = {s.upper() for s in params["sku"].split(",")}
                out = []
                for p in self.P.values():
                    if p["type"] == "simple" and (p.get("sku") or "").upper() in skus:
                        out.append(self._p(p))
                    for v in p.get("variations") or []:
                        if v["sku"].upper() in skus:
                            out.append({**v, "type": "variation", "parent_id": p["id"], "name": p["name"]})
                return out
            if "search" in params:
                return [self._p(p) for p in self.P.values() if params["search"] in p["name"]]
        if path.startswith("/products/") and path.endswith("/variations"):
            pid = int(path.split("/")[2])
            return copy.deepcopy(self.P[pid].get("variations") or []) if params.get("page", 1) == 1 else []
        raise AssertionError(f"GET לא צפוי {path}")

    def post(self, path, body):
        self.posts.append((path, copy.deepcopy(body)))
        if path.endswith("/variations/batch"):
            pid = int(path.split("/")[2])
            if pid == self.fail_pid:
                raise RuntimeError("WC 500")
            out = []
            for u in body["update"]:
                v = next(x for x in self.P[pid]["variations"] if x["id"] == u["id"])
                v.update({k: u[k] for k in ("stock_status", "description") if k in u})
                out.append({"id": u["id"]})
            return {"update": out}
        if path == "/products/batch":
            out = []
            for u in body["update"]:
                p = self.P[u["id"]]
                if "stock_status" in u:
                    p["stock_status"] = u["stock_status"]
                if "meta_data" in u:
                    p["meta_data"] = u["meta_data"]
                out.append({"id": u["id"]})
            return {"update": out}
        raise AssertionError(f"POST לא צפוי {path}")


def _new_supplier(name="בדיקה"):
    sid = db.ss_supplier_save(name, dict(CFG))
    for bc, (pid, vid) in ss.ISHPAR_SEEDS.items():
        db.ss_map_upsert(sid, bc, wc_product_id=pid, wc_variation_id=vid, confirmed=0, ignored=0, source="seed")
    return sid


# ── פרסור ──
def test_parse_fixture_counts():
    assert len(ROWS) == 112
    assert sum(r["avail"] == "יש מלאי" for r in ROWS) == 60
    assert sum(r["avail"] == "אין מלאי" for r in ROWS) == 52
    assert all(r["barcode"].isdigit() for r in ROWS)
    assert "DRAGON CHAIR" not in BY_MODEL and "דגם" not in BY_MODEL


def test_norm_barcode_and_csv():
    assert ss.norm_barcode(7290114430838) == "7290114430838"
    assert ss.norm_barcode(7290114430838.0) == "7290114430838"
    assert ss.norm_barcode(" 7290114430838 ") == "7290114430838"
    assert ss.norm_barcode("7290114430838.0") == "7290114430838"
    assert ss.norm_barcode(None) == ""
    csv_txt = "DRAGON CHAIR,,,,,,\nדגם,תיאור,מחיר,,ברקוד,זמינות,הערות\nGPDRC-X,כיסא,699,,7290000000001,יש מלאי,\n" \
              "GPDRC-Y,כיסא,699,,7290000000002,אין מלאי,יחידות בודדות\nשולחנות,,,,,,\n"
    rows = ss.parse_report(csv_txt.encode("utf-8"), "a.csv", CFG)
    assert [r["barcode"] for r in rows] == ["7290000000001", "7290000000002"] and rows[1]["note"] == "יחידות בודדות"


# ── כללים ──
def test_classify_rules():
    assert ss.classify(BY_MODEL["GPDRA-TR-W"], CFG) == {"kind": "lead", "status": "instock", "lead": 21}
    assert ss.classify(BY_MODEL["GPDRC-SNIP-B"], CFG) == {"kind": "keep", "status": "instock", "lead": None}
    assert ss.classify(BY_MODEL["GPDRC-CYBER-B"], CFG) == {"kind": "oos", "status": "outofstock", "lead": None}
    assert ss.classify(BY_MODEL["GPDRC-FLEX-BB"], CFG)["status"] == "instock"
    assert ss.classify({"avail": "אין מלאי", "note": "צפי לא ידוע"}, CFG)["kind"] == "unknown"
    assert ss.classify({"avail": "אין מלאי", "note": "ניתן לעדכן 14 ימי עסקים"}, CFG)["lead"] == 14
    kinds = [ss.classify(r, CFG)["kind"] for r in ROWS]
    assert kinds.count("lead") == 5 and kinds.count("keep") == 15 and kinds.count("oos") == 32


def test_lead_span_idempotent():
    d0 = "<p>שולחן</p>"
    d1 = ss.lead_apply(d0, 21)
    assert ss.lead_days(d1) == 21 and d1.count("gm-lead") == 1
    assert ss.lead_apply(d1, 21) == d1
    d2 = ss.lead_apply(d1, 14)
    assert ss.lead_days(d2) == 14 and d2.count("gm-lead") == 1
    assert ss.lead_apply(d2, None) == d0 and ss.lead_apply(ss.lead_apply(d2, None), None) == d0
    assert ss.lead_days('<p>x</p>\n<span class="gm-lead" data-days="7"></span>') == 7


def test_pos_physical_protection():
    assert ss.is_physical({"sku": "520654", "manage_stock": True, "stock_quantity": 2})
    assert not ss.is_physical({"sku": "520654", "manage_stock": True, "stock_quantity": 0})
    assert not ss.is_physical({"sku": "520654", "manage_stock": False, "stock_quantity": 5})
    assert not ss.is_physical({"sku": "7290114430838", "manage_stock": True, "stock_quantity": 5})
    assert not ss.is_physical({"sku": "620654", "manage_stock": True, "stock_quantity": 5})


# ── מיפוי, חסימה, תוכנית, ביצוע ──
def test_full_flow():
    wc = FakeWC()
    sid = _new_supplier("ישפאר-בדיקה")
    rid = db.ss_report_add(sid, "week1.xlsx", "asi", ROWS)
    st = ss.suggest_mappings(sid, ROWS, wc, report_id=rid)
    maps = db.ss_map_all(sid)
    assert len(maps) == 112
    tr = maps[BY_MODEL["GPDRA-TR-W"]["barcode"]]
    assert (tr["wc_product_id"], tr["wc_variation_id"], tr["source"], tr["confirmed"]) == (500, 501, "sku", 0)
    gt = maps[BY_MODEL["GPDRC-GT-BL"]["barcode"]]
    assert (gt["wc_product_id"], gt["wc_variation_id"], gt["source"]) == (700, None, "model")
    seed = maps["7290114432368"]
    assert (seed["wc_product_id"], seed["wc_variation_id"], seed["source"]) == (46373, 46414, "seed")
    assert st["by_sku"] == 5 and st["by_model"] == 1

    plan = ss.plan_for_report(sid, rid, wc, fresh=True)
    assert plan["blocked"] and plan["n_blocked"] == 112   # כלום לא מאושר
    # אישור גורף: רק sku+seed, לא model
    n = ss.confirm_certain(sid, "asi")
    assert n == 5 + 8       # 5 לפי SKU==ברקוד + 8 זרעים (הזרע של FLEX-FBBG קודם להצעת SKU)
    assert not db.ss_map_all(sid)[BY_MODEL["GPDRC-GT-BL"]["barcode"]]["confirmed"]
    ss.map_action(sid, BY_MODEL["GPDRC-GT-BL"]["barcode"], "confirm", "asi")
    # התאמה ידנית: ACE → וריאציה עם מלאי פיזי בסניף
    ace = BY_MODEL["GPDRC-ACE-BWFB"]["barcode"]
    ss.map_action(sid, ace, "set", "asi", pid=600, vid=601, label="שחור")
    plan = ss.plan_for_report(sid, rid, wc, fresh=True)
    assert plan["blocked"]
    # כל השאר — "לא נמכר אצלנו" (נזכר בשבוע הבא)
    for p in plan["rows"]:
        if p["group"] in ("blocked", "missing_wc"):
            ss.map_action(sid, p["barcode"], "ignore", "asi")
    plan = ss.plan_for_report(sid, rid, wc, fresh=True)
    assert not plan["blocked"], [(p["model"], p["group"], p["msg"]) for p in plan["rows"] if p["group"] in ("blocked", "missing_wc")]
    G = {p["model"]: p for p in plan["rows"]}
    assert G["GPDRA-TR-W"]["group"] == "lead" and G["GPDRA-TR-W"]["lead_change"] == "add"
    assert G["GPDRC-SNIP-B"]["group"] == "lead" and G["GPDRC-SNIP-B"]["lead_change"] == "remove"   # בודדות → בלי span
    assert G["GPDRC-CYBER-B"]["group"] == "close" and G["GPDRC-CYBER-B"]["new_status"] == "outofstock"
    assert G["GPDRC-FLEX-FBBG"]["group"] == "open"
    assert G["GPDRC-MON-BNW"]["group"] == "unchanged"       # מוצר פשוט, בודדות → נשאר במלאי
    assert G["GPDRC-ACE-BWFB"]["group"] == "protected" and "מלאי פיזי" in G["GPDRC-ACE-BWFB"]["msg"]
    assert G["GPDRC-R70-B"]["group"] == "close"
    s = plan["summary"]
    assert s["counts"]["in_report"] == 112 and s["counts"]["unmatched"] == 0
    assert s["counts"]["protected"] == 1 and s["counts"]["down"] == 2 and s["counts"]["back"] == 2
    assert [w["name"] for w in s["listings"]["whole_oos"]] == ["כיסא R70"]
    assert any(w["name"] == "כיסא Dragon FLEX" for w in s["listings"]["whole_back"])

    # payloads
    prods = ss.fetch_products(wc, {500, 600, 46373, 46337, 700, 800}, fresh=True)
    ss.attach_descriptions(plan["rows"], prods)
    pl = {b["path"]: b for b in ss.build_payloads(plan["rows"])}
    v500 = {u["id"]: u for _, u in pl["/products/500/variations/batch"]["items"]}
    assert v500[501] == {"id": 501, "description": '<p>שולחן לבן</p><span class="gm-lead" data-days="21"></span>'}
    assert v500[502] == {"id": 502, "description": "<p>כחול</p>"}
    assert v500[503] == {"id": 503, "stock_status": "outofstock"}
    assert "/products/600/variations/batch" not in pl                       # מוגן — לא נוגעים
    assert all("price" not in json_ for b in pl.values() for _, u in b["items"] for json_ in [str(u)])
    assert all(set(u) <= {"id", "stock_status", "description", "meta_data"} for b in pl.values() for _, u in b["items"])

    # ביצוע מלא (סינכרוני) עם התקדמות, ניקוי מטמון וטלגרם
    run_id = db.ss_run_add(sid, rid, "week1.xlsx", "asi")
    purged, sent, progress = [], [], []
    orig = db.ss_run_update

    def spy(rid_, **f):
        if "done" in f:
            progress.append(f["done"])
        return orig(rid_, **f)
    db.ss_run_update = spy
    try:
        summ = ss.run_apply(run_id, sid, rid, wc, purge=lambda u: purged.append(u) or True, notify=sent.append,
                            link="https://x/?gdstock=1")
    finally:
        db.ss_run_update = orig
    run = db.ss_run_get(run_id)
    assert run["status"] == "done" and run["total"] == run["done"] > 0, run
    assert progress and progress[-1] == run["total"]
    assert summ["counts"]["errors"] == 0 and summ["counts"]["down"] == 2
    assert wc.P[500]["variations"][2]["stock_status"] == "outofstock"
    assert ss.lead_days(wc.P[500]["variations"][0]["description"]) == 21
    assert wc.P[600]["variations"][0]["stock_status"] == "instock"
    assert "https://greenmobile.co.il/p/r70" in purged and "" not in purged
    assert sent and "ירדו מהמלאי: 2" in sent[0] and "ליסט שלם אזל: כיסא R70" in sent[0] and "gdstock" in sent[0]

    # ריצה חוזרת על אותו דוח — אידמפוטנטי (אין שינויים)
    plan2 = ss.plan_for_report(sid, rid, wc, fresh=True)
    assert not ss.build_payloads(plan2["rows"])

    # שבוע 2: CYBER-B חזר, TR-W ירד בלי הערה, ברקוד חדש, R70 נעלם
    rows2 = copy.deepcopy(ROWS)
    for r in rows2:
        if r["model"] == "GPDRC-CYBER-B":
            r["avail"] = "יש מלאי"
        if r["model"] == "GPDRA-TR-W":
            r["note"] = ""
    rows2 = [r for r in rows2 if r["model"] != "GPDRC-R70-B"]
    rows2.append({"row": 999, "model": "GPDRC-NEW", "desc": "כיסא חדש", "price": 1, "barcode": "7290999999999",
                  "avail": "יש מלאי", "note": ""})
    rid2 = db.ss_report_add(sid, "week2.xlsx", "asi", rows2)
    ss.suggest_mappings(sid, rows2, wc, report_id=rid2)
    plan3 = ss.plan_for_report(sid, rid2, wc, fresh=True)
    d = plan3["diff"]
    assert [x["model"] for x in d["back"]] == ["GPDRC-CYBER-B"] and [x["model"] for x in d["down"]] == ["GPDRA-TR-W"]
    assert [x["model"] for x in d["new"]] == ["GPDRC-NEW"] and [x["model"] for x in d["gone"]] == ["GPDRC-R70-B"]
    new_row = next(p for p in plan3["rows"] if p["model"] == "GPDRC-NEW")
    assert new_row["is_new"] and new_row["group"] == "blocked" and plan3["blocked"]
    assert plan3["summary"]["counts"]["new"] == 1
    assert not any(p["is_new"] for p in plan3["rows"] if p["model"] != "GPDRC-NEW")
    assert next(p for p in plan3["rows"] if p["model"] == "GPDRC-ACE-BWFB")["group"] == "protected"
    # ריצה חסומה לא נוגעת באתר
    n_posts = len(wc.posts)
    run2 = db.ss_run_add(sid, rid2, "week2.xlsx", "asi")
    assert ss.run_apply(run2, sid, rid2, wc) is None
    assert db.ss_run_get(run2)["status"] == "error" and len(wc.posts) == n_posts


def test_summary_errors_and_untagged():
    wc = FakeWC()
    wc.P[800]["tags"] = []
    wc.fail_pid = 500
    sid = _new_supplier("שגיאות")
    rid = db.ss_report_add(sid, "w.xlsx", "asi", ROWS)
    ss.suggest_mappings(sid, ROWS, wc, report_id=rid)
    ss.confirm_certain(sid, "asi")
    for p in ss.plan_for_report(sid, rid, wc, fresh=True)["rows"]:
        if p["group"] in ("blocked", "missing_wc"):
            ss.map_action(sid, p["barcode"], "ignore", "asi")
    plan = ss.plan_for_report(sid, rid, wc, fresh=True)
    assert not plan["blocked"]
    assert next(p for p in plan["rows"] if p["model"] == "GPDRC-R70-B")["group"] == "untagged"
    run_id = db.ss_run_add(sid, rid, "w.xlsx", "asi")
    s = ss.run_apply(run_id, sid, rid, wc, purge=None)
    assert s["counts"]["errors"] == 3 and all("WC 500" in e for e in s["errors"].values())
    assert s["counts"]["down"] == 0 and s["counts"]["back"] == 2 and s["counts"]["untagged"] == 1
    assert "שגיאות" in db.ss_run_get(run_id)["phase"]
    # config: לעבד גם בלי תג
    cfg2 = {**CFG, "require_tag": False}
    db.ss_supplier_save("שגיאות", cfg2, sid=sid)
    plan = ss.plan_for_report(sid, rid, wc, fresh=True)
    assert next(p for p in plan["rows"] if p["model"] == "GPDRC-R70-B")["group"] == "close"


def test_seed_and_endpoints():
    import main
    from fastapi.testclient import TestClient
    main.cfg.ADMIN_PASSWORD = "legacy-secret"
    c = TestClient(main.app)
    ADM = {"X-Admin-Key": "legacy-secret"}
    db.device_register("dev-ss-000000000000001", "קופה", "ua", "1.2.3.4", "1", "approved")
    for un, role, pr in [("asi", "owner", []), ("dana", "user", ["orders"])]:
        c.post("/api/admin/users", headers=ADM, json={"username": un, "name": un, "password": "pass123", "role": role, "perms": pr})

    def login(un):
        t = c.post("/api/auth/login", json={"username": un, "password": "pass123"},
                   headers={"X-Device-Token": "dev-ss-000000000000001"}).json()["token"]
        return {"X-User-Token": t, "X-Device-Token": "dev-ss-000000000000001"}
    A, D = login("asi"), login("dana")
    assert c.get("/api/greenday/stock/suppliers", headers=D).status_code == 403
    r = c.get("/api/greenday/stock/suppliers", headers=A).json()
    sup = next(s for s in r["suppliers"] if s["name"] == ss.ISHPAR_NAME)
    assert sup["stale"] and sup["map_stats"]["total"] == 8
    seeds = db.ss_map_all(sup["id"])
    assert seeds["7290114430210"]["wc_product_id"] == 46337 and seeds["7290114430210"]["wc_variation_id"] is None
    assert seeds["7290017725925"]["wc_variation_id"] == 46257 and not seeds["7290017725925"]["confirmed"]
    ss.ensure_seed()
    assert len([s for s in db.ss_suppliers() if s["name"] == ss.ISHPAR_NAME]) == 1   # לא נזרע פעמיים

    ss.cache_clear()
    fake = FakeWC()
    main._ss_wc = lambda: fake
    up = c.post(f"/api/greenday/stock/suppliers/{sup['id']}/upload", headers=A,
                json={"file_name": "דוח זמינות מלאי ריהוט דרגון - לקוחות.xlsx",
                      "content_b64": base64.b64encode(CONTENT).decode()})
    assert up.status_code == 200, up.text
    pv = up.json()
    assert pv["blocked"] and pv["summary"]["counts"]["in_report"] == 112 and pv["groups"]
    rid = pv["report"]["id"]
    assert c.post(f"/api/greenday/stock/reports/{rid}/apply", headers=A).status_code == 409   # חסום
    assert c.post(f"/api/greenday/stock/suppliers/{sup['id']}/upload", headers=A,
                  json={"file_name": "x.pdf", "content_b64": "AA=="}).status_code == 400
    n = c.post(f"/api/greenday/stock/suppliers/{sup['id']}/mapping/confirm-certain", headers=A,
               json={"report_id": rid}).json()["confirmed"]
    assert n >= 8
    res = c.get("/api/greenday/stock/wc-search", headers=A, params={"q": "R70"}).json()["items"]
    assert res and res[0]["variations"][0]["id"] == 801
    bad = c.post(f"/api/greenday/stock/suppliers/{sup['id']}/mapping", headers=A,
                 json={"barcode": BY_MODEL["GPDRC-GT-W"]["barcode"], "action": "set", "wc_product_id": 800})
    assert bad.status_code == 400   # מוצר משתנה בלי וריאציה
    pv = c.get(f"/api/greenday/stock/reports/{rid}/preview", headers=A).json()
    for p in pv["rows"]:
        if p["group"] in ("blocked", "missing_wc"):
            assert c.post(f"/api/greenday/stock/suppliers/{sup['id']}/mapping", headers=A,
                          json={"barcode": p["barcode"], "action": "ignore"}).status_code == 200
    pv = c.get(f"/api/greenday/stock/reports/{rid}/preview", headers=A).json()
    assert not pv["blocked"]
    main._ss_notify = lambda t: None
    # ניקוי מטמון האתר אחרי הסנכרון (wp_purge) פונה לרשת האמיתית — בבדיקה הוא תקע את הריצה
    # ב-'running' מעבר לחלון ההמתנה (נכשל מאז 21/10 כשהאתר היה איטי). לא קשור ללוגיקת הסנכרון.
    ss.run_apply.__defaults__ = (lambda u: True, None, "")
    ap = c.post(f"/api/greenday/stock/reports/{rid}/apply", headers=A)
    assert ap.status_code == 200, ap.text
    import time
    for _ in range(50):
        run = c.get(f"/api/greenday/stock/runs/{ap.json()['run_id']}", headers=A).json()
        if run["status"] != "running":
            break
        time.sleep(0.1)
    assert run["status"] == "done", run
    assert run["summary"]["counts"]["in_report"] == 112 and run["results"]
    hist = c.get(f"/api/greenday/stock/suppliers/{sup['id']}/runs", headers=A).json()["runs"]
    assert hist and hist[0]["id"] == run["id"] and "results" not in hist[0]
    sup2 = next(s for s in c.get("/api/greenday/stock/suppliers", headers=A).json()["suppliers"] if s["id"] == sup["id"])
    assert not sup2["stale"] and sup2["last_run"]["status"] == "done"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))


def test_open_managed_unit_disables_manage_stock():
    """פתיחת וריאציה עם manage_stock=true (qty 0) חייבת לכבות ניהול מלאי — אחרת WC מתעלם."""
    import supplier_stock as ss
    rows = [{"group": "open", "status_change": True, "lead_change": "", "unit_kind": "variation",
             "vid": 46414, "pid": 46373, "barcode": "7290114432368", "new_status": "instock",
             "new_lead": None, "manage_stock": True},
            {"group": "close", "status_change": True, "lead_change": "", "unit_kind": "variation",
             "vid": 46415, "pid": 46373, "barcode": "x", "new_status": "outofstock",
             "new_lead": None, "manage_stock": True}]
    pl = ss.build_payloads(rows)
    items = {u["id"]: u for _, u in pl[0]["items"]}
    assert items[46414]["manage_stock"] is False and items[46414]["stock_status"] == "instock"
    assert "manage_stock" not in items[46415]
