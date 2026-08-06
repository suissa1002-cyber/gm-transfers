# inspect_dialog.py — READ ONLY. Dumps the controls of whatever POS window/dialog
# is open right now (employee popup / removal form / item screen). Never clicks/saves.
# Run it once at EACH screen and send me the output file it prints.
#
#   python inspect_dialog.py emp      # at the "בחר שם עובד" popup   -> pos_emp.txt
#   python inspect_dialog.py form     # at the "הורדה מהמלאי" form   -> pos_form.txt
#   python inspect_dialog.py items    # at the item-entry screen     -> pos_items.txt

import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

label = (sys.argv[1] if len(sys.argv) > 1 else "dialog")

try:
    from pywinauto import Desktop
except ImportError:
    print("pywinauto not installed. Run:  pip install pywinauto"); sys.exit(1)


def ctrl_line(c):
    try:
        cid = ""
        try: cid = c.control_id()
        except Exception: pass
        txt = ""
        try: txt = c.window_text()
        except Exception: pass
        rect = ""
        try: rect = c.rectangle()
        except Exception: pass
        return "class=%-26s id=%-6s text=%r rect=%s" % (c.class_name(), cid, txt, rect)
    except Exception as e:
        return "  (ctrl err: %s)" % e


wins = Desktop(backend="win32").windows()

# every visible ThunderRT6 window (the POS + its dialogs are all ThunderRT6*),
# plus the current foreground dialog whatever it is.
print("==== VISIBLE WINDOWS (%s) ====" % label)
targets = []
for w in wins:
    try:
        t = w.window_text(); cn = w.class_name()
        # ⚠️ אין לדלג על חלונות ללא כותרת! פופאפ "בחר שם עובד" הוא בדיוק כזה,
        # והדילוג עליו הסתיר אותו מהמיפוי (27/07).
        vis = "ThunderRT6" in cn or any(k in (t or "") for k in ("אורדר", "מלאי", "עובד", "פעולה"))
        print(("  * " if vis else "    ") + "[%s] %r" % (cn, t))
        if vis:
            targets.append(w)
    except Exception:
        pass

for w in targets:
    try:
        print("\n==== CONTROLS: %r ====" % w.window_text())
        for c in w.descendants():
            print(ctrl_line(c))
    except Exception as e:
        print("  dump failed: %s" % e)

print("\nDONE (%s). Send me this output." % label)
