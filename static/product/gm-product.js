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

/* ═══ polish r3: תווית נקייה, אייקוני אמון, מיקום המקושרים, מודאלי שירות ═══ */
(function ($) {
  'use strict';

  /* תווית "צבע: חום" — בסיס נקי גם אם התוסף/ריצה קודמת הוסיפו ערך */
  function cleanBase(t) { return t.split(':')[0].trim().replace(/^בחירת\s+/, ''); }
  var origLabelRow = null;
  $(function () {
    $('.gm-atc table.variations label').each(function () {
      $(this).data('gmBase', cleanBase($(this).text()));
    });
  });

  /* אייקוני קו לקוביות האמון (לפי סדר: משלוח/איסוף/מעבדה/אחריות) */
  var T_ICONS = [
    '<path d="M3 7h11v8H3zM14 10h4l3 3v2h-7z"/><circle cx="7" cy="17.5" r="1.6"/><circle cx="17" cy="17.5" r="1.6"/>',
    '<path d="M4 9l8-5 8 5v10a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1z"/><path d="M9 20v-6h6v6"/>',
    '<path d="M14.7 6.3a4.5 4.5 0 0 0-6 6L3 18l3 3 5.7-5.7a4.5 4.5 0 0 0 6-6L14 13l-3-3z"/>',
    '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="M9 12l2 2 4-4"/>'
  ];
  $(function () {
    $('.trust').each(function () {
      $(this).find('.titem').each(function (i) {
        var $t = $(this);
        if ($t.find('.t-ic').length) return;
        var inner = $t.html();
        $t.html('<span class="t-ic"><svg viewBox="0 0 24 24">' + T_ICONS[i % 4] + '</svg></span><span class="t-tx">' + inner + '</span>');
      });
    });
  });

  /* הווידג'ט המקורי של המקושרים (עם הפופ-אפ) — עולה למיקום המוקאפ: לפני שורת הקנייה */
  function placeLinked() {
    var $lp = $('.gm-lp-wrap').first();
    if (!$lp.length || $lp.data('gmPlaced')) return;
    var $anchor = $('.gm-atc');
    if ($anchor.length) { $anchor.after($lp); $lp.data('gmPlaced', 1); }
  }
  $(placeLinked);
  setInterval(placeLinked, 900);

  /* מודאלי Green Care / טרייד-אין — החוויה המלאה בפופ-אפ (בלי לעזוב את העמוד) */
  var SVC = {
    gc: 'https://gm-transfers.onrender.com/static/mockups/gm-greencare-landing.html',
    ti: 'https://gm-transfers.onrender.com/static/mockups/gm-tradein-mockup.html'
  };
  function svcOpen(kind) {
    var $b = $('#gmSvcBackdrop');
    if (!$b.length) {
      $b = $('<div class="gm-svc-backdrop" id="gmSvcBackdrop"><div class="gm-svc-modal">' +
             '<button type="button" class="gm-svc-x" onclick="jQuery(\'#gmSvcBackdrop\').removeClass(\'open\');document.body.style.overflow=\'\'">×</button>' +
             '<iframe id="gmSvcFrame" src="about:blank"></iframe></div></div>');
      $('body').append($b);
      $b.on('click', function (e) { if (e.target === this) { $b.removeClass('open'); document.body.style.overflow = ''; } });
    }
    $('#gmSvcFrame').attr('src', SVC[kind]);
    $b.addClass('open');
    document.body.style.overflow = 'hidden';
  }
  $(function () {
    $('.pwidgets .pw-card').each(function (i) {
      var $c = $(this), gc = $c.hasClass('gc');
      /* עיגול אייקון + וורדמארק כמו במוקאפ */
      if (!$c.find('.pw-ic').length) {
        var ic = gc
          ? '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>'
          : '<path d="M7 4 3.5 7.5 7 11"/><path d="M3.5 7.5H17a3.5 3.5 0 0 1 3.5 3.5"/><path d="M17 20l3.5-3.5L17 13"/><path d="M20.5 16.5H7A3.5 3.5 0 0 1 3.5 13"/>';
        $c.prepend('<span class="pw-ic"><svg viewBox="0 0 24 24">' + ic + '</svg></span>');
      }
      if (gc && !$c.find('.gc-word').length) {
        $c.prepend('<span class="gc-word"><span class="g1">green</span>care<b>.</b></span>');
      }
      /* לחיצה פותחת פופ-אפ במקום ניווט */
      $c.find('.pw-btn').attr('href', 'javascript:void(0)').off('click').on('click', function (e) {
        e.preventDefault();
        svcOpen(gc ? 'gc' : 'ti');
      });
    });
  });
})(jQuery);
