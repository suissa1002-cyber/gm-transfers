/* Green Mobile — עמוד המוצר החדש: גלריה, טאבים, וסנכרון תמונת-וריאציה.
   הטופס עצמו = WooCommerce המקורי (וריאציות/סל עובדים כרגיל). */
(function ($) {
  'use strict';

  /* גלריה: לחיצה על ממוזערת מחליפה את התמונה הראשית */
  $(document).on('click', '.gth', function () {
    var full = $(this).data('full');
    if (!full) return;
    $('.gth').removeClass('sel');
    $(this).addClass('sel');
    $('.gmain img').attr('src', full).removeAttr('srcset sizes');
  });

  /* טאבים: תיאור / מפרט */
  $(document).on('click', '.tabbar button', function () {
    var t = $(this).data('tab');
    $('.tabbar button').removeClass('sel');
    $(this).addClass('sel');
    $('.tabpane').removeClass('sel');
    $('#tab-' + t).addClass('sel');
  });

  /* וריאציה נבחרה → תמונת הווריאציה לגלריה הראשית */
  $(document).on('found_variation', 'form.variations_form', function (e, variation) {
    if (variation && variation.image && variation.image.full_src) {
      $('.gmain img').attr('src', variation.image.full_src).removeAttr('srcset sizes');
      $('.gth').removeClass('sel');
    }
  });
  $(document).on('reset_data', 'form.variations_form', function () {
    var $first = $('.gth').first();
    if ($first.length) $first.trigger('click');
  });
})(jQuery);

/* ═══ polish r2: תוויות חכמות, בחירת ברירת-מחדל, מחיר חי, אייקון בכפתור ═══ */
(function ($) {
  'use strict';
  var CART_SVG = '<svg style="width:20px;height:20px;fill:none;stroke:currentColor;stroke-width:1.8" viewBox="0 0 24 24"><circle cx="9" cy="19.5" r="1.4"/><circle cx="17" cy="19.5" r="1.4"/><path d="M3 4h2.5l2.2 11.5h10.4L20.5 8H7"/></svg>';
  var origPrice = null;

  function labelRow($ul) {
    /* "בחירת צבע" → "צבע: <הערך הנבחר>" */
    var $row = $ul.closest('tr, .value').closest('tr');
    var $label = $row.find('label').first();
    if (!$label.length) return;
    var base = ($label.data('gmBase') || $label.text().trim().replace(/^בחירת\s+/, ''));
    $label.data('gmBase', base);
    var cur = $ul.find('.variable-item.selected, .variable-item[aria-checked="true"]').attr('data-title') || '';
    $label.html(base + (cur ? ': <span class="curval">' + cur + '</span>' : ''));
  }
  function labelAll() { $('.gm-atc .variable-items-wrapper').each(function () { labelRow($(this)); }); }

  function autoSelect() {
    /* מוצר וריאציות בלי בחירה — בוחרים אוטומטית את האופציה הראשונה הזמינה */
    $('.gm-atc .variable-items-wrapper').each(function () {
      var $ul = $(this);
      if ($ul.find('.variable-item.selected').length) return;
      var $first = $ul.find('.variable-item:not(.disabled)').first();
      if ($first.length) $first.trigger('click');
    });
  }

  $(function () {
    var $btn = $('.gm-atc .single_add_to_cart_button');
    if ($btn.length && !$btn.find('svg').length) $btn.prepend(CART_SVG + ' ');
    $('.gm-atc form.cart').not('.variations_form').addClass('gm-simple');
    origPrice = $('.pricebox').html();
    labelAll();
    setTimeout(autoSelect, 350);
  });

  $(document).on('click', '.gm-atc .variable-item', function () { setTimeout(labelAll, 60); });
  $(document).on('found_variation', 'form.variations_form', function (e, variation) {
    labelAll();
    if (variation && variation.price_html) $('.pricebox').html(variation.price_html);
  });
  $(document).on('reset_data', 'form.variations_form', function () {
    labelAll();
    if (origPrice) $('.pricebox').html(origPrice);
  });
})(jQuery);
