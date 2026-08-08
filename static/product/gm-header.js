/* ⚠️ נוצר אוטומטית ע"י agents/homepage/design/build_header_js.py — אין לערוך ידנית.
   מקור: gm_nav.GM_SEARCH_JS + gm_nav.GM_HEADER_JS.
   נטען בעמודים שמקבלים את ההדר המשותף מאופציית WP (מוצר/חוק/תודה), כי WordPress
   מסנן <script> משמירת אופציה — ולכן ההתנהגות לא יכולה לנסוע בתוך המרקאפ. */
(function () {
  if (window.__gmHeaderJS) return;
  window.__gmHeaderJS = true;

var _gsT=null,_gsSeq=0;   /* var: ההדר וגם עמודים ותיקים מכלילים את הבלוק — כפילות חייבת להיות חוקית */
function gmGoSearch(e,sfx){ e.preventDefault(); const v=document.getElementById('gsrch-'+sfx).value.trim();
  if(v) location.href='https://greenmobile.co.il/search/?q='+encodeURIComponent(v); return false; }
function gmSearchInput(inp,sfx){ const q=inp.value.trim(), box=document.getElementById('gsrch-sug-'+sfx);
  inp.closest('.gsrch').classList.toggle('has-text', inp.value.length>0);
  clearTimeout(_gsT); if(q.length<2){ box.classList.remove('show'); box.innerHTML=''; return; }
  _gsT=setTimeout(()=>gmRunSearch(q,box),180); }
function gmClearSearch(sfx){ const inp=document.getElementById('gsrch-'+sfx); inp.value='';
  inp.closest('.gsrch').classList.remove('has-text');
  const box=document.getElementById('gsrch-sug-'+sfx); box.classList.remove('show'); box.innerHTML=''; inp.focus(); }
var GM_FIBO='https://greenmobile.co.il/wp-content/plugins/ajax-search-for-woocommerce-premium/includes/Engines/TNTSearchMySQL/Endpoints/search.php';
function gmRunSearch(q,box){ const seq=++_gsSeq; box.innerHTML='<div class="sempty">מחפש…</div>'; box.classList.add('show');
  const u=GM_FIBO?GM_FIBO+'?s='+encodeURIComponent(q):'/api/mock/search?q='+encodeURIComponent(q)+'&limit=7';
  fetch(u).then(r=>r.json()).then(d=>{
    if(seq!==_gsSeq) return;
    let res=(d&&d.results)||[];
    if(GM_FIBO){ let prods=((d&&d.suggestions)||[]).filter(x=>x.type==='product');
      /* דיוק דגם: מספר בחיפוש (17, 512...) חייב להופיע בשם המוצר — המנוע משלים
         התאמות חלקיות (16 במקום 17); מסננים, עם נסיגה אם הרשימה מתרוקנת */
      const qn=(q.match(/\d+/g)||[]);
      if(qn.length){ const strict=prods.filter(x=>qn.every(n=>(x.value||'').includes(n)));
        if(strict.length) prods=strict; }
      res=prods.slice(0,7).map(x=>{ let im=x.image_src||x.thumb_url||'';
        if(!im&&x.thumb_html){ const m=x.thumb_html.match(/src="([^"]+)"/); if(m) im=m[1]; }
        return {name:x.value,url:x.url,img:im,priceHtml:x.price||''}; }); }
    if(!res.length){ box.innerHTML='<div class="sempty">לא נמצאו תוצאות</div>'; return; }
    const esc=s=>(s||'').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    let h=res.map(p=>'<a href="'+p.url+'"><img src="'+(p.img||'')+'" alt="" loading="lazy"><span class="snm">'+esc(p.name)+'</span><span class="spr">'+(p.priceHtml!=null&&p.priceHtml!==''?p.priceHtml:(p.price!=null?'₪'+p.price.toLocaleString('en-US'):''))+'</span></a>').join('');
    h+='<a class="sall" href="https://greenmobile.co.il/search/?q='+encodeURIComponent(q)+'">כל התוצאות ל\u201c'+esc(q)+'\u201d</a>';
    box.innerHTML=h;
  }).catch(()=>{ if(seq===_gsSeq) box.innerHTML='<div class="sempty">שגיאת חיפוש</div>'; }); }
document.addEventListener('click',e=>{ if(!e.target.closest('.gsrch')) document.querySelectorAll('.gsrch-sug').forEach(b=>b.classList.remove('show')); });

  /* ייצוא לגלובל: הטפסים בהדר קוראים ל-gmGoSearch/gmSearchInput ב-onsubmit/oninput */
  window.gmGoSearch = gmGoSearch;
  window.gmSearchInput = gmSearchInput;
  window.gmClearSearch = gmClearSearch;
  window.gmRunSearch = gmRunSearch;
})();

/* ── Header boost (אסי 31/07): הדר דביק בכל האתר + פונט ניווט מודגש + לוגו.
   למה ב-JS: (1) header.site{position:sticky} קיים ב-CSS של כל עמוד, אבל
   html/body{overflow-x:hidden} (מהתבניות) מבטל sticky בשקט בכל הצאצאים —
   ו-overflow:clip אסור על html כי הוא חותך את מגירת התפריט (position:fixed,
   הלקח מ-31/07). position:fixed עובד גם תחת hidden ⇒ הצמדה ב-JS עם ממלא-מקום.
   (2) ה-CSS של ההדר אפוי בכל עמוד בנפרד — הזרקת <style> מאוחרת מיישרת את
   כולם ממקור אחד בלי לבנות מחדש עשרות עמודים. z-index 55 < 60 (overlay). */
(function () {
  if (window.__gmHdrBoost) return; window.__gmHdrBoost = 1;
  var st = document.createElement('style'); st.id = 'gm-hdr-boost';
  st.textContent = 'nav.cats{font-size:1.02rem;font-weight:700;}'
    + '.gmnav{font-size:1rem;font-weight:700;}'
    + '.logo-img{height:40px!important;}'
    + '@media (max-width:820px){.logo-img{height:32px!important;}}'
    + '.mnav-head .logo-img{height:26px!important;}'
    + 'header.site.gm-hfix{position:fixed;top:0;left:0;right:0;z-index:55;'
    + 'box-shadow:0 6px 24px rgba(10,12,15,.08);}';
  /* ⚠️ לסוף ה-body ולא ל-head: בעמודים האפויים (בית/קטלוג) ה-CSS יושב בתוך
     ה-body — style שמוזרק ל-head מפסיד לו בסדר המסמך. אחרון במסמך = מנצח. */
  (document.body || document.head).appendChild(st);
  function init() {
    var hdr = document.querySelector('header.site');
    if (!hdr || hdr.__gmHfix) return;
    hdr.__gmHfix = 1;
    var ph = document.createElement('div'); ph.style.display = 'none';
    hdr.parentNode.insertBefore(ph, hdr.nextSibling);
    var fixed = false;
    function upd() {
      var y = window.pageYOffset || document.documentElement.scrollTop || 0;
      if (y > 2 && !fixed) {
        ph.style.height = hdr.offsetHeight + 'px'; ph.style.display = 'block';
        hdr.classList.add('gm-hfix'); fixed = true;
      } else if (y <= 2 && fixed) {
        hdr.classList.remove('gm-hfix'); ph.style.display = 'none'; fixed = false;
      }
    }
    window.addEventListener('scroll', upd, { passive: true });
    window.addEventListener('resize', function () {
      if (fixed) { hdr.classList.remove('gm-hfix'); ph.style.display = 'none'; fixed = false; }
      upd();
    }, { passive: true });
    upd();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();

if (!window.toggleNav) { window.toggleNav = function () {
  var n = document.getElementById('mnav'), o = document.getElementById('mnavOverlay');
  var open = n.classList.toggle('open'); o.classList.toggle('open', open);
  document.body.style.overflow = open ? 'hidden' : '';
}; }

/* ── מיני-סל משותף: ספירה חיה + מגירה, זהה בכל עמוד. ──
   הבעיה שנפתרת: ספירת הסל נאפתה "0" לתוך עמוד המטמון של LiteSpeed —
   כאן מושכים את הסל האמיתי מ-Store API בכל טעינה ומעדכנים באדג'+פיל.
   בעמוד מוצר (.gm-atc קיים) ה-gm-product.js הייעודי הוא הבעלים — פה יוצאים. */
(function () {
  if (window.__gmHdrCart) return;
  if (document.querySelector('.gm-atc')) return;
  window.__gmHdrCart = true;
  var API = '/wp-json/wc/store/v1/cart', nonce = null;
  function esc(s){var d=document.createElement('div');d.textContent=(s==null?'':s);return d.innerHTML;}
  /* Store API מחזיר שמות מקודדי-ישויות (&#8211;) — מפענחים לפני esc, אחרת הישות מוצגת כטקסט */
  function dec(s){var t=document.createElement('textarea');t.innerHTML=(s==null?'':String(s));return t.value;}
  function money(c,m){return '‏₪'+(c/Math.pow(10,m||0)).toLocaleString('en-US');}
  function get(){return fetch(API,{credentials:'same-origin'}).then(function(r){nonce=r.headers.get('Nonce');return r.json();});}
  function op(path,payload){
    var run = nonce ? Promise.resolve(nonce) : get().then(function(){return nonce;});
    return run.then(function(n){
      return fetch(API+'/'+path,{method:'POST',credentials:'same-origin',
        headers:{'Content-Type':'application/json','Nonce':n},body:JSON.stringify(payload)});
    }).then(function(r){nonce=r.headers.get('Nonce')||nonce;return r.json();});
  }
  function setCount(n){
    var mb=document.querySelector('.mcart-b'); if(mb) mb.textContent=n;
    var pill=document.querySelector('.cart-pill');
    if(pill){var svg=pill.querySelector('svg'); pill.innerHTML=(svg?svg.outerHTML:'')+' הסל שלי ('+n+')';}
    document.querySelectorAll('.cart-count-n').forEach(function(e){e.textContent=n;});
  }
  var SIDE_HTML='<div class="cart-side" id="cartSide">'+
    '<div class="side-h"><b>שווה להוסיף עכשיו 🚀</b>'+
    '<small>האביזרים שמתאימים למכשירים שבסל</small></div>'+
    '<div class="side-scroll" id="cartSideList"></div></div>';
  /* ⚠️ רוב עמודי האתר **אופים** את מגירת הסל ב-HTML שלהם (העתק של הקוד הזה
     מזמן הבנייה). לכן v2 לא יכול "לדלג אם קיים" — הוא משדרג את הקיים:
     עוטף את תוכנו ב-.cart-main ומוסיף את פס התוספות. ככה השדרוג חל בכל
     עמוד מיד, בלי לבנות מחדש עשרות עמודים. (אסי, 08/08) */
  function upgrade(d){
    if(!d || d.querySelector('.cart-main')) return;
    var main=document.createElement('div'); main.className='cart-main';
    while(d.firstChild) main.appendChild(d.firstChild);
    d.appendChild(main);
    d.insertAdjacentHTML('beforeend', SIDE_HTML);
    d.classList.add('no-rail');
  }
  function ensure(){
    var ex=document.getElementById('cartDrawer');
    if(ex){ upgrade(ex); return; }
    var w=document.createElement('div');
    w.innerHTML=''+
      '<div class="cart-overlay" id="cartOverlay"></div>'+
      '<aside class="cart-drawer no-rail gmv2" id="cartDrawer" aria-label="עגלת הקניות">'+
      '<div class="cart-main">'+
      '<div class="cart-head"><strong>הסל שלי (<span class="cart-count-n">0</span>)</strong>'+
      '<button class="mclose" id="cartClose" aria-label="סגור">×</button></div>'+
      '<div class="cart-ship" id="cartShip"></div>'+
      '<div class="cart-items" id="cartItems"></div>'+
      '<div class="cart-foot"><div class="cart-subtotal"><span>סכום ביניים</span><span class="cs-amt" id="cartSubtotal">‏₪0</span></div>'+
      '<div class="cart-note">המשלוח מחושב בעמוד התשלום</div>'+
      gmBpCartHtml()+
      '<a class="cart-checkout" href="/מעבר-לתשלום/">מעבר לתשלום</a></div></div>'+
      SIDE_HTML+'</aside>';
    while(w.firstChild) document.body.appendChild(w.firstChild);
    document.getElementById('cartOverlay').addEventListener('click',close);
    document.getElementById('cartClose').addEventListener('click',close);
  }
  /* ── שורת "תשלום חודשי" של Blender בתחתית המגירה ──
     מבוססת על סכום הסל (לא על מחיר מוצר). הנתונים מגיעים מ-window.gmBpCfg
     שמוזרק ב-wp_head ע"י סניפט 49143 (מטמון התמחור של התוסף, בלי קריאת API).
     אין cfg / הסל מתחת לסף Blender → השורה פשוט לא מוצגת. */
  function gmBpCartHtml(){
    var C=window.gmBpCfg;
    if(!C||!C.opts||!C.opts.length) return '';
    var logo=C.logo?'<img class="gm-bp-cart-logo skip-lazy" src="'+C.logo+'" alt="Blender" width="65" height="30" decoding="async" data-no-lazy="1">':'';
    return '<div class="gm-bp-cart" id="gmBpCart" hidden>'+logo+
      '<span class="gm-bp-cart-txt">או בעד <b class="gm-bp-cart-t">0</b> תשלומים החל מ־<b class="gm-bp-cart-v">₪0</b> לחודש'+
      '<span class="gm-bp-cart-sub">הוראת קבע ללא תפיסת מסגרת</span></span></div>';
  }
  function gmBpCartLine(sub){
    var el=document.getElementById('gmBpCart'); if(!el) return;
    var C=window.gmBpCfg;
    if(!C||!C.opts||!C.opts.length||!(sub>0)||sub<(C.min||1000)||(C.max&&sub>C.max)){ el.hidden=true; return; }
    var best=null,i,pay;
    for(i=0;i<C.opts.length;i++){
      pay=Math.ceil(sub/C.opts[i].T+sub/1000*C.opts[i].K);
      if(!best||pay<best.v) best={t:C.opts[i].T,v:pay};
    }
    if(!best){ el.hidden=true; return; }
    var t=el.querySelector('.gm-bp-cart-t'), v=el.querySelector('.gm-bp-cart-v');
    if(!t||!v){ el.hidden=true; return; }
    t.textContent=best.t; v.textContent='₪'+best.v.toLocaleString('en-US');
    el.hidden=false;
  }
  function render(c){
    if(!c||!c.totals) return;
    ensure();
    var items=document.getElementById('cartItems'); if(!items) return;
    var minor=c.totals.currency_minor_unit||0, count=0, list=c.items||[];
    if(!list.length){ items.innerHTML='<div class="cart-empty">הסל ריק</div>'; }
    else { items.innerHTML=list.map(function(it){
      count+=it.quantity;
      var img=(it.images&&it.images[0])?it.images[0].thumbnail:'';
      var vv=(it.variation||[]).map(function(v){return v.value;}).join(' · ');
      return '<div class="citem" data-key="'+it.key+'" data-pid="'+it.id+'"><img class="citem-img" src="'+img+'" alt="">'+
        '<div class="citem-main"><div class="citem-nm">'+esc(dec(it.name))+'</div>'+
        (vv?'<div class="citem-var">'+esc(dec(vv))+'</div>':'')+
        '<div class="citem-bottom"><div class="cqty"><button data-d="-1">−</button><span>'+it.quantity+'</span><button data-d="1">+</button></div>'+
        '<span class="citem-pr">'+money(it.totals.line_total,minor)+'</span></div></div>'+
        '<button class="citem-rm" data-key="'+it.key+'" aria-label="הסר"><svg viewBox="0 0 24 24" style="width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:2"><line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/></svg></button></div>';
    }).join(''); }
    var subEl=document.getElementById('cartSubtotal'); if(subEl) subEl.textContent=money(+c.totals.total_items,minor);
    setCount(count);
    var sub=(+c.totals.total_items)/Math.pow(10,minor), TH=500, ship=document.getElementById('cartShip');
    if(ship){
      if(sub>=TH) ship.innerHTML='<b>קיבלת משלוח חינם!</b><div class="bar"><div class="fill" style="width:100%"></div></div>';
      else ship.innerHTML='עוד <b>‏₪'+(TH-sub).toLocaleString('en-US')+'</b> ותיהנו ממשלוח חינם<div class="bar"><div class="fill" style="width:'+Math.min(100,Math.round(sub/TH*100))+'%"></div></div>';
    }
    gmBpCartLine(sub);
    extras(list);
  }
  /* ══ תוספות + Green Care לפי הסל (אסי 07/08) ══════════════════════════
     שתי נקודות קצה ציבוריות לקריאה בלבד:
       gm-addons/v1/for-cart?ids=   → התוספות המשויכות למוצרים שבסל
       gm-services/v1/greencare?ids= → זכאות Green Care לכל מוצר (tiers+prices)
     נכשל / אין תוצאות ⇒ הפס נעלם (.no-rail) והכפתורים לא מוצגים. אפס שבירה. */
  function extras(list){
    var ids=(list||[]).map(function(it){return it.id;}).filter(Boolean).join(',');
    var side=document.getElementById('cartSideList'), drawer=document.getElementById('cartDrawer');
    if(!ids){ if(side)side.innerHTML=''; if(drawer)drawer.classList.add('no-rail'); return; }
    fetch('/wp-json/gm-addons/v1/for-cart?ids='+ids,{credentials:'same-origin'})
      .then(function(r){return r.json();}).then(function(d){ rail((d&&d.items)||[]); })
      .catch(function(){ rail([]); });
    fetch('/wp-json/gm-services/v1/greencare?ids='+ids,{credentials:'same-origin'})
      .then(function(r){return r.json();}).then(function(d){ gcRender((d&&d.items)||{}, list); })
      .catch(function(){});
  }
  function rail(items){
    var side=document.getElementById('cartSideList'), drawer=document.getElementById('cartDrawer');
    if(!side||!drawer) return;
    if(!items.length){ side.innerHTML=''; drawer.classList.add('no-rail'); return; }
    drawer.classList.remove('no-rail');
    side.innerHTML=items.map(function(a){
      return '<div class="acard" data-aid="'+a.id+'">'+
        '<img src="'+a.img+'" alt="" loading="lazy">'+
        '<div class="acard-n">'+esc(dec(a.name))+'</div>'+
        '<div class="acard-p">‏₪'+(+a.price).toLocaleString('en-US')+'</div>'+
        '<button class="aadd" type="button" data-aid="'+a.id+'" aria-label="הוספה לסל">+</button></div>';
    }).join('');
  }
  var GC_SHIELD='<svg class="ic" viewBox="0 0 24 24"><path d="M12 2.5 4.5 5.5v6c0 4.5 3.2 8 7.5 10 4.3-2 7.5-5.5 7.5-10v-6z"/><path d="M8.8 11.8l2.3 2.3 4.3-4.5"/></svg>';
  function gcRender(map, list){
    /* שורות Green Care שכבר בסל — מזוהות לפי שם הפריט (מוצר-העוגן) */
    var added=(list||[]).filter(function(it){return /green\s?care/i.test(dec(it.name||''));})
      .map(function(it){ return (dec(it.name||'')+' '+((it.item_data||[]).map(function(m){return m.value;}).join(' '))).toLowerCase(); });
    document.querySelectorAll('.citem').forEach(function(ci){
      var old=ci.nextElementSibling;
      while(old && (old.classList.contains('gcopts')||old.classList.contains('gcmore'))){ var nx=old.nextElementSibling; old.remove(); old=nx; }
      var pid=ci.getAttribute('data-pid'), conf=map[pid];
      if(!conf) return;
      var nm=(conf.name||'').toLowerCase().slice(0,14);
      var isAdded=added.some(function(t){return nm && t.indexOf(nm)>-1;});
      var rows='';
      if(conf.tiers&&conf.tiers.gc&&conf.prices.gc>0){
        rows+=btn(pid,'gc',conf.prices.gc,'Green Care','אחריות שנה שנייה מלאה',isAdded);
      }
      if(conf.tiers&&conf.tiers.gcp&&conf.prices.gcp>0){
        rows+=btn(pid,'gcp',conf.prices.gcp,'Green Care <b>+</b>','24 חודשים, כולל שברים ונזקי נוזלים',isAdded);
      }
      if(!rows) return;
      var wrap=document.createElement('div'); wrap.className='gcopts'; wrap.innerHTML=rows;
      ci.insertAdjacentElement('afterend', wrap);
      var more=document.createElement('a'); more.className='gcmore'; more.href='/green-care/';
      more.innerHTML='מה כלול בכל מסלול? <u>לפרטים המלאים</u>';
      wrap.insertAdjacentElement('afterend', more);
    });
  }
  function btn(pid,plan,price,title,sub,isAdded){
    return '<button class="gcopt'+(isAdded?' added':'')+'" type="button" data-gc="'+plan+'" data-pid="'+pid+'" data-price="'+price+'">'+
      GC_SHIELD+'<span class="gcopt-t">'+(isAdded?'':'הוספת ')+title+
      '<span>'+sub+'</span></span><span class="gcopt-p">+‏₪'+(+price).toLocaleString('en-US')+'</span></button>';
  }
  function open(){ ensure(); document.getElementById('cartDrawer').classList.add('open'); document.getElementById('cartOverlay').classList.add('open'); document.body.style.overflow='hidden'; get().then(render); }
  function close(){ var d=document.getElementById('cartDrawer'),o=document.getElementById('cartOverlay'); if(d)d.classList.remove('open'); if(o)o.classList.remove('open'); document.body.style.overflow=''; }
  function openWith(c){ ensure(); render(c);
    document.getElementById('cartDrawer').classList.add('open');
    document.getElementById('cartOverlay').classList.add('open');
    document.body.style.overflow='hidden'; }
  document.addEventListener('click',function(e){
    var t=e.target;
    if(t.closest('.cart-pill,.mcart')){ e.preventDefault(); open(); return; }
    /* "הוספה לסל" מכרטיסיות (בית/קטלוג): הוספה דרך Store API + פתיחת המיני-עגלה
       מיד — כמו בעמוד מוצר. נכשל? נופלים לניווט הרגיל של ?add-to-cart. */
    var atc=t.closest('a.card-btn');
    if(atc && !atc.classList.contains('opts') && /[?&]add-to-cart=\d+/.test(atc.getAttribute('href')||'')){
      e.preventDefault();
      if(atc.dataset.busy) return;
      var id=+(atc.getAttribute('href').match(/add-to-cart=(\d+)/)||[])[1];
      if(!id){ location.href=atc.href; return; }
      atc.dataset.busy='1'; var txt=atc.textContent; atc.textContent='מוסיף לסל…';
      op('add-item',{id:id,quantity:1}).then(function(c){
        delete atc.dataset.busy; atc.textContent=txt;
        if(c && c.items){ openWith(c); } else { location.href=atc.href; }
      }).catch(function(){ delete atc.dataset.busy; atc.textContent=txt; location.href=atc.href; });
      return;
    }
    var q=t.closest('.citem .cqty button');
    if(q){ var ci=q.closest('.citem'), d=+q.getAttribute('data-d'), cur=parseInt(ci.querySelector('.cqty span').textContent,10)+d;
      (cur<1?op('remove-item',{key:ci.getAttribute('data-key')}):op('update-item',{key:ci.getAttribute('data-key'),quantity:cur})).then(render); return; }
    var rm=t.closest('.citem-rm');
    if(rm){ op('remove-item',{key:rm.getAttribute('data-key')}).then(render); return; }
    /* + על תוספת בפס הצדדי */
    var aa=t.closest('.aadd');
    if(aa && !aa.classList.contains('done')){
      var aid=+aa.getAttribute('data-aid'); if(!aid) return;
      aa.disabled=true; aa.textContent='…';
      op('add-item',{id:aid,quantity:1}).then(function(c){ if(c&&c.items) render(c); else { aa.disabled=false; aa.textContent='+'; } })
        .catch(function(){ aa.disabled=false; aa.textContent='+'; });
      return;
    }
    /* בחירת מסלול Green Care — נשמר דרך אותו נתיב של עמוד המוצר
       (admin-ajax gm_svc_greencare), שכבר יודע לצרף שורה למכשיר שבסל. */
    var gc=t.closest('.gcopt');
    if(gc && !gc.classList.contains('added')){
      var fd=new FormData();
      fd.append('action','gm_svc_greencare');
      fd.append('plan',gc.getAttribute('data-gc'));
      fd.append('product_id',gc.getAttribute('data-pid'));
      fd.append('price',gc.getAttribute('data-price'));
      gc.style.opacity='.6';
      fetch('/wp-admin/admin-ajax.php',{method:'POST',credentials:'same-origin',body:fd})
        .then(function(r){return r.json();})
        .then(function(){ gc.style.opacity=''; get().then(render); })
        .catch(function(){ gc.style.opacity=''; });
      return;
    }
  });
  function init(){ ensure(); get().then(render); }
  if(document.readyState!=='loading') init(); else document.addEventListener('DOMContentLoaded',init);
})();

(function () {
  if (window.__gmCartV2) return; window.__gmCartV2 = 1;
  var API='/wp-json/wc/store/v1/cart', nonce=null, tmr=null;
  var CSS=[
   '.cart-drawer.gmv2{width:min(96vw,740px)!important;display:grid!important;grid-template-columns:minmax(0,1fr) minmax(0,240px);flex-direction:unset!important;overflow:hidden;}',
   '.cart-drawer.gmv2.no-rail{width:min(93vw,450px)!important;grid-template-columns:minmax(0,1fr);}',
   '.cart-drawer.gmv2.no-rail .cart-side{display:none;}',
   '.cart-main{display:flex;flex-direction:column;min-width:0;min-height:0;overflow:hidden;}',
   '.cart-side{background:var(--alt,#f5f7f6);border-inline-end:1px solid var(--line,#e6eae8);display:flex;flex-direction:column;min-width:0;min-height:0;overflow:hidden;}',
   '.side-h{padding:15px 14px 11px;flex:none;text-align:center;}',
   '.side-h b{display:block;font-size:.87rem;font-weight:900;line-height:1.35;}',
   '.side-h small{display:block;color:var(--ink2,#5c666d);font-size:.72rem;font-weight:600;line-height:1.35;margin-top:4px;}',
   '.side-scroll{flex:1;min-height:0;overflow-y:auto;-webkit-overflow-scrolling:touch;padding:0 12px 14px;}',
   '.acard{background:var(--surface,#fff);border:1px solid var(--line,#e6eae8);border-radius:16px;padding:12px 10px 13px;margin-bottom:10px;text-align:center;}',
   '.acard img{width:104px;height:104px;object-fit:contain;display:block;margin:0 auto 8px;}',
   '.acard-n{font-size:.75rem;font-weight:700;line-height:1.3;color:var(--ink2,#5c666d);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:2.1em;}',
   '.acard-p{font-weight:900;font-size:1rem;margin:6px 0 8px;}',
   '.aadd{width:34px;height:34px;border-radius:50%;border:none;background:var(--accent,#149c40);color:#fff;font-size:1.3rem;line-height:1;cursor:pointer;margin:0 auto;display:block;}',
   '.aadd[disabled]{opacity:.55;cursor:default;}',
   '.aadd.done{background:var(--accent-soft,#e7f6ec);color:#0e5c2b;font-size:1rem;}',
   '.gcopts{display:grid;grid-template-columns:1fr;gap:7px;margin:14px 0 8px;width:66%;}',
   '.gcopt{display:flex;align-items:center;gap:9px;background:var(--surface,#fff);border:1.5px solid var(--line,#e6eae8);border-radius:13px;padding:10px 12px;cursor:pointer;font-family:inherit;text-align:start;width:100%;}',
   '.gcopt:hover{border-color:#9ad7b0;}',
   '.gcopt .ic{width:17px;height:17px;flex:none;fill:none;stroke:var(--accent,#149c40);stroke-width:1.9;}',
   '.gcopt-t{font-size:.8rem;font-weight:800;line-height:1.25;color:var(--ink,#111417);}',
   '.gcopt-t span{display:block;font-weight:600;color:var(--ink2,#5c666d);font-size:.72rem;margin-top:1px;}',
   '.gcopt-p{margin-inline-start:auto;font-weight:900;font-size:.85rem;white-space:nowrap;}',
   '.gcopt.added{border-color:var(--accent,#149c40);background:var(--accent-soft,#e7f6ec);}',
   '.gcmore{display:block;margin:0 0 14px;font-size:.72rem;color:var(--ink2,#5c666d);text-decoration:none;}',
   /* רצועת הכניסה וכפתור החזרה שייכים למובייל בלבד */
   '.gm-side-cta{display:none;}',
   '.side-back{display:none;}',
   /* ── מובייל: אין מקום לעמודה שנייה ⇒ שלב נפרד (כמו GoMobile): רצועת
      כניסה מעל הפוטר, ומסך תוספות שנפתח מעל הסל עם "חזרה לסל". הפוטר
      (סכום + מעבר לתשלום) נשאר גלוי — bottom מחושב ב-JS לפי גובהו. ── */
   '@media(max-width:820px){',
   '.cart-drawer.gmv2,.cart-drawer.gmv2.no-rail{width:min(93vw,430px)!important;grid-template-columns:minmax(0,1fr);}',
   '.cart-drawer.gmv2 .cart-side{position:absolute;inset-inline:0;top:0;bottom:0;transform:translateX(105%);transition:transform .28s ease;z-index:6;border:0;background:var(--bg,#fff);}',
   '.cart-drawer.gmv2.side-open .cart-side{transform:none;}',
   '.cart-drawer.gmv2.no-rail .cart-side{display:flex;}',
   '.side-back{display:flex!important;align-items:center;gap:8px;background:none;border:none;font:inherit;font-weight:900;font-size:1rem;color:var(--ink,#111417);padding:16px 18px 10px;cursor:pointer;width:100%;}',
   '.side-back svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:2.2;stroke-linecap:round;}',
   '.gm-side-cta{display:flex!important;align-items:center;gap:10px;width:100%;background:var(--accent-soft,#e7f6ec);border:none;border-radius:14px;padding:12px 14px;margin:0 0 12px;font:inherit;font-weight:800;color:#0e5c2b;cursor:pointer;text-align:start;}',
   '.gm-side-cta i{margin-inline-start:auto;font-style:normal;font-weight:900;}',
   '.acard{display:flex;align-items:center;gap:12px;text-align:start;padding:10px;}',
   '.acard img{width:64px;height:64px;margin:0;}',
   '.acard-n{min-height:0;}',
   '.acard-p{margin:4px 0 0;font-size:.95rem;}',
   '.aadd{margin:0;flex:none;}',
   '}'
  ].join('');
  var BACK='<button class="side-back" id="cartSideBack" type="button" hidden>'+
    '<svg viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg> חזרה לסל קניות</button>';
  var SIDE='<div class="cart-side" id="cartSide">'+BACK+'<div class="side-h"><b>שווה להוסיף עכשיו 🚀</b>'+
    '<small>האביזרים שמתאימים למכשירים שבסל</small></div><div class="side-scroll" id="cartSideList"></div></div>';
  var SHIELD='<svg class="ic" viewBox="0 0 24 24"><path d="M12 2.5 4.5 5.5v6c0 4.5 3.2 8 7.5 10 4.3-2 7.5-5.5 7.5-10v-6z"/><path d="M8.8 11.8l2.3 2.3 4.3-4.5"/></svg>';
  function esc(s){var d=document.createElement('div');d.textContent=(s==null?'':s);return d.innerHTML;}
  function dec(s){var t=document.createElement('textarea');t.innerHTML=(s==null?'':String(s));return t.value;}
  function style(){ if(document.getElementById('gm-cart-v2')) return;
    var st=document.createElement('style'); st.id='gm-cart-v2'; st.textContent=CSS;
    (document.body||document.head).appendChild(st); }
  function upgrade(d){
    if(d.querySelector('.cart-main')){          /* נוצר ע"י המודול הרגיל */
      d.classList.add('gmv2');
      if(!d.querySelector('.cart-side')) d.insertAdjacentHTML('beforeend', SIDE);
      return d.querySelector('.cart-main');
    }
    var main=document.createElement('div'); main.className='cart-main';
    while(d.firstChild) main.appendChild(d.firstChild);
    d.appendChild(main); d.insertAdjacentHTML('beforeend', SIDE);
    /* ⛔ ה-CSS של v2 ממוקד ל-.gmv2 — נוסף רק כאן, אחרי שהמבנה באמת שודרג.
       (08/08: הזרקת grid למגירה עם מבנה ישן שברה את הפריסה בעמודי מוצר.) */
    d.classList.add('gmv2'); d.classList.add('no-rail');
    return main;
  }
  function get(){ return fetch(API,{credentials:'same-origin'}).then(function(r){nonce=r.headers.get('Nonce');return r.json();}); }
  function add(id){
    var run = nonce ? Promise.resolve(nonce) : get().then(function(){return nonce;});
    return run.then(function(n){ return fetch(API+'/add-item',{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json','Nonce':n},body:JSON.stringify({id:id,quantity:1})}); })
      .then(function(r){ nonce=r.headers.get('Nonce')||nonce; return r.json(); });
  }
  function rail(items, inCart){
    var box=document.getElementById('cartSideList'), d=document.getElementById('cartDrawer');
    if(!box||!d) return;
    if(!items.length){ box.innerHTML=''; d.classList.add('no-rail'); cta(0); return; }
    d.classList.remove('no-rail'); cta(items.length);
    box.innerHTML=items.map(function(a){
      var has=inCart.indexOf(+a.id)>-1;
      return '<div class="acard"><img src="'+a.img+'" alt="" loading="lazy">'+
        '<div class="acard-n">'+esc(dec(a.name))+'</div>'+
        '<div class="acard-p">‏₪'+(+a.price).toLocaleString('en-US')+'</div>'+
        '<button class="aadd'+(has?' done':'')+'" type="button" data-aid="'+a.id+'"'+(has?' disabled':'')+
        ' aria-label="הוספה לסל">'+(has?'✓':'+')+'</button></div>';
    }).join('');
  }
  /* רצועת הכניסה למסך התוספות — מוצגת רק במובייל (CSS) ורק כשיש תוספות */
  function cta(n){
    var foot=document.querySelector('#cartDrawer .cart-foot'); if(!foot) return;
    var b=document.getElementById('gmSideCta');
    if(!n){ if(b) b.remove(); return; }
    if(!b){ b=document.createElement('button'); b.id='gmSideCta'; b.type='button'; b.className='gm-side-cta';
      foot.insertAdjacentElement('afterbegin', b); }
    b.innerHTML='🚀 שווה להוסיף עכשיו <i>'+n+' אביזרים ›</i>';
  }
  function sideOpen(on){
    var d=document.getElementById('cartDrawer'); if(!d) return;
    d.classList.toggle('side-open', !!on);
    var back=document.getElementById('cartSideBack'); if(back) back.hidden=!on;
    var side=document.getElementById('cartSide'), foot=document.querySelector('#cartDrawer .cart-foot');
    if(side&&foot&&window.matchMedia('(max-width:820px)').matches){
      side.style.bottom = on ? (foot.getBoundingClientRect().height+'px') : '';
    } else if(side){ side.style.bottom=''; }
  }
  function gcBtn(pid,plan,price,title,sub,added){
    return '<button class="gcopt'+(added?' added':'')+'" type="button" data-gc="'+plan+'" data-pid="'+pid+
      '" data-price="'+price+'">'+SHIELD+'<span class="gcopt-t">'+(added?'':'הוספת ')+title+
      '<span>'+sub+'</span></span><span class="gcopt-p">+‏₪'+(+price).toLocaleString('en-US')+'</span></button>';
  }
  function gcRender(map, items){
    var addedTxt=items.filter(function(it){return /green ?care/i.test(dec(it.name||''));})
      .map(function(it){ var extra=(it.item_data||[]).map(function(m){return m.value;}).join(' ');
        return (dec(it.name||'')+' '+extra).toLowerCase(); });
    document.querySelectorAll('#cartDrawer .citem').forEach(function(ci){
      var nx=ci.nextElementSibling;
      while(nx && (nx.classList.contains('gcopts')||nx.classList.contains('gcmore'))){ var k=nx.nextElementSibling; nx.remove(); nx=k; }
      var key=ci.getAttribute('data-key'), pid=ci.getAttribute('data-pid');
      if(!pid){ var m=items.filter(function(it){return it.key===key;})[0]; pid=m?String(m.id):''; }
      var conf=map[pid]; if(!conf) return;
      var nm=(conf.name||'').toLowerCase().slice(0,14);
      var isAdded=!!nm && addedTxt.some(function(t){return t.indexOf(nm)>-1;});
      var rows='';
      if(conf.tiers&&+conf.tiers.gc&&+conf.prices.gc>0) rows+=gcBtn(conf.pid||pid,'gc',conf.prices.gc,'Green Care','אחריות שנה שנייה מלאה',isAdded);
      if(conf.tiers&&+conf.tiers.gcp&&+conf.prices.gcp>0) rows+=gcBtn(conf.pid||pid,'gcp',conf.prices.gcp,'Green Care <b>+</b>','24 חודשים, כולל שברים ונזקי נוזלים',isAdded);
      if(!rows) return;
      var wrap=document.createElement('div'); wrap.className='gcopts'; wrap.innerHTML=rows;
      ci.insertAdjacentElement('afterend', wrap);
      var a=document.createElement('a'); a.className='gcmore'; a.href='/green-care/';
      a.innerHTML='מה כלול בכל מסלול? <u>לפרטים המלאים</u>';
      wrap.insertAdjacentElement('afterend', a);
    });
  }
  function sync(){
    var d=document.getElementById('cartDrawer'); if(!d) return;
    style(); upgrade(d);
    get().then(function(c){
      var items=(c&&c.items)||[], ids=items.map(function(it){return it.id;}).filter(Boolean);
      var inCart=ids.map(Number);
      if(!ids.length){ rail([],inCart); return; }
      fetch('/wp-json/gm-addons/v1/for-cart?ids='+ids.join(','),{credentials:'same-origin'})
        .then(function(r){return r.json();}).then(function(x){ rail(((x&&x.items)||[]), inCart); })
        .catch(function(){ rail([],inCart); });
      fetch('/wp-json/gm-services/v1/greencare?ids='+ids.join(','),{credentials:'same-origin'})
        .then(function(r){return r.json();}).then(function(x){ gcRender((x&&x.items)||{}, items); })
        .catch(function(){});
    });
  }
  function later(ms){ clearTimeout(tmr); tmr=setTimeout(sync, ms||400); }
  document.addEventListener('click', function(e){
    var t=e.target;
    if(t.closest('.cart-pill,.mcart,a.card-btn,.gm-atc')) { sideOpen(false); later(600); return; }
    if(t.closest('#cartClose,.cart-overlay')) { sideOpen(false); return; }
    if(t.closest('#gmSideCta')){ e.preventDefault(); sideOpen(true); return; }
    if(t.closest('#cartSideBack')){ e.preventDefault(); sideOpen(false); return; }
    if(t.closest('#cartDrawer .citem-rm') || t.closest('#cartDrawer .cqty button')){ later(500); }
    var aa=t.closest('.aadd');
    if(aa && !aa.disabled){ e.preventDefault(); e.stopPropagation();
      var id=+aa.getAttribute('data-aid'); if(!id) return;
      aa.disabled=true; aa.textContent='…';
      add(id).then(function(){ later(120); }).catch(function(){ aa.disabled=false; aa.textContent='+'; });
      return; }
    var gc=t.closest('.gcopt');
    if(gc && !gc.classList.contains('added')){ e.preventDefault(); e.stopPropagation();
      var fd=new FormData(); fd.append('action','gm_svc_greencare');
      fd.append('plan',gc.getAttribute('data-gc'));
      fd.append('product_id',gc.getAttribute('data-pid'));
      fd.append('price',gc.getAttribute('data-price'));
      gc.style.opacity='.6';
      fetch('/wp-admin/admin-ajax.php',{method:'POST',credentials:'same-origin',body:fd})
        .then(function(){ gc.style.opacity=''; later(150); })
        .catch(function(){ gc.style.opacity=''; });
      return; }
  }, true);
  function watch(){
    var it=document.getElementById('cartItems'); if(!it||it.__gmw) return; it.__gmw=1;
    /* כל שינוי ברשימת הפריטים (הוספה/הסרה/ריקון) מפעיל סנכרון — כך שהפס
       נעלם ברגע שהסל מתרוקן, וגם מתעדכן כשמוסיפים מוצר. (אסי, 09/08) */
    new MutationObserver(function(){ later(250); }).observe(it,{childList:true,subtree:true});
  }
  function boot(){ style(); var d=document.getElementById('cartDrawer'); if(d){ upgrade(d); watch(); } later(900); }
  if(document.readyState!=='loading') boot(); else document.addEventListener('DOMContentLoaded', boot);
  setTimeout(function(){ var d=document.getElementById('cartDrawer'); if(d){ upgrade(d); watch(); } }, 1500);
})();
