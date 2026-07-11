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

  /* the legacy branch-pickup popup must not run on the new checkout */
  window.gmPickupInitialized = true;

  function setLabelText($field, text) {
    var $l = $field.find('label').first();
    if (!$l.length) return;
    $l.contents().filter(function () { return this.nodeType === 3; }).first().replaceWith(text + ' ');
  }

  /* ---------- 1: address fields into step 2, mockup order + labels ---------- */
  (function relocateAddress() {
    /* company stays in step 1 (invoice-name note); only address fields move */
    var $addr = $('#billing_country_field,#billing_address_1_field,#billing_address_2_field,#billing_postcode_field,#billing_city_field,#billing_state_field');
    if ($addr.length && !$('.gm-addr-block').length) {
      var $block = $('<div class="gm-addr-block"><div class="gm-addr-title">כתובת למשלוח</div><div class="gm-addr-fields"></div></div>');
      $ship.append($block);
      var $w = $block.find('.gm-addr-fields');
      /* mockup order: עיר|מיקוד → רחוב|דירה → (מדינה — מוסתרת, בסוף) */
      $w.append($('#billing_city_field'), $('#billing_postcode_field'),
                $('#billing_address_1_field'), $('#billing_address_2_field'),
                $('#billing_country_field'), $('#billing_state_field'));
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
    $('#billing_company').attr('placeholder', 'הזן שם חברה במידה וצריך חשבונית על שם החברה');
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
      /* icon in its own flex column — the subtitle aligns exactly under the title's first letter */
      $label.html('<span class="gm-ship-row">' + conf.icon
        + '<span class="gm-ship-txt"><b class="gm-ship-t">' + conf.t + '</b>'
        + '<span class="gm-ship-s">' + conf.s + '</span></span></span>'
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

  /* ---------- our own pay button in the summary (proxy) ----------
     The real #place_order stays where WooCommerce/PayPlus manage it (hidden by
     CSS) — no more fighting over its text/ownership. Our proxy triggers it. */
  function ensureProxy() {
    if (!$('#gm-po-slot').length) {
      $('.gm-summary').append('<div id="gm-po-slot"><button type="button" id="gm-pay-proxy" class="gm-pay-btn">אישור ותשלום</button></div>');
    }
    /* a second pay button at the END of the flow (mobile) — the user finishes
       filling at the bottom and pays right there, no scrolling back up */
    if (!$('#gm-pay-proxy2').length) {
      $('#gm-step-payment').append('<button type="button" id="gm-pay-proxy2" class="gm-pay-btn">אישור ותשלום</button>');
    }
  }
  $(document).on('click', '#gm-pay-proxy, #gm-pay-proxy2', function () {
    var $b = $('form.checkout #place_order');
    if ($b.length) $b.first().trigger('click');
    else $('form.checkout').trigger('submit');
  });

  /* summary: "× 1" -> "כמות: 1" */
  function fixQty() {
    $('.woocommerce-checkout-review-order-table .product-quantity').each(function () {
      var n = (this.textContent.match(/\d+/) || ['1'])[0];
      if (this.textContent.indexOf('כמות') === -1) this.textContent = 'כמות: ' + n;
    });
  }

  /* PayPlus side card: hidden until the REAL PayPlus form loads (after אישור
     ותשלום). The PayPlus page itself is branded via their dashboard editor.
     Hides if the customer switches to Blender. */
  function ppSlotState() {
    var $c = $('input[name="payment_method"]:checked');
    var sig = ($c.val() || '') + ' ' + ($c.attr('id') || '') + ' ' + ($c.closest('li').attr('class') || '');
    var $slot = $('#gm-pp-slot');
    if (!$slot.length) return;
    if (/blender/.test(sig)) { $slot.prop('hidden', true).removeClass('show'); return; }
    if ($('#pp_iframe').length) $slot.prop('hidden', false).addClass('show');
  }
  $(document.body).on('change', 'input[name="payment_method"]', ppSlotState);

  /* ---------- pickup: inline branch selector (mockup) — replaces the legacy popup ---------- */
  var PICKUP_VAL = 'local_pickup:2';
  var GM_BRANCHES = [
    { id: 'gan-hair', name: 'סניף גן העיר', disp: 'גן העיר — אשדוד',   address: 'הגדוד העברי 5, אשדוד' },
    { id: 'star',     name: 'סניף סטאר',    disp: 'סטאר סנטר — אשדוד', address: "ז'בוטינסקי 45, אשדוד" },
    { id: 'city',     name: 'סניף סיטי',    disp: 'סיטי — אשדוד',      address: 'הציונות 13, אשדוד' },
    { id: 'ad-halom', name: 'סניף עד הלום', disp: 'עד הלום — אשדוד',   address: 'צומת עד הלום, אשדוד' }
  ];
  function gmSaveBranch(b) {
    /* same three channels the legacy popup used — the order keeps receiving the branch */
    var full = b.name + ' - ' + b.address;
    var $f = $('#gm_pickup_branch');
    if (!$f.length) $f = $('<input type="hidden" id="gm_pickup_branch" name="gm_pickup_branch">').appendTo('form.checkout');
    $f.val(full);
    try { sessionStorage.setItem('gm_selected_branch', JSON.stringify(b)); } catch (e) {}
    var url = (typeof gmAjaxUrl !== 'undefined' && gmAjaxUrl) ? gmAjaxUrl : '/wp-admin/admin-ajax.php';
    var body = 'action=gm_save_pickup_branch&branch=' + encodeURIComponent(full);
    if (typeof gmPickupNonce !== 'undefined' && gmPickupNonce) body += '&nonce=' + encodeURIComponent(gmPickupNonce);
    try {
      fetch(url, { method: 'POST', credentials: 'same-origin',
                   headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: body });
    } catch (e) {}
  }
  function gmBranchById(id) {
    for (var i = 0; i < GM_BRANCHES.length; i++) if (GM_BRANCHES[i].id === id) return GM_BRANCHES[i];
    return GM_BRANCHES[0];
  }
  function pickupUI() {
    var isPickup = $('input.shipping_method[value="' + PICKUP_VAL + '"]').is(':checked');
    /* address fields are delivery-only; pickup shows the branch selector instead */
    $('.gm-addr-block, .woocommerce-shipping-fields, .woocommerce-additional-fields').toggle(!isPickup);
    document.body.style.overflow = '';   /* the legacy popup locks scroll when it opens — always release */
    var $wrap = $('#gm-branch-wrap');
    if (!isPickup) { $wrap.remove(); return; }
    if (!$wrap.length) {
      var saved = null;
      try { saved = JSON.parse(sessionStorage.getItem('gm_selected_branch') || 'null'); } catch (e) {}
      var savedId = saved && saved.id;
      var opts = GM_BRANCHES.map(function (b) {
        return '<option value="' + b.id + '"' + (b.id === savedId ? ' selected' : '') + '>' + b.disp + '</option>';
      }).join('');
      $wrap = $('<div id="gm-branch-wrap"><div class="gm-addr-title">בחירת סניף לאיסוף</div>'
        + '<select id="gm-branch-pick">' + opts + '</select>'
        + '<div class="gm-branch-note">נעדכן אותך כשההזמנה מוכנה לאיסוף · זמן הכנה משוער: מספר שעות</div></div>');
      $('#gm-shipping-list').after($wrap);
      $wrap.on('change', '#gm-branch-pick', function () { gmSaveBranch(gmBranchById(this.value)); });
      gmSaveBranch(gmBranchById($wrap.find('#gm-branch-pick').val()));
    }
  }

  function decorateCC() {
    /* card-brand icons up to the title row; the box's bottom text becomes line 2 */
    var $cc = $('li.payment_method_payplus-payment-gateway');
    if (!$cc.length) return;
    var $b = $cc.find('.gm-pm-txt b').first();
    if (!$b.length || $b.find('.gm-cc-brands').length) return;
    var $imgs = $cc.find('.payment_box .payplus-checkout-image');
    if ($imgs.length) {
      var $br = $('<span class="gm-cc-brands"></span>');
      $imgs.each(function () { $br.append($(this).clone().removeAttr('style')); });
      $b.append($br);
    }
    $cc.find('.gm-pm-txt > span').first().text('שלם בצורה מאובטחת עם פיי פלוס · עד 12 תשלומים');
  }
  function fixTotalLabel() {
    /* mockup wording: the grand-total row reads "לתשלום" */
    $('.gm-summary tr.order-total th').each(function () {
      var n = this.firstChild;
      if (n && n.nodeType === 3 && n.nodeValue.indexOf('לתשלום') === -1) n.nodeValue = 'לתשלום ';
    });
  }
  function decorateAll() { decorateShipping(); decoratePayment(); decorateCC(); ensureProxy(); fixQty(); fixTotalLabel(); ppSlotState(); pickupUI(); }
  decorateAll();
  $(document.body).on('updated_checkout', function () { decorateAll(); setTimeout(decorateAll, 80); });

  /* the whole shipping card selects (radios are hidden) */
  $(document).on('click', '#gm-shipping-list ul#shipping_method li', function () {
    var $inp = $(this).find('input.shipping_method');
    if ($inp.length && !$inp.prop('checked')) {
      $inp.prop('checked', true).trigger('change');
    }
  });

  /* ---------- 4: PayPlus embedded iframe -> side-column slot ---------- */
  function movePP() {
    var $frame = $('#pp_iframe');
    if (!$frame.length) return;
    var $slot = $('#gm-pp-slot');
    if (!$slot.length || $.contains($slot[0], $frame[0])) return;
    var $wrap = $frame.closest('.pp_iframe');
    $slot.find('.gm-pp-wait,.gm-pp-skel').remove();
    $slot.find('.gm-pp-body').append($wrap.length ? $wrap : $frame);
    /* grant Payment Request permission inside the cross-origin iframe —
       without it Google Pay/Apple Pay fall back to opening a new tab */
    if (!$frame.data('gmAllow')) {
      $frame.attr('allow', 'payment');
      $frame.attr('allowpaymentrequest', '');
      $frame.data('gmAllow', 1);
      var src = $frame.attr('src');
      if (src) $frame.attr('src', src);   /* reload once so the permission applies */
    }
    $slot.prop('hidden', false).addClass('show');
    $slot[0].scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
  movePP();
  new MutationObserver(movePP).observe(document.body, { childList: true, subtree: true });

  /* tapping the wallets zone (upper part of the PayPlus iframe) reveals fields at
     the FORM'S BOTTOM (e.g. Apple Pay installments) — auto-scroll there so the
     user sees that something happened. Clicks lower (card fields) don't scroll. */
  var gmLastPt = { x: 0, y: 0 };
  window.addEventListener('touchstart', function (e) {
    if (e.touches && e.touches[0]) gmLastPt = { x: e.touches[0].clientX, y: e.touches[0].clientY };
  }, true);
  window.addEventListener('mousedown', function (e) { gmLastPt = { x: e.clientX, y: e.clientY }; }, true);
  window.addEventListener('blur', function () {
    var f = document.getElementById('pp_iframe');
    if (!f || document.activeElement !== f) return;
    var r = f.getBoundingClientRect();
    var walletZone = Math.min(420, r.height * 0.45);
    if (gmLastPt.y >= r.top && gmLastPt.y <= r.top + walletZone) {
      setTimeout(function () { f.scrollIntoView({ behavior: 'smooth', block: 'end' }); }, 350);
    }
  });
});
