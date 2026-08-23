"""⛔ מחירון תיקונים — אסור שהתאמה תחצה מותגים.

⚠️ אלה דיווחה (19/08/2026): q="iphone 14 plus מסך" החזיר
"note 14 pro plus 5g" של שיאומי. iphone 14 plus אינו במחירון, ההתאמה
המלאה נכשלה, הנפילה הסירה את "iphone" וחיפשה ["14","plus"] — ונחתה על
דגם שיאומי. לקוח אייפון קיבל מחיר של מכשיר אחר לגמרי.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main  # noqa: E402


def run():
    passed, failed = 0, []

    def check(name, cond):
        nonlocal passed
        if cond: passed += 1
        else: failed.append(name)

    fam = main._repair_family
    check("iphone → apple", fam(["iphone", "14", "plus"]) == "apple")
    check("ipad → apple", fam(["ipad", "air"]) == "apple")
    check("note → xiaomi", fam(["note", "14", "pro"]) == "xiaomi")
    check("redmi → xiaomi", fam(["redmi", "12"]) == "xiaomi")
    check("galaxy → samsung", fam(["galaxy", "s24"]) == "samsung")
    check("⛔ בלי מותג → ריק (לא חוסם)", fam(["14", "plus"]) == "")

    # ⛔ הליבה: משפחות שונות לעולם לא מתאימות זו לזו
    check("⛔ apple ≠ xiaomi", fam(["iphone", "14"]) != fam(["note", "14"]))
    check("⛔ apple ≠ samsung", fam(["iphone", "14"]) != fam(["galaxy", "s24"]))
    check("⛔ samsung ≠ xiaomi", fam(["galaxy"]) != fam(["redmi"]))

    # הסינון עצמו: מפתח משיאומי נפסל לשאילתת אייפון, מפתח בלי מותג עובר
    q_fam = fam(["iphone", "14", "plus"])
    for key, want in (("note 14 pro plus 5g", False),
                      ("galaxy s24 plus", False),
                      ("iphone 14 pro", True),
                      ("14 plus", True)):          # בלי מותג — לא חוסמים
        kf = fam(key.split())
        ok = kf in (q_fam, "")
        check(f"{'עובר' if want else '⛔ נפסל'}: {key}", ok is want)

    # ⚠️ הגיליון לא עקבי: "iphone 14 +" מול "iphone 16 plus" מול "iphone 6+".
    # בלי נרמול, iphone 14 plus (שקיים!) לא נמצא — ואסי תיקן אותי על כך.
    np = main._norm_plus
    check("'iphone 14 +' מתנרמל", np("iphone 14 +") == "iphone 14 plus")
    check("'iphone 6+' מתנרמל", np("iphone 6+") == "iphone 6 plus")
    check("'iphone 16 plus' ללא שינוי", np("iphone 16 plus") == "iphone 16 plus")
    check("⛔ שאילתה ומפתח מתלכדים", np("iphone 14 plus") == np("iphone 14 +"))
    check("⛔ שיאומי לא מתלכד עם אפל",
          np("note 14 pro plus 5g") != np("iphone 14 +"))
    check("ריק בטוח", np("") == "" and np(None) == "")

    print(f"עברו {passed}/{passed + len(failed)}")
    for f in failed: print("  ⛔", f)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
