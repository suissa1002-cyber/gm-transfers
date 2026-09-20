"""זהות על כרטיס: שגיאת הקלדה ≠ זהות שנייה, ושם זהה ≠ קארדינג.

⚠️ 20/09/2026 (51895): עידו חומש, 3 הזמנות שסופקו, נצבע אדום כי בהזמנה
אחת הקליד idocemex@gmail.cim במקום .com.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("WC_STORE_URL", "https://example.invalid")
os.environ.setdefault("WC_CONSUMER_KEY", "k"); os.environ.setdefault("WC_CONSUMER_SECRET", "s")
import main

fails = []

def check(label, got, want):
    ok = got == want
    print(f"{'✅' if ok else '❌'} {label:52} {got!r}")
    if not ok:
        fails.append(f"{label}: קיבלתי {got!r} ציפיתי {want!r}")

print("── נרמול מייל ──")
check("gmail.cim → gmail.com", main._canon_email("idocemex@gmail.cim"), "idocemex@gmail.com")
check("gmail.co → gmail.com", main._canon_email("a@gmail.co"), "a@gmail.com")
check("gmial.com → gmail.com", main._canon_email("a@gmial.com"), "a@gmail.com")
check("hotmial.com → hotmail.com", main._canon_email("a@hotmial.com"), "a@hotmail.com")
check("נקודות בג'ימייל עדיין מנורמלות", main._canon_email("i.do.cemex@gmail.com"), "idocemex@gmail.com")
check("⛔ דומיין אמיתי לא נגרר", main._canon_email("a@greenmobile.co.il"), "a@greenmobile.co.il")
check("⛔ דומיין רחוק לא נגרר", main._canon_email("a@example.org"), "a@example.org")

print("\n── מרחק עריכה ──")
check("com↔cim", main._edit_distance_le1("gmail.com", "gmail.cim"), True)
check("com↔con", main._edit_distance_le1("gmail.com", "gmail.con"), True)
check("שני תווים שונים", main._edit_distance_le1("gmail.com", "gmail.cin"), False)

print("\n── הטריאז עצמו ──")
main._ip_geo = lambda ip: {"cc": "IL", "isp": "Pelephone", "proxy": False, "hosting": False}
main._proxycheck = lambda ip: {"proxy": False, "type": "ISP", "risk": 0, "operator": ""}
# המפתחות שהטריאז קורא בפועל (grep 'wa\["'): contacted_after, first_in_days,
# known, last_in_days, name_match, wa_name
main._wa_signal = lambda *a, **k: {"known": False, "contacted_after": False,
                                   "wa_name": "", "name_match": None,
                                   "last_in_days": None, "first_in_days": None,
                                   "after_hours": None}

def triage(graph_entries, name, email):
    o = {"id": 1, "number": "999", "total": "300.00",
         "billing": {"first_name": name, "last_name": "", "email": email,
                     "phone": "0503535104"},
         "customer_ip_address": "1.1.1.1",
         "line_items": [{"name": "קוד דיגיטלי Xbox", "quantity": 1, "total": "300.00",
                         "sku": "519707"}],
         "meta_data": []}
    meta = {"payplus_four_digits": "4797", "payplus_brand_name": "visa",
            "payplus_approval_num": "123456"}
    r = main._fraud_triage(o, meta, {"cards": {"4797|visa": graph_entries}, "ips": {}},
                           force=True)
    assert (r.get("signals") or {}).get("card_last4") == "4797", \
        "הכרטיס לא הגיע לטריאז — הבדיקה חסרת משמעות"
    return r

same = [["51644", main._norm_name("עידו חומש"), "idocemex@gmail.com"],
        ["51677", main._norm_name("עידו חומש"), "idocemex@gmail.com"]]
r = triage(same, "עידו חומש", "idocemex@gmail.com")
check("שם אחד + מייל אחד → לא hard_fraud", r.get("hard_fraud"), False)

two_mails = [["51644", main._norm_name("עידו חומש"), "idocemex@gmail.com"],
             ["51677", main._norm_name("עידו חומש"), "idocemex@other.com"]]
r = triage(two_mails, "עידו חומש", "idocemex@gmail.com")
check("שם אחד + 2 מיילים → לא hard_fraud", r.get("hard_fraud"), False)

two_names = [["51644", main._norm_name("עידו חומש"), "idocemex@gmail.com"],
             ["51677", main._norm_name("משה כהן"), "moshe@gmail.com"]]
r = triage(two_names, "עידו חומש", "idocemex@gmail.com")
check("⛔ 2 שמות → hard_fraud נשאר", r.get("hard_fraud"), True)

assert not fails, "נכשלו:\n  " + "\n  ".join(fails)
print("\n✅ הכל עבר")
