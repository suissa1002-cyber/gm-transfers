/* Green Mobile — new checkout behaviour (gated ?gmnew).
   1) Relocates address fields into step-2 + reorders/labels them like the mockup.
   2) Decorates shipping options: icon + clean title/sub + price.
   3) Decorates payment methods: clean title/sub + logos aligned left.
   4) Moves the PayPlus embedded iframe into the side-column card.
   Re-applies after every WooCommerce updated_checkout fragment swap. */
jQuery(function ($) {
  var $ship = $('#gm-step-shipping');
  if (!$ship.length) return;

  /* ---------- icons (line set) ---------- */
  function ic(paths) {
    return '<svg class="gm-ship-ic" viewBox="0 0 24 24" aria-hidden="true">' + paths + '</svg>';
  }
  var ICONS = {
    truck: ic('<path d="M2.5 6h11v10h-11z"/><path d="M13.5 9.5h4l3 3.5v3h-7z"/><circle cx="6.5" cy="17.5" r="1.8"/><circle cx="16.5" cy="17.5" r="1.8"/>'),
    bolt:  ic('<path d="M13 2 5 13h5.5L9.5 22 19 10.5h-6z"/>'),
    pin:   ic('<path d="M12 21s-6.5-5.4-6.5-10a6.5 6.5 0 0 1 13 0C18.5 15.6 12 21 12 21z"/><circle cx="12" cy="10.5" r="2.4"/>'),
    store: ic('<path d="M4 9.5 5.5 4h13L20 9.5M4 9.5h16M4 9.5v10.5h16V9.5M4 9.5a2.5 2.5 0 0 0 5 0 2.5 2.5 0 0 0 5 0 2.5 2.5 0 0 0 5 0"/>')
  };

  function setLabelText($field, text) {
    var $l = $field.find('label').first();
    if (!$l.length) return;
    $l.contents().filter(function () { return this.nodeType === 3; }).first().replaceWith(text + ' ');
  }

  /* ---------- 1: address fields into step 2, mockup order + labels ---------- */
  (function relocateAddress() {
    var $addr = $('#billing_company_field,#billing_country_field,#billing_address_1_field,#billing_address_2_field,#billing_postcode_field,#billing_city_field,#billing_state_field');
    if ($addr.length && !$('.gm-addr-block').length) {
      var $block = $('<div class="gm-addr-block"><div class="gm-addr-title">כתובת למשלוח</div><div class="gm-addr-fields"></div></div>');
      $ship.append($block);
      var $w = $block.find('.gm-addr-fields');
      /* mockup order: עיר|מיקוד → רחוב|דירה → (מדינה, חברה — רוחב מלא בסוף) */
      $w.append($('#billing_city_field'), $('#billing_postcode_field'),
                $('#billing_address_1_field'), $('#billing_address_2_field'),
                $('#billing_country_field'), $('#billing_company_field'), $('#billing_state_field'));
    }
    var $shipFields = $('.woocommerce-shipping-fields');
    if ($shipFields.length) $ship.append($shipFields);
    var $notes = $('.woocommerce-additional-fields');
    if ($notes.length) $ship.append($notes);

    /* labels like the mockup */
    setLabelText($('#billing_phone_field'), 'טלפון נייד');
    setLabelText($('#billing_email_field'), 'דוא"ל');
    setLabelText($('#billing_postcode_field'), 'מיקוד');
    setLabelText($('#billing_address_1_field'), 'רחוב ומספר');
    var $a2l = $('#billing_address_2_field label');
    $a2l.removeClass('screen-reader-text').text('דירה / כניסה');
    $('#order_comments_field label').text('הערות לשליח (אופציונלי)');

    /* placeholders */
    $('#billing_first_name').attr('placeholder', 'ישראל');
    $('#billing_last_name').attr('placeholder', 'ישראלי');
    $('#billing_phone').attr('placeholder', '050-0000000');
    $('#billing_email').attr('placeholder', 'name@example.com');
    $('#billing_address_1').attr('placeholder', 'הרצל 25');
    $('#billing_address_2').attr('placeholder', 'דירה 4');
    $('#order_comments').attr('placeholder', 'קומה, קוד כניסה, שעות נוחות...');
  })();

  /* ---------- 2: shipping options decorated like the mockup ---------- */
  function decorateShipping() {
    $('#gm-shipping-list ul#shipping_method li').each(function () {
      var $li = $(this), $label = $li.find('label').first();
      if (!$label.length || $li.data('gmDecorated')) return;
      var raw = $label.text();
      var conf = null;
      if (/באותו היום/.test(raw))       conf = { icon: ICONS.bolt,  t: 'משלוח באותו היום',    s: 'בהזמנה עד 13:00 · א׳–ה׳ · ב״ש–חיפה' };
      else if (/נקודת מסירה/.test(raw)) conf = { icon: ICONS.pin,   t: 'נקודת מסירה תל אביב', s: 'מסירה למחרת · י.ל פרץ 35 · א׳–ה׳ 10:00–16:00' };
      else if (/איסוף עצמי/.test(raw))  conf = { icon: ICONS.store, t: 'איסוף עצמי מהסניף',   s: 'ניצור קשר לתיאום ובחירת סניף' };
      else if (/1[-–]6/.test(raw))      conf = { icon: ICONS.truck, t: 'משלוח שליח עד הבית',  s: '1–6 ימי עסקים' };
      if (!conf) return;
      var $price = $label.find('.woocommerce-Price-amount').first();
      var priceHtml = $price.length ? ('<span class="gm-ship-price">' + $price.prop('outerHTML') + '</span>')
                                    : '<span class="gm-ship-price free">חינם</span>';
      $label.html(conf.icon
        + '<span class="gm-ship-txt"><b>' + conf.t + '</b><span>' + conf.s + '</span></span>'
        + priceHtml);
      $li.data('gmDecorated', true);
    });
    $('.gm-shipping-total td').each(function () {
      var h = $(this).html();
      if (h && h.indexOf('חינם!') !== -1) $(this).html(h.replace(/חינם!/g, 'חינם'));
    });
  }

  /* ---------- 3: payment methods decorated like the mockup ---------- */
  var PM = {
    'payment_method_payplus-payment-gateway':           { t: 'כרטיס אשראי', s: 'Visa / Mastercard · עד 12 תשלומים' },
    'payment_method_payplus-payment-gateway-googlepay': { t: 'Google Pay',  s: '' },
    'payment_method_payplus-payment-gateway-applepay':  { t: 'Apple Pay',   s: '' },
    'payment_method_payplus-payment-gateway-bit':       { t: 'bit',         s: 'תשלום מהיר מהאפליקציה' },
    'payment_method_blender':                           { t: 'Blender · תשלומים', s: 'פריסה נוחה לתשלומים' }
  };
  function decoratePayment() {
    $('ul.wc_payment_methods > li.wc_payment_method').each(function () {
      var $li = $(this);
      if ($li.data('gmP')) return;
      var m = this.className.match(/payment_method_[\w-]+/);
      var conf = m && PM[m[0]];
      if (!conf) return;
      var $label = $li.children('label').first();
      if (!$label.length) return;
      var $imgs = $label.find('img').detach();
      $label.html('<span class="gm-pm-txt"><b>' + conf.t + '</b>' + (conf.s ? '<span>' + conf.s + '</span>' : '') + '</span>');
      /* logos sit right next to the radio (label start), like the mockup */
      if ($imgs.length) $label.prepend($('<span class="gm-pm-logos"></span>').append($imgs));
      $li.data('gmP', 1);
    });
  }

  /* ---------- place-order button lives in the summary card (mockup) ----------
     WooCommerce re-renders #payment (incl. a fresh #place_order) on every
     updated_checkout — keep exactly ONE button: drop any stale copy in the slot
     and adopt the freshest one. */
  function movePlaceOrder() {
    if (!$('#gm-po-slot').length) $('.gm-summary').append('<div id="gm-po-slot"></div>');
    var $slot = $('#gm-po-slot');
    var $btns = $('#place_order');
    if (!$btns.length) return;
    var $fresh = $btns.filter(function () { return !$.contains($slot[0], this); }).last();
    if ($fresh.length) {
      $slot.empty().append($fresh);
      $fresh.text('אישור ותשלום');
    }
  }

  /* summary: "× 1" -> "כמות: 1" */
  function fixQty() {
    $('.woocommerce-checkout-review-order-table .product-quantity').each(function () {
      var n = (this.textContent.match(/\d+/) || ['1'])[0];
      if (this.textContent.indexOf('כמות') === -1) this.textContent = 'כמות: ' + n;
    });
  }

  /* PayPlus side card follows the chosen method: placeholder for PayPlus methods, hidden for Blender */
  function ppSlotState() {
    var $c = $('input[name="payment_method"]:checked');
    var sig = ($c.val() || '') + ' ' + ($c.attr('id') || '') + ' ' + ($c.closest('li').attr('class') || '');
    var $slot = $('#gm-pp-slot');
    if (!$slot.length) return;
    if (/blender/.test(sig)) { $slot.prop('hidden', true).removeClass('show'); return; }
    if (!$('#pp_iframe').length && !$slot.find('.gm-pp-skel').length) {
      $slot.find('.gm-pp-body').html(
        '<div class="gm-pp-skel">'
        + '<div class="sk-title">פרטי תשלום</div>'
        + '<div class="sk-wallets"><div class="sk-w apple">Pay</div><div class="sk-w google">G Pay</div><div class="sk-w bit">bit</div></div>'
        + '<div class="sk-or">או בכרטיס אשראי</div>'
        + '<div class="sk-lab">מספר כרטיס</div><div class="sk-field">0000 0000 0000 0000</div>'
        + '<div class="sk-row"><div><div class="sk-lab">תוקף</div><div class="sk-field">חודש / שנה</div></div><div><div class="sk-lab">CVV</div><div class="sk-field">3 ספרות</div></div></div>'
        + '<div class="sk-row"><div><div class="sk-lab">שם בעל/ת הכרטיס</div><div class="sk-field">כפי שמופיע בכרטיס</div></div><div><div class="sk-lab">מספר תשלומים</div><div class="sk-field">תשלום אחד</div></div></div>'
        + '<div class="sk-note">הטופס יופעל לאחר לחיצה על "אישור ותשלום" — ההזנה מתבצעת מול PayPlus בלבד</div>'
        + '</div>');
    }
    $slot.prop('hidden', false).addClass('show');
  }
  $(document.body).on('change', 'input[name="payment_method"]', ppSlotState);

  function decorateAll() { decorateShipping(); decoratePayment(); movePlaceOrder(); fixQty(); ppSlotState(); }
  decorateAll();
  $(document.body).on('updated_checkout', function () { decorateAll(); setTimeout(decorateAll, 80); });

  /* WooCommerce re-writes the button text from the gateway's order_button_text
     after our handler — enforce ours whenever it changes */
  (function enforceBtnText() {
    var slot = document.getElementById('gm-po-slot');
    if (!slot) return;
    new MutationObserver(function () {
      var b = document.getElementById('place_order');
      if (b && b.textContent.trim() !== 'אישור ותשלום') b.textContent = 'אישור ותשלום';
    }).observe(slot, { childList: true, subtree: true, characterData: true });
  })();

  /* ---------- 4: PayPlus embedded iframe -> side-column slot ---------- */
  function movePP() {
    var $frame = $('#pp_iframe');
    if (!$frame.length) return;
    var $slot = $('#gm-pp-slot');
    if (!$slot.length || $.contains($slot[0], $frame[0])) return;
    var $wrap = $frame.closest('.pp_iframe');
    $slot.find('.gm-pp-wait,.gm-pp-skel').remove();
    $slot.find('.gm-pp-body').append($wrap.length ? $wrap : $frame);
    $slot.prop('hidden', false).addClass('show');
    $slot[0].scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
  movePP();
  new MutationObserver(movePP).observe(document.body, { childList: true, subtree: true });
});
