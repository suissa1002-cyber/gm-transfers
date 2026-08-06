# סוכן הורדה מהמלאי — מכונת Windows

הסוכן מושך פעולות הורדה שנקלטו ב-GreenOS (`/removals`) ומזין אותן לקופת
NewOrder/Morning, עם אימות מלאי מובנה. רץ על מכונת ה-Windows שהקופה פתוחה בה.

## התקנה (פעם אחת)
```
pip install -r requirements.txt
```

## הרצה
```
python agent.py
```
בהרצה ראשונה יבקש מפתח ניהול GreenOS — נשמר מקומית ל-`agent_key.txt`.

## שליטה (מ-GreenOS, לא מהמכונה)
- **enabled** — kill-switch. כברירת מחדל **מושבת** → הסוכן לא נוגע בקופה.
- **dry_run** — כברירת מחדל **פעיל** → הסוכן ממלא הכל אבל **מבטל במקום לשמור**
  (מצלם מסך ל-`screenshots/`). כלום לא יורד מהמלאי.
- מפעילים דרך: `POST /api/admin/pos/agent-config {"enabled":true,"dry_run":false}`.

## בטיחות
- אחרי כל הורדה חיה הסוכן קורא את המלאי ומוודא שירד בדיוק בכמות שהוזנה;
  אחרת = error והוא **לא ממשיך**.
- `claim` אטומי → אין הזנה כפולה. `agent.log` + צילום מסך לכל פעולה.

## מצב נוכחי
מפת ה-UI של מסך "הורדה מהמלאי" עדיין לא הוגדרה (`UI_MAP_READY=False` ב-pos_driver.py).
להשלמתה: הרץ `inspect_dialog.py` על שלושת המסכים ושלח את הפלט:
```
# פתח בקופה: מלאי -> הורדה מהמלאי -> ובכל מסך הרץ:
python inspect_dialog.py emp    > pos_emp.txt     # במסך "בחר שם עובד"
python inspect_dialog.py form   > pos_form.txt    # בטופס "פעולה חדשה - הורדה מהמלאי"
python inspect_dialog.py items  > pos_items.txt   # במסך הזנת הפריטים
```
