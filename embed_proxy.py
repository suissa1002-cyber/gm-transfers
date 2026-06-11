"""
embed_proxy — reverse-proxy להטמעת אפליקציות סוכנים ב-GreenOS *native* (לא iframe).
כל אפליקציה מוגשת תחת /embed/<key>; ה-proxy מושך מה-upstream בצד השרת, מזריק טוקן
upstream אם צריך, ומשכתב נתיבים אבסולוטיים (/api//static/) לקידומת — כך הדפדפן
מדבר רק עם GreenOS (אותו origin), בלי CORS ובלי חשיפת טוקנים.
"""

import os
import re

import requests

# key → (base_url, upstream_bearer_env)
APPS = {
    "inv": ("https://invoice-manager-tfqj.onrender.com", None),
    "sw":  ("https://uri-stock-watcher.onrender.com", "STOCK_WATCHER_TOKEN"),
}
_TEXT = ("text/html", "application/javascript", "text/javascript",
         "text/css", "application/json")
_HOP = {"content-encoding", "content-length", "transfer-encoding", "connection",
        "keep-alive", "set-cookie", "strict-transport-security"}

BACK_BTN = (
    '<a href="/" style="position:fixed;bottom:16px;inset-inline-start:16px;z-index:99999;'
    'background:#1f2440;color:#fff;text-decoration:none;padding:10px 16px;border-radius:12px;'
    'font-weight:700;box-shadow:0 6px 20px rgba(0,0,0,.25);font-family:system-ui">⬅ GreenOS</a>'
)

# תיקוני מובייל מוזרקים ל-<head> של כל אפליקציה מוטמעת (בלי לגעת ב-repo שלה)
MOBILE_CSS = {
    "inv": """<style id="gos-mobile">@media (max-width:768px){
      body{font-size:14px;padding-bottom:74px!important}
      h1,.title{font-size:18px!important}
      /* כל עוטף עם overflow-hidden (Tailwind) → גלילה אופקית במקום חיתוך */
      [class*="overflow-hidden"],[class*="overflow-x"]{overflow-x:auto!important;-webkit-overflow-scrolling:touch}
      table{font-size:12px!important}
      table th,table td{padding:6px 5px!important;white-space:nowrap}
      .grid{grid-template-columns:1fr 1fr!important}
    }</style>""",
    "sw": """<style id="gos-mobile">@media (max-width:768px){
      body{margin:16px auto!important;padding:0 12px!important;font-size:14px}
      h1{font-size:20px} table{font-size:12.5px;display:block;overflow-x:auto;white-space:nowrap}
      body{padding-bottom:70px!important}
    }</style>""",
}


def app_for(key):
    return APPS.get(key)


def _rewrite(text: str, key: str, is_html: bool) -> str:
    pref = f"/embed/{key}"
    # נתיבים אבסולוטיים פנימיים בלבד (לא https:// חיצוני) → תחת הקידומת
    for q in ('"', "'", "`", "(", "="):
        text = text.replace(f'{q}/api/', f'{q}{pref}/api/')
        text = text.replace(f'{q}/static/', f'{q}{pref}/static/')
    if is_html:
        css = MOBILE_CSS.get(key, "")
        vp = ('<meta name="viewport" content="width=device-width, initial-scale=1">'
              if "viewport" not in text else "")
        if "</head>" in text:
            text = text.replace("</head>", vp + css + "</head>", 1)
        elif "<body" in text:
            text = re.sub(r"(<body[^>]*>)", r"\1" + vp + css, text, count=1)
        # כפתור חזרה צף ל-GreenOS (לא סרגל כהה)
        if "</body>" in text:
            text = text.replace("</body>", BACK_BTN + "</body>", 1)
    return text


def proxy(key: str, path: str, method: str, query: str,
          body: bytes, content_type: str):
    base, tok_env = APPS[key]
    url = f"{base}/{path}" if path else base
    if query:
        url += "?" + query
    headers = {}
    if tok_env and os.getenv(tok_env):
        headers["Authorization"] = f"Bearer {os.getenv(tok_env)}"
    if content_type:
        headers["Content-Type"] = content_type
    r = requests.request(method, url, data=body or None, headers=headers,
                         timeout=45, allow_redirects=False)
    ct = r.headers.get("content-type", "")
    out_headers = {k: v for k, v in r.headers.items() if k.lower() not in _HOP}
    is_text = any(t in ct for t in _TEXT)
    if is_text:
        content = _rewrite(r.text, key, "text/html" in ct).encode("utf-8")
    else:
        content = r.content
    return r.status_code, content, ct, out_headers
