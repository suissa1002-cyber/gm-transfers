# מעבר ל-WhatsApp עצמאי — ניתוק קונקטופ (תוכנית + מעקב)

**מטרה:** להפעיל את ה-WhatsApp שלנו ישירות מול **WhatsApp Cloud API של מטא**, בלי
קונקטופ כשכבת ביניים — כולל אוטומציות, קטלוג דינמי בצ'אט, ואורי (Claude) כמנוע השיחה.

**עיקרון על:** בונים הכל מוכן ובדוק **לצד** קונקטופ. ההפניה אלינו
(`override_callback_uri` ב-Meta) היא הצעד האחרון. בזמן ה-cutover השרת שלנו **מעביר
עותק לקונקטופ**, כך שאפילו אז שום דבר לא נשבר.

## עובדות שאומתו (13/06/2026, דרך Graph API)
- **ה-WABA שלנו** (Green Mobile, business 707734200838433, type SELF/APPROVED).
- **המספר** +972-50-296-6671 — שלנו, **CLOUD_API**, איכות **GREEN**.
- **אפליקציית מטא** 1746640449184847 — שלנו.
- ה-webhook מופנה כרגע ל-`newapp.connectop.co.il/php/whatsapp.php` (override שאנחנו שולטים בו).
- ⚠️ `business_verification_status: rejected`, `name_status: DECLINED` — לא חוסם, לתקן בנפרד (תקרות שליחה).
- **משמעות:** אין צורך בהעברת מספר. ניתוק = הפניית webhook אלינו. הפיך.

## שלבים

### שלב 0 — הכנות (אסי, ב-Render env)
- [ ] `META_WEBHOOK_VERIFY_TOKEN` — מחרוזת שאנחנו ממציאים (לאימות GET של מטא).
- [ ] `META_APP_SECRET` — מ-Meta App (Settings → Basic) — לאימות חתימת ה-POST.
- [ ] (קיים) `META_WA_TOKEN`, `META_WA_PHONE_ID`.

### שלב 1 — קבלה ✅ נבנה (13/06/2026)
- [x] טבלאות: `wa_msg` (הודעות נכנס/יוצא + מדיה + סטטוס), `wa_contact` (חלון 24ש + דגלים).
- [x] `wa_webhook.py` — אימות חתימה, פענוח (טקסט/מדיה/אינטראקטיבי/סטטוס/אנשי-קשר), שמירה.
- [x] endpoints: `GET/POST /api/wa/webhook` (verify + receive) · `/api/admin/wa/store-stats`.
- [x] העברה לקונקטופ בזמן מעבר (`WA_FORWARD_CONNECTOP=1`).
- רדום עד ההפניה (שלב 5). נבדק מקומית מקצה-לקצה (parse→store→read).

### שלב 2 — מדיה
- [ ] מדיה נכנסת: `media_id` → הורדה מ-Graph (`GET /{id}` → URL מאומת → bytes) → אחסון → הגשה ב-`/api/wa/media/{id}`.
- [ ] שליחת מדיה החוצה דרך מטא.

### שלב 3 — קריאה (ה-swap הגדול)
- [ ] `wa.get_thread` / `list_conversations` / `contact_card` → לקרוא מ-`wa_msg`/`wa_contact` (במקום מהדשבורד של קונקטופ). מיזוג `wa_shadow` (שליחות ישירות) לחנות המאוחדת.
- [ ] toggle env לגיבוי (ConnectOp ↔ עצמאי).

### שלב 4 — שליחה
- [ ] טקסט חופשי דרך מטא ישיר (במקום `connectop_client`); כל הודעה יוצאת נשמרת ב-`wa_msg`.
- [ ] מעקב סטטוסים (sent/delivered/read/failed) מה-webhook.

### שלב 5 — Cutover
- [ ] להפנות `override_callback_uri` אלינו (`https://gm-transfers.onrender.com/api/wa/webhook`).
- [ ] לאמת קבלה חיה (store-stats עולה). העברה לקונקטופ נשארת דולקת כמה ימים.
- [ ] לכבות העברה (`WA_FORWARD_CONNECTOP=0`) + לנתק קונקטופ.

### שלב 6 — העתיד (אחרי cutover)
- [ ] הודעות אינטראקטיביות (רשימות/כפתורים).
- [ ] קטלוג WooCommerce דינמי בצ'אט.
- [ ] אורי כמנוע תשובות (אוטומטי עם אישור אנושי) · הזמנה תוך כדי צ'אט.
