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
  function ensure(){
    if(document.getElementById('cartDrawer')) return;
    var w=document.createElement('div');
    w.innerHTML=''+
      '<div class="cart-overlay" id="cartOverlay"></div>'+
      '<aside class="cart-drawer" id="cartDrawer" aria-label="עגלת הקניות">'+
      '<div class="cart-head"><strong>הסל שלי (<span class="cart-count-n">0</span>)</strong>'+
      '<button class="mclose" id="cartClose" aria-label="סגור">×</button></div>'+
      '<div class="cart-ship" id="cartShip"></div>'+
      '<div class="cart-items" id="cartItems"></div>'+
      '<div class="cart-foot"><div class="cart-subtotal"><span>סכום ביניים</span><span class="cs-amt" id="cartSubtotal">‏₪0</span></div>'+
      '<div class="cart-note">המשלוח מחושב בעמוד התשלום</div>'+
      '<a class="cart-checkout" href="/מעבר-לתשלום/">מעבר לתשלום</a></div></aside>';
    while(w.firstChild) document.body.appendChild(w.firstChild);
    document.getElementById('cartOverlay').addEventListener('click',close);
    document.getElementById('cartClose').addEventListener('click',close);
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
      return '<div class="citem" data-key="'+it.key+'"><img class="citem-img" src="'+img+'" alt="">'+
        '<div class="citem-main"><div class="citem-nm">'+esc(it.name)+'</div>'+
        (vv?'<div class="citem-var">'+esc(vv)+'</div>':'')+
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
  });
  function init(){ ensure(); get().then(render); }
  if(document.readyState!=='loading') init(); else document.addEventListener('DOMContentLoaded',init);
})();
