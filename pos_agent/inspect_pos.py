# inspect_pos.py - READ ONLY. Does not click or change anything.
# Finds the NewOrder / Morning POS window and dumps its controls.
# Run it while the POS is open on screen. Send me the file pos_tree.txt it creates.

import sys, io

# make output UTF-8 so Hebrew control names are readable
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

try:
    from pywinauto import Desktop
except ImportError:
    print("pywinauto is not installed. Run:  pip install pywinauto")
    sys.exit(1)

# window titles that identify the POS (ASCII 'Morning' is the safe anchor)
NEEDLES = ["morning", "אורדר", "קופה", "neworder"]


def looks_like_pos(title):
    t = (title or "").lower()
    return any(n.lower() in t for n in NEEDLES)


found_any = False
for backend in ("uia", "win32"):
    print("\n" + "=" * 72)
    print("BACKEND =", backend)
    print("=" * 72)
    try:
        wins = Desktop(backend=backend).windows()
    except Exception as e:
        print("  could not open Desktop(%s): %s" % (backend, e))
        continue

    # 1) list every visible top-level window (so we see the real title)
    print("\n-- all visible windows --")
    for w in wins:
        try:
            t = w.window_text()
            if t and t.strip():
                print("  [%s] %r" % (w.class_name(), t))
        except Exception:
            pass

    # 2) dump the full control tree of the POS window
    for w in wins:
        try:
            if looks_like_pos(w.window_text()):
                found_any = True
                print("\n-- CONTROL TREE of POS window (%s) --" % backend)
                print("   title: %r" % w.window_text())
                w.print_control_identifiers(depth=6)
                break
        except Exception as e:
            print("  dump failed: %s" % e)

if not found_any:
    print("\n\n!!! POS window was not auto-detected.")
    print("Look at the '-- all visible windows --' list above, find the POS line,")
    print("and send it to me so I can adjust the search.")

print("\nDONE. Send me the file 'pos_tree.txt'.")
