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
