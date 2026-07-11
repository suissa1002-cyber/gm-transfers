/* Green Mobile — new checkout behaviour (gated ?gmnew).
   1) Relocates address fields into step-2 (JS-relocation pattern, form intact).
   2) Decorates shipping options: icon + clean title/sub + price (mockup look).
   3) Moves the PayPlus embedded iframe into the side column card (mockup slot).
   Re-applies decoration after every WooCommerce updated_checkout fragment swap. */
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

  /* ---------- 1: address fields into step 2 ---------- */
  (function relocateAddress() {
    var addrSel = ['#billing_company_field', '#billing_country_field', '#billing_address_1_field',
      '#billing_address_2_field', '#billing_postcode_field', '#billing_city_field', '#billing_state_field'].join(',');
    var $addr = $(addrSel);
    if ($addr.length && !$('.gm-addr-block').length) {
      var $block = $('<div class="gm-addr-block"><div class="gm-addr-title">כתובת למשלוח</div><div class="gm-addr-fields"></div></div>');
      $ship.append($block);
      $block.find('.gm-addr-fields').append($addr);
    }
    var $shipFields = $('.woocommerce-shipping-fields');
    if ($shipFields.length) $ship.append($shipFields);
    var $notes = $('.woocommerce-additional-fields');
    if ($notes.length) $ship.append($notes);

    $('#billing_first_name').attr('placeholder', 'ישראל');
    $('#billing_last_name').attr('placeholder', 'ישראלי');
    $('#billing_phone').attr('placeholder', '050-0000000');
    $('#billing_email').attr('placeholder', 'name@example.com');
    $('#billing_address_1').attr('placeholder', 'רחוב ומספר');
    $('#billing_address_2').attr('placeholder', 'דירה / כניסה (אופציונלי)');
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
    /* summary: "חינם!" -> "חינם" */
    $('.gm-shipping-total td').each(function () {
      var h = $(this).html();
      if (h && h.indexOf('חינם!') !== -1) $(this).html(h.replace(/חינם!/g, 'חינם'));
    });
  }
  decorateShipping();
  $(document.body).on('updated_checkout', decorateShipping);

  /* ---------- 3: PayPlus embedded iframe -> side-column slot ---------- */
  function movePP() {
    var $frame = $('#pp_iframe');
    if (!$frame.length) return;
    var $slot = $('#gm-pp-slot');
    if (!$slot.length || $.contains($slot[0], $frame[0])) return;
    var $wrap = $frame.closest('.pp_iframe');
    $slot.find('.gm-pp-body').append($wrap.length ? $wrap : $frame);
    $slot.prop('hidden', false).addClass('show');
    $slot[0].scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
  movePP();
  new MutationObserver(movePP).observe(document.body, { childList: true, subtree: true });
});
