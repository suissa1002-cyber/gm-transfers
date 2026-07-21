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

  /* לייטבוקס: לחיצה על התמונה הראשית מגדילה למסך מלא, עם דפדוף בין תמונות
     הגלריה (חיצים/מקלדת/לחיצה על הרקע לסגירה) */
  function gmLbImgs() {
    var list = $('.gth').map(function () { return $(this).data('full'); }).get().filter(Boolean);
    if (!list.length) { var s = $('.gmain img').attr('src'); if (s) list = [s]; }
    return list;
  }
  function gmLbShow(i) {
    var imgs = $('#gmLb').data('imgs') || []; if (!imgs.length) return;
    i = ((i % imgs.length) + imgs.length) % imgs.length;
    $('#gmLb').data('idx', i).find('.gm-lb-img').attr('src', imgs[i]);
    $('#gmLb .gm-lb-cnt').text((i + 1) + ' / ' + imgs.length);
    $('#gmLb .gm-lb-prev,#gmLb .gm-lb-next').toggle(imgs.length > 1);
    $('#gmLb .gm-lb-cnt').toggle(imgs.length > 1);
  }
  function gmLbClose() { $('#gmLb').removeClass('open'); document.body.style.overflow = ''; }
  $(document).on('click', '.gm-pdp-wrap .gmain img', function () {
    var imgs = gmLbImgs(); if (!imgs.length) return;
    if (!$('#gmLb').length) {
      $('body').append(
        '<div id="gmLb" aria-modal="true" role="dialog" aria-label="תצוגת תמונה מוגדלת">' +
        '<button type="button" class="gm-lb-x" aria-label="סגור">×</button>' +
        '<button type="button" class="gm-lb-prev" aria-label="הקודמת">‹</button>' +
        '<img class="gm-lb-img" alt="">' +
        '<button type="button" class="gm-lb-next" aria-label="הבאה">›</button>' +
        '<div class="gm-lb-cnt"></div></div>');
      $('#gmLb').on('click', function (e) { if (e.target === this || $(e.target).is('.gm-lb-x')) gmLbClose(); });
      $('#gmLb .gm-lb-prev').on('click', function () { gmLbShow(($('#gmLb').data('idx') || 0) - 1); });
      $('#gmLb .gm-lb-next').on('click', function () { gmLbShow(($('#gmLb').data('idx') || 0) + 1); });
      $(document).on('keydown', function (e) {
        if (!$('#gmLb').hasClass('open')) return;
        if (e.key === 'Escape') gmLbClose();
        else if (e.key === 'ArrowLeft') gmLbShow(($('#gmLb').data('idx') || 0) + 1);
        else if (e.key === 'ArrowRight') gmLbShow(($('#gmLb').data('idx') || 0) - 1);
      });
    }
    var cur = $('.gmain img').attr('src');
    var idx = Math.max(0, imgs.indexOf(cur));
    $('#gmLb').data('imgs', imgs).addClass('open');
    document.body.style.overflow = 'hidden';
    gmLbShow(idx);
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
    /* "בחירת צבע" → "צבע: <הערך הנבחר>" — הערך נקרא מה-select הקנוני של WC */
    var $row = $ul.closest('tr, .value').closest('tr');
    var $label = $row.find('label').first();
    if (!$label.length) return;
    var base = ($label.data('gmBase') || $label.text().split(':')[0].trim().replace(/^בחירת\s+/, ''));
    $label.data('gmBase', base);
    var $sel = $row.find('select').first();
    var cur = '';
    if ($sel.length && $sel.val()) cur = $sel.find('option:selected').text().trim();
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
  function gmEnsureSku() {
    var $s = $('#gmSku');
    if (!$s.length) {                       /* התבנית לא הועלתה — מזריקים לבד לפני הטאבים */
      var $tabs = $('.tabs').first(); if (!$tabs.length) return null;
      $s = $('<div id="gmSku" class="gm-sku" style="display:none">מק״ט: <span class="gm-sku-v"></span></div>');
      $tabs.before($s);
    }
    return $s;
  }
  function gmSetSku(sku) {
    var $s = gmEnsureSku(); if (!$s) return;
    if (sku) { $s.find('.gm-sku-v').text(sku); $s.show(); } else { $s.hide(); }
  }
  function gmParentSku() { var $s = $('#gmSku'); return $s.length ? ($s.attr('data-parent-sku') || '') : ''; }
  /* באדג'ים בעמוד המוצר (החליף את סניפט #42987): לפי product_tag + "יבואן רשמי"
     לפי id. נטענים מ-Store API (שמחזיר tags) ומוזרקים כאוברליי על התמונה הראשית.
     ⚠️ מקביל ל-build_category_data.py::_badges — לשמור מסונכרן. */
  var GM_TAG_BADGES = { 3513: ['preorder', 'מכירה מוקדמת'], 3515: ['instock', 'זמין במלאי'], 3514: ['gifts', 'מתנה ברכישה'] };
  var GM_IMPORTER_IDS = [43268];
  function gmRenderBadges(d) {
    try {
      var $main = $('.gm-pdp-wrap .gmain'); if (!$main.length || !d) return;
      var out = [];
      (d.tags || []).forEach(function (t) { if (GM_TAG_BADGES[t.id]) out.push(GM_TAG_BADGES[t.id]); });
      if (GM_IMPORTER_IDS.indexOf(d.id) !== -1) out.push(['importer', 'יבואן רשמי']);
      $main.find('.gm-pdp-badges').remove();
      if (!out.length) return;
      var h = '<div class="gm-pdp-badges">' + out.map(function (b) {
        return '<span class="badge ' + b[0] + '">' + b[1] + '</span>';
      }).join('') + '</div>';
      $main.append(h);
    } catch (e) {}
  }
  $(function () {
    var m = (document.body.className.match(/postid-(\d+)/) || [])[1];
    if (!m) return;
    var $s = $('#gmSku');
    var parent = $s.length ? ($s.attr('data-parent-sku') || null) : null;
    if (parent) gmSetSku(parent);                       /* התבנית סיפקה מק"ט אב */
    fetch('/wp-json/wc/store/v1/products/' + m, { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!parent) { var $e = gmEnsureSku(); if ($e) $e.attr('data-parent-sku', d.sku || ''); gmSetSku(d.sku || ''); }
        gmRenderBadges(d);
      })
      .catch(function () {});
  });
  /* ── מונה צפיות (ביקון עמיד-מטמון): נורה מהלקוח פעם אחת לכל מוצר בסשן.
     LiteSpeed/Cloudflare מגישים HTML ממטמון בלי PHP, אז ספירה בצד-שרת ברינדור
     אינה אמינה — הביקון מבטיח ספירה בכל טעינה אמיתית של דפדפן. הצד השני:
     gm-product/v1/view בתוסף greenmobile-product (סינון בוטים + דה-דופ IP). ── */
  $(function () {
    try {
      var pid = (document.body.className.match(/postid-(\d+)/) || [])[1];
      if (!pid || navigator.webdriver) return;
      var k = 'gmv-' + pid;
      if (sessionStorage.getItem(k)) return;
      sessionStorage.setItem(k, '1');
      var fire = function () {
        fetch('/wp-json/gm-product/v1/view', {
          method: 'POST', credentials: 'same-origin', keepalive: true,
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: +pid })
        }).catch(function () {});
      };
      if ('requestIdleCallback' in window) requestIdleCallback(fire, { timeout: 3000 });
      else setTimeout(fire, 1500);
    } catch (e) {}
  });
  $(document).on('found_variation', 'form.variations_form', function (e, variation) {
    labelAll();
    if (variation && variation.price_html) $('.pricebox').html(variation.price_html);
    gmSetSku(variation && variation.sku ? variation.sku : gmParentSku());  /* מק"ט הווריאציה */
  });
  $(document).on('reset_data', 'form.variations_form', function () {
    labelAll();
    if (origPrice) $('.pricebox').html(origPrice);
    gmSetSku(gmParentSku());   /* חזרה למק"ט האב */
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
    /* סדר המוקאפ: צבע → נפח → אפשרויות נוספות → כמות+הוספה לסל → וידג'טים */
    var $lp = $('.gm-lp-wrap').first();
    if ($lp.length && !$lp.data('gmPlaced')) {
      var $vars = $('.gm-atc table.variations');
      if ($vars.length) { $vars.after($lp); $lp.data('gmPlaced', 1); }
      else if ($('.gm-atc').length) { $('.gm-atc form.cart').prepend($lp); $lp.data('gmPlaced', 1); }
    }
    var $svc = $('.gm-svc-addons').first();
    if ($svc.length && !$svc.data('gmPlaced') && $('.gm-atc').length) {
      $('.gm-atc').after($svc); $svc.data('gmPlaced', 1);
    }
    /* מוצר פשוט (בלי וריאציות) + בלי וידג'טי Green Care/טרייד-אין → העמודה
       השמאלית ריקה: מעבירים את קוביות האמון שמאלה (class על body ל-CSS) */
    var sparse = $('.gm-atc table.variations').length === 0 && $('.gm-svc-addons .addon').length === 0;
    $('body').toggleClass('gm-trust-left', sparse);
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

/* ═══ r4 — פורט 1:1 של טכנולוגיית המוקאפ ═══
   מקור האמת: agents/homepage/design/generate_product_mockup.py + fetch_product.py */
(function ($) {
  'use strict';

  /* ---------- מטריצת וריאציות דלילה: cfg|color → {price, stock} ---------- */
  var M = { price: {}, avail: {}, colors: [], cfgs: [], colorAttr: null, cfgAttrs: [] };
  function buildMatrix() {
    var $form = $('form.variations_form');
    if (!$form.length) return false;
    var vars = $form.data('product_variations');
    if (!vars || !vars.length) return false;
    var attrNames = Object.keys(vars[0].attributes || {});
    M.colorAttr = attrNames.find(function (n) { return /color|צבע/i.test(n); }) || null;
    M.cfgAttrs = attrNames.filter(function (n) { return n !== M.colorAttr; });
    vars.forEach(function (v) {
      var color = M.colorAttr ? (v.attributes[M.colorAttr] || '') : 'יחיד';
      var cfg = M.cfgAttrs.map(function (n) { return v.attributes[n] || ''; }).join('|') || 'יחיד';
      var key = cfg + '||' + color;
      M.price[key] = v.display_price;
      M.avail[key] = v.is_in_stock ? 'in' : 'out';
      if (M.colors.indexOf(color) < 0) M.colors.push(color);
      if (M.cfgs.indexOf(cfg) < 0) M.cfgs.push(cfg);
    });
    return true;
  }
  function curVal(attr) {
    var $ul = $('.gm-atc .variable-items-wrapper[data-attribute_name="' + attr + '"]');
    return $ul.find('.variable-item.selected').attr('data-value') || '';
  }
  function curColor() { return M.colorAttr ? curVal(M.colorAttr) : 'יחיד'; }
  function curCfg() {
    return M.cfgAttrs.map(function (n) { return curVal(n); }).join('|') || 'יחיד';
  }
  function itemUL(attr) { return $('.gm-atc .variable-items-wrapper[data-attribute_name="' + attr + '"]'); }

  /* ---------- הבורר החכם (refresh/pickStor מהמוקאפ) ---------- */
  function refreshSmart() {
    if (!M.cfgs.length) return;
    var color = curColor();
    /* אפרוּר תצורות שלא קיימות בצבע הנבחר (מטריצה דלילה) */
    M.cfgAttrs.forEach(function (attr, ai) {
      itemUL(attr).find('.variable-item').each(function () {
        var val = $(this).attr('data-value');
        var exists = M.cfgs.some(function (cfg) {
          var parts = cfg.split('|');
          return parts[ai] === val && ((cfg + '||' + color) in M.price);
        });
        $(this).toggleClass('gm-off', !exists);
      });
    });
    /* מלאי + טקסט לפי הצירוף שנבחר */
    var key = curCfg() + '||' + color;
    if (key in M.avail) {
      var ok = M.avail[key] !== 'out';
      var $ins = $('.instk');
      if (ok) $ins.removeClass('oos').html('✓ במלאי · מוכן למשלוח');
      else $ins.addClass('oos').html('אזל מהמלאי · זמין בהזמנה מהספק');
    }
  }
  /* בחירת ברירת-מחדל חכמה בטעינה: אם הצירוף שנבחר אוטומטית אזל מהמלאי —
     קופצים לצירוף שיש במלאי (מעדיפים לשמור את הצבע הנוכחי; אחרת מחליפים צבע) */
  function pickInStockDefault() {
    if (!M.cfgs.length || !Object.keys(M.avail).length) return;
    var color = curColor(), key = curCfg() + '||' + color;
    if ((key in M.avail) && M.avail[key] !== 'out') return;   /* כבר במלאי — לא נוגעים */
    var anyIn = Object.keys(M.avail).some(function (k) { return M.avail[k] !== 'out'; });
    if (!anyIn) return;                                        /* הכל אזל — משאירים כמו שהוא */
    /* 1) אותו צבע, תצורה אחרת במלאי */
    var target = null;
    M.cfgs.forEach(function (cfg) {
      if (!target && M.avail[cfg + '||' + color] === 'in') target = cfg + '||' + color;
    });
    /* 2) אחרת — הצירוף הראשון במלאי (כל צבע) */
    if (!target) {
      Object.keys(M.avail).forEach(function (k) { if (!target && M.avail[k] === 'in') target = k; });
    }
    if (!target) return;
    var parts = target.split('||'), tCfg = parts[0], tColor = parts[1];
    function selCfg() {
      tCfg.split('|').forEach(function (val, ai) {
        if (!val || val === 'יחיד') return;
        itemUL(M.cfgAttrs[ai]).find('.variable-item[data-value="' + val + '"]')
          .removeClass('gm-off').trigger('click');
      });
      setTimeout(refreshSmart, 120);
    }
    if (M.colorAttr && tColor !== 'יחיד' && tColor !== color) {
      itemUL(M.colorAttr).find('.variable-item[data-value="' + tColor + '"]').trigger('click');
      setTimeout(selCfg, 150);                                /* קודם צבע, ואז תצורה */
    } else {
      selCfg();
    }
  }
  /* קפיצה חכמה: תצורה שלא קיימת בצבע הנוכחי → עוברים לצבע שיש בו (עדיפות במלאי) */
  $(document).on('click', '.gm-atc .variable-item.gm-off', function (e) {
    e.preventDefault(); e.stopImmediatePropagation();
    var $it = $(this);
    var attr = $it.closest('.variable-items-wrapper').data('attribute_name');
    var ai = M.cfgAttrs.indexOf(attr);
    if (ai < 0 || !M.colorAttr) return;
    var val = $it.attr('data-value');
    var candidates = M.colors.filter(function (c) {
      return M.cfgs.some(function (cfg) { return cfg.split('|')[ai] === val && ((cfg + '||' + c) in M.price); });
    });
    var pref = candidates.find(function (c) {
      return M.cfgs.some(function (cfg) { return cfg.split('|')[ai] === val && M.avail[cfg + '||' + c] !== 'out'; });
    }) || candidates[0];
    if (!pref) return;
    /* בוחרים קודם את הצבע המתאים, ואז את התצורה המבוקשת */
    itemUL(M.colorAttr).find('.variable-item[data-value="' + pref + '"]').trigger('click');
    setTimeout(function () { $it.removeClass('gm-off').trigger('click'); setTimeout(refreshSmart, 120); }, 150);
  });
  $(document).on('click', '.gm-atc .variable-item', function () { setTimeout(refreshSmart, 120); });
  $(document).on('found_variation reset_data', 'form.variations_form', function () { setTimeout(refreshSmart, 60); });

  /* ---------- פרסר המפרט (פורט מדויק של הפרסר בפייתון) ---------- */
  var SPEC_LABELS = ["גודל מסך", "עמיד למים", "רזולוציה", "PPI", "צפיפות", "מעבד", "זיכרון RAM",
    "נפח אחסון", "מאפיינים נוספים", "חיישן ביומטרי", "חיישנים", "מימדים", "מידות",
    "מצלמה קדמית", "מצלמה אחורית", "מצלמה ראשית", "מצלמות", "משקל", "פלט שמע",
    "סים", "קיבולת סוללה", "סוללה", "טעינה", "מערכת הפעלה", "ערכת שבבים", "מאיץ גרפי",
    "חיבור USB", "בלוטות", "תדרי", "צבע", "מסך", "דגם"];
  function isLabel(s) {
    s = s.replace(/^\*+/, '').trim();
    return s.length <= 32 && SPEC_LABELS.some(function (k) { return s.indexOf(k) > -1; });
  }
  function parseSpec(text) {
    var lines = text.split('\n').map(function (x) { return x.trim(); }).filter(Boolean);
    var colon = lines.filter(function (l) { return l.indexOf(':') > -1; }).length;
    var rows = [];
    if (lines.length && colon >= lines.length * 0.5) {
      lines.forEach(function (l) {
        var i = l.indexOf(':');
        if (i > -1) {
          var k = l.slice(0, i).replace(/[*–-\s]+$/,'').replace(/^[*–-\s]+/,'').trim();
          var v = l.slice(i + 1).trim();
          if (k && v && k.length <= 40) rows.push([k, [v]]);
          else if (v && rows.length) rows[rows.length - 1][1].push(v);
        } else if (rows.length) rows[rows.length - 1][1].push(l);
      });
    } else {
      lines.forEach(function (l) {
        if (isLabel(l)) rows.push([l.replace(/^\*+/, '').trim(), []]);
        else if (rows.length) rows[rows.length - 1][1].push(l);
      });
    }
    return rows.filter(function (r) { return r[1].length; });
  }
  function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
  function buildSpec() {
    var $tbl = $('#tab-spec .spectbl');
    if (!$tbl.length || $tbl.data('gmParsed')) return;
    var raw = '';
    $tbl.find('tr').each(function () {
      var k = $(this).find('th').text().trim();
      if (k.indexOf('מפרט') > -1) raw = $(this).find('td').text();
    });
    if (!raw.trim()) return;
    var rows = parseSpec(raw);
    if (rows.length < 3) return;   /* פורמט לא מזוהה — משאירים את הטבלה */
    /* שורות תכונה גנריות שימושיות (בלי צירי וריאציה ובלי בלוב המפרט) */
    var extra = '';
    $tbl.find('tr').each(function () {
      var k = $(this).find('th').text().trim(), v = $(this).find('td').text().trim();
      if (!k || k.indexOf('מפרט') > -1 || k.indexOf('בחירת') > -1) return;
      if (rows.some(function (r) { return r[0] === k; })) return;
      extra += '<div class="spec-row"><div class="spec-k">' + esc(k) + '</div><div class="spec-v">' + esc(v) + '</div></div>';
    });
    var h = rows.map(function (r) {
      return '<div class="spec-row"><div class="spec-k">' + esc(r[0]) + '</div><div class="spec-v">' + esc(r[1].join(' · ')) + '</div></div>';
    }).join('') + extra;
    $tbl.replaceWith('<div class="specwrap">' + h + '</div>');
  }

  /* ---------- חילוץ התיאור הקצר: פסקת שיווק + אחריות לקוביות (כמו fetch_product) ---------- */
  function extractShort() {
    var $raw = $('#gm-shortdesc-raw');
    var market = '', warranty = '', note = '', giftImgs = [], giftText = '';
    if ($raw.length) {
      /* הערת מציאון: בלוק .gm-outlet-note. מחלצים ומסירים מה-DOM לפני זיהוי פסקת
         השיווק — אחרת ה-<strong>מציאון</strong> שבתוכה נתפס כפסקת השיווק ודורס אותה. */
      var $note = $raw.find('.gm-outlet-note').first();
      if ($note.length) { note = $note.text().trim(); $note.remove(); }
      /* באנר מתנות (תג "מתנות ברכישה"): תמונות בתיאור הקצר שאינן באנר המשלוחים
         או אייקון המותג — מוצגות מתחת לפסקת השיווק. בלעדיהן הבאדג' בקטלוג מבטיח
         מתנה שהעמוד לא מראה. */
      $raw.find('img').each(function () {
        /* LiteSpeed lazy-load מחליף src ב-placeholder; הכתובת האמיתית ב-data-src */
        var src = $(this).attr('data-src') || $(this).attr('data-lazy-src') || $(this).attr('src') || '';
        if (src && src.indexOf('data:') !== 0 && src.indexOf('Xnip2023') < 0 && src.indexOf('GREENMOBILE_PROFILE') < 0) giftImgs.push(src);
      });
      /* פורמט התיאורים של גלי: פסקת השיווק היא <strong> בתוך div — לא <p> */
      $raw.find('strong').each(function () {
        var t = $(this).text().trim();
        if (!t) return;
        if (/מתנ/.test(t) && !/^(אחריות|תשלומים|משלוח)/.test(t)) { if (!giftText) giftText = t; return; }
        if (!market && !/^(אחריות|תשלומים|משלוח)/.test(t)) market = t;
      });
      $raw.find('p').each(function () {
        var txt = $(this).text().trim();
        if (!txt || $(this).find('img').length) return;
        if (/^אחריות|אחריות:/.test(txt)) { warranty = (txt.split(':')[1] || txt).trim(); }
        else if (/משלוח מהיר|אקספרס|^משלוח חינם/.test(txt)) { /* שורות שירות — לקוביות בלבד */ }
        else if (!market && txt.indexOf('תשלומים') < 0) market = txt;
      });
    }
    if (market) $('#gmPshort').text(market); else $('#gmPshort').remove();
    if (giftImgs.length || giftText) {
      var $g = $('<div class="pgift"></div>');
      if (giftText) {
        $('<div class="pgift-t"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="18" height="18" aria-hidden="true"><rect x="3" y="8" width="18" height="4"/><path d="M5 12v8h14v-8"/><line x1="12" y1="8" x2="12" y2="20"/><path d="M12 8c-1.5-3.5-6-3.5-6-1s4.5 2.5 6 1zm0 0c1.5-3.5 6-3.5 6-1s-4.5 2.5-6 1z"/></svg><span></span></div>')
          .find('span').text(giftText).end().appendTo($g);
      }
      giftImgs.forEach(function (src) {
        $('<img loading="lazy" alt="מתנה ברכישה">').attr('src', src).appendTo($g);
      });
      $g.insertAfter($('#gmPshort').length ? $('#gmPshort') : $('.pricebox'));
    }
    if (note) {
      var txt = note.replace(/^\s*מציאון\s*[–\-]\s*/, '');   /* התווית מגיעה מהעיצוב */
      $('<div class="pnote"><b>מציאון</b><span></span></div>')
        .find('span').text(txt).end()
        .insertBefore($('#gmPshort').length ? $('#gmPshort') : $('.gm-atc'));
    }
    return { warranty: warranty || 'שנה אחריות יבואן' };
  }

  /* ---------- קוביות אמון — התוכן הדינמי של המוקאפ ---------- */
  function ensureFourTrust() {
    /* התבנית שולחת 2 קוביות במובייל — משלימים ל-4 (התוכן ממולא דינמית) */
    $('.trust').each(function () {
      var $t = $(this);
      while ($t.find('.titem').length < 4) {
        var $src = $t.find('.titem').first();
        if (!$src.length) return;
        $t.append($src.clone());
      }
    });
  }
  function buildTrust(warranty) {
    ensureFourTrust();
    var price = 0;
    var m = ($('.pricebox .price').text().match(/[\d,]+/) || [''])[0].replace(/,/g, '');
    price = parseInt(m, 10) || 0;
    var free = price >= 500;
    var cubes = [
      { t: free ? 'משלוח חינם' : 'משלוח רגיל', s: '1–6 ימי עסקים' },
      { t: 'משלוח באותו היום', s: 'בהזמנה עד 13:00 · א׳–ה׳ · ₪89 · ב״ש–חיפה' },
      { t: 'עד 12 תשלומים', s: 'אשראי · 3 ללא ריבית' },
      { t: 'אחריות', s: warranty }
    ];
    $('.trust').each(function () {
      $(this).find('.titem').each(function (i) {
        var c = cubes[i % 4];
        var $tx = $(this).find('.t-tx');
        var target = $tx.length ? $tx : $(this);
        target.html('<b>' + esc(c.t) + '</b><span>' + esc(c.s) + '</span>');
      });
    });
  }

  /* ---------- מיני-סל (דרור) — הסל האמיתי דרך Store API ---------- */
  var cartNonce = null;
  function storeNonce() {
    if (cartNonce) return Promise.resolve(cartNonce);
    return fetch('/wp-json/wc/store/v1/cart', { credentials: 'same-origin' })
      .then(function (r) { cartNonce = r.headers.get('Nonce'); return r.json(); })
      .then(function (c) { drawerRender(c); return cartNonce; });
  }
  function cartOp(path, payload) {
    return storeNonce().then(function (n) {
      return fetch('/wp-json/wc/store/v1/cart/' + path, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'Nonce': n }, body: JSON.stringify(payload)
      });
    }).then(function (r) { cartNonce = r.headers.get('Nonce') || cartNonce; return r.json(); });
  }
  function money(cents, minor) { return '‏₪' + (cents / Math.pow(10, minor)).toLocaleString('en-US'); }
  function drawerRender(c) {
    if (!c || !c.totals) return;
    drawerEnsure();                      /* הרינדור הראשון עשוי להקדים את יצירת הדרור */
    var $items = $('#cartItems'); if (!$items.length) return;
    var minor = c.totals.currency_minor_unit || 0;
    var count = 0;
    if (!(c.items || []).length) $items.html('<div class="cart-empty">הסל ריק</div>');
    else $items.html(c.items.map(function (it, i) {
      count += it.quantity;
      var img = (it.images && it.images[0]) ? it.images[0].thumbnail : '';
      var varTxt = (it.variation || []).map(function (v) { return v.value; }).join(' · ');
      return '<div class="citem" data-key="' + it.key + '"><img class="citem-img" src="' + img + '" alt="">' +
        '<div class="citem-main"><div class="citem-nm">' + esc(it.name) + '</div>' +
        (varTxt ? '<div class="citem-var">' + esc(varTxt) + '</div>' : '') +
        '<div class="citem-bottom"><div class="cqty"><button data-d="-1">−</button><span>' + it.quantity + '</span><button data-d="1">+</button></div>' +
        '<span class="citem-pr">' + money(it.totals.line_total, minor) + '</span></div></div>' +
        '<button class="citem-rm" aria-label="הסר"><svg viewBox="0 0 24 24" style="width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:2"><line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/></svg></button></div>';
    }).join(''));
    $('#cartSubtotal').text(money(+c.totals.total_items, minor));
    $('.cart-count-n').text(count);
    $('.mcart-b').text(count);
    $('.cart-pill').html($('.cart-pill svg').prop('outerHTML') + ' הסל שלי (' + count + ')');
    var sub = (+c.totals.total_items) / Math.pow(10, minor);
    var TH = 500, $ship = $('#cartShip');
    if (sub >= TH) $ship.html('<b>קיבלת משלוח חינם!</b><div class="bar"><div class="fill" style="width:100%"></div></div>');
    else $ship.html('עוד <b>‏₪' + (TH - sub).toLocaleString('en-US') + '</b> ותיהנו ממשלוח חינם<div class="bar"><div class="fill" style="width:' + Math.min(100, Math.round(sub / TH * 100)) + '%"></div></div>');
    gmBpCartLine(sub);
  }
  function drawerEnsure() {
    if ($('#cartDrawer').length) return;
    $('body').append(
      '<div class="cart-overlay" id="cartOverlay"></div>' +
      '<aside class="cart-drawer" id="cartDrawer" aria-label="עגלת הקניות">' +
      '<div class="cart-head"><strong>הסל שלי (<span class="cart-count-n">0</span>)</strong>' +
      '<button class="mclose" id="cartClose" aria-label="סגור">×</button></div>' +
      '<div class="cart-added" id="cartAdded">✓ המוצר נוסף לסל</div>' +
      '<div class="cart-ship" id="cartShip"></div>' +
      '<div class="cart-items" id="cartItems"></div>' +
      '<div class="cart-foot"><div class="cart-subtotal"><span>סכום ביניים</span><span class="cs-amt" id="cartSubtotal">‏₪0</span></div>' +
      '<div class="cart-note">המשלוח מחושב בעמוד התשלום</div>' +
      gmBpCartHtml() +
      '<a class="btn primary cart-checkout" href="/מעבר-לתשלום/">מעבר לתשלום</a></div></aside>');
    $('#cartOverlay,#cartClose').on('click', closeDrawer);
  }
  /* ── שורת "תשלום חודשי" של Blender בתחתית המגירה ──
     מבוססת על סכום הסל (לא על מחיר המוצר בעמוד). הנתונים מ-window.gmBpCfg
     שמוזרק ב-wp_head ע"י סניפט 49143 (מטמון התמחור של התוסף, בלי קריאת API).
     אין cfg / הסל מתחת לסף Blender → השורה פשוט לא מוצגת.
     זהה למימוש ב-gm_nav.GM_HEADER_JS (המגירה של שאר האתר). */
  function gmBpCartHtml() {
    var C = window.gmBpCfg;
    if (!C || !C.opts || !C.opts.length) return '';
    var logo = C.logo ? '<img class="gm-bp-cart-logo skip-lazy" src="' + C.logo + '" alt="Blender" width="65" height="30" decoding="async" data-no-lazy="1">' : '';
    return '<div class="gm-bp-cart" id="gmBpCart" hidden>' + logo +
      '<span class="gm-bp-cart-txt">או בעד <b class="gm-bp-cart-t">0</b> תשלומים החל מ־<b class="gm-bp-cart-v">₪0</b> לחודש' +
      '<span class="gm-bp-cart-sub">הוראת קבע ללא תפיסת מסגרת</span></span></div>';
  }
  function gmBpCartLine(sub) {
    var el = document.getElementById('gmBpCart'); if (!el) return;
    var C = window.gmBpCfg;
    if (!C || !C.opts || !C.opts.length || !(sub > 0) || sub < (C.min || 1000) || (C.max && sub > C.max)) { el.hidden = true; return; }
    var best = null, i, pay;
    for (i = 0; i < C.opts.length; i++) {
      pay = Math.ceil(sub / C.opts[i].T + sub / 1000 * C.opts[i].K);
      if (!best || pay < best.v) best = { t: C.opts[i].T, v: pay };
    }
    if (!best) { el.hidden = true; return; }
    var t = el.querySelector('.gm-bp-cart-t'), v = el.querySelector('.gm-bp-cart-v');
    if (!t || !v) { el.hidden = true; return; }
    t.textContent = best.t; v.textContent = '₪' + best.v.toLocaleString('en-US');
    el.hidden = false;
  }
  function openDrawer(added) {
    drawerEnsure();
    $('#cartDrawer').addClass('open'); $('#cartOverlay').addClass('open');
    document.body.style.overflow = 'hidden';
    if (added) { var $a = $('#cartAdded'); $a.addClass('show'); clearTimeout(window._caT); window._caT = setTimeout(function () { $a.removeClass('show'); }, 2600); }
  }
  function closeDrawer() { $('#cartDrawer').removeClass('open'); $('#cartOverlay').removeClass('open'); document.body.style.overflow = ''; }
  $(document).on('click', '.citem .cqty button', function () {
    var $ci = $(this).closest('.citem'), d = +$(this).data('d');
    var q = parseInt($ci.find('.cqty span').text(), 10) + d;
    (q < 1 ? cartOp('remove-item', { key: $ci.data('key') }) : cartOp('update-item', { key: $ci.data('key'), quantity: q })).then(drawerRender);
  });
  $(document).on('click', '.citem-rm', function () {
    cartOp('remove-item', { key: $(this).closest('.citem').data('key') }).then(drawerRender);
  });
  /* הוספה לסל בלי לעזוב את העמוד → נפתח הדרור (כמו במוקאפ) */
  $(document).on('submit', '.gm-atc form.cart', function (e) {
    var $form = $(this);
    var pid = +($form.find('input[name=variation_id]').val() || $form.find('button[name=add-to-cart]').val() || $form.data('product_id') || 0);
    if (!pid) return; /* בלי מזהה — נופלים לזרימה הרגילה */
    e.preventDefault();
    var qty = +($form.find('input.qty').val() || 1);
    cartOp('add-item', { id: pid, quantity: qty }).then(function (c) {
      if (c && c.items) { drawerRender(c); openDrawer(true); }
      else { $form.off('submit').trigger('submit'); }  /* שגיאה — הזרימה הרגילה */
    });
  });
  /* פיל הסל בהדר פותח את הדרור */
  $(document).on('click', '.cart-pill, .mcart', function (e) { e.preventDefault(); openDrawer(false); });

  /* ---------- init ---------- */
  $(function () {
    buildMatrix();
    /* אחרי ש-autoSelect (350ms) בחר דיפולט — מתקנים אם אזל, ואז מרעננים */
    setTimeout(function () { pickInStockDefault(); refreshSmart(); }, 700);
    buildSpec();
    var ex = extractShort();
    buildTrust(ex.warranty);
    storeNonce();  /* טוען את מצב הסל האמיתי לפיל ולדרור */
  });
})(jQuery);

/* ═══ r7: פלוס ירוק + באנר סיכום באפשרויות נוספות · מגן ירוק ולוגו גדול ב-Green Care ═══ */
(function ($) {
  'use strict';
  function linkedNote() {
    var $wrap = $('.gm-lp-wrap').first();
    if (!$wrap.length) return;
    var n = 0, sum = 0;
    $wrap.find('.gm-lp-tile.in-cart').each(function () {
      n++;
      var m = ($(this).find('.gm-lp-tile-price').text().match(/[\d,]+/) || [''])[0].replace(/,/g, '');
      sum += parseInt(m, 10) || 0;
    });
    var $note = $wrap.find('.gm-lp-note');
    if (!n) { $note.remove(); return; }
    if (!$note.length) { $note = $('<div class="gm-lp-note"></div>'); $wrap.append($note); }
    var lab = n === 1 ? 'נוסף אביזר אחד' : 'נוספו ' + n + ' אביזרים';
    $note.html('<svg viewBox="0 0 24 24" style="width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:2.2;stroke-linecap:round;stroke-linejoin:round"><path d="M4 12.5l5 5L20 6.5"/></svg> ' + lab + ' · +₪' + sum.toLocaleString('en-US'));
  }
  function gcIcon() {
    var $gc = $('.gm-svc-addons .addon.greencare');
    if (!$gc.length || $gc.find('.addon-ic').length) return;
    $gc.prepend('<span class="addon-ic"><svg viewBox="0 0 24 24" style="width:20px;height:20px;fill:none;stroke:#fff;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="M9 12l2 2 4-4"/></svg></span>');
  }
  $(function () { linkedNote(); gcIcon(); });
  setInterval(function () { linkedNote(); gcIcon(); }, 900);
})(jQuery);
