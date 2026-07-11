/* Green Mobile — new checkout behaviour (gated ?gmnew).
   Relocates the address fields into step-2 (delivery) so step-1 is personal
   details only — same JS-relocation pattern as the product-page widgets.
   Fields stay inside form.checkout, so WooCommerce validation/AJAX are untouched. */
jQuery(function ($) {
  var $ship = $('#gm-step-shipping');
  if (!$ship.length) return;

  /* ---- step 2: address block under the delivery options ---- */
  var addrSel = [
    '#billing_company_field',
    '#billing_country_field',
    '#billing_address_1_field',
    '#billing_address_2_field',
    '#billing_postcode_field',
    '#billing_city_field',
    '#billing_state_field'
  ].join(',');
  var $addr = $(addrSel);
  if ($addr.length) {
    var $block = $('<div class="gm-addr-block"><div class="gm-addr-title">כתובת למשלוח</div><div class="gm-addr-fields woocommerce-billing-fields__field-wrapper"></div></div>');
    $ship.append($block);
    $block.find('.gm-addr-fields').append($addr);
  }

  /* ship-to-different-address + shipping fields + order notes also belong to step 2 */
  var $shipFields = $('.woocommerce-shipping-fields');
  if ($shipFields.length) $ship.append($shipFields);
  var $notes = $('.woocommerce-additional-fields');
  if ($notes.length) $ship.append($notes);

  /* ---- friendly placeholders (like the mockup) ---- */
  $('#billing_first_name').attr('placeholder', 'ישראל');
  $('#billing_last_name').attr('placeholder', 'ישראלי');
  $('#billing_phone').attr('placeholder', '050-0000000');
  $('#billing_email').attr('placeholder', 'name@example.com');
  $('#billing_address_2').attr('placeholder', 'דירה / כניסה (אופציונלי)');
  $('#order_comments').attr('placeholder', 'קומה, קוד כניסה, שעות נוחות...');
});
