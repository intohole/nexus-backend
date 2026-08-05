from __future__ import annotations

from typing import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

_SPLASH_TEMPLATE = """
<style id="__nexus_splash_style__">
#__nexus_splash__{position:fixed;inset:0;z-index:2147483647;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:18px;background:var(--nexus-splash-bg,#f8fafc);color:#0f172a;font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;transition:opacity .35s ease}
html[data-theme="dark"] #__nexus_splash__{background:#0f172a;color:#e2e8f0}
#__nexus_splash__ .nexus-spinner{width:42px;height:42px;border-radius:50%;border:3px solid var(--nexus-splash-ring,rgba(var(--app-accent-rgb,15,118,110),.18));border-top-color:var(--app-accent,#059669);animation:nexus-spin .8s linear infinite}
#__nexus_splash__ .nexus-brand{font-size:16px;font-weight:600}
#__nexus_splash__ .nexus-hint{font-size:12px;opacity:.55}
@keyframes nexus-spin{to{transform:rotate(360deg)}}
</style>
<div id="__nexus_splash__" role="status" aria-live="polite">
  <div class="nexus-spinner" aria-hidden="true"></div>
  <div class="nexus-brand">__APP_NAME__</div>
  <div class="nexus-hint">正在加载应用，请稍候…</div>
</div>
<script>
(function(){var el=document.getElementById("__nexus_splash__");function done(){if(!el||el.classList.contains("is-done"))return;el.classList.add("is-done");setTimeout(function(){var p=el.parentNode;if(p)p.removeChild(el)},400)}window.NexusSplash={done:done};var t=null;function fallback(){if(t)clearTimeout(t);done()}if(document.readyState==="complete"){t=setTimeout(done,600)}else{window.addEventListener("load",function(){t=setTimeout(done,400)})}t=setTimeout(done,15000);var app=document.getElementById("app");if(app&&window.MutationObserver){var mo=new MutationObserver(function(){if(app.childNodes.length){clearTimeout(t);done();mo.disconnect()}});mo.observe(app,{childList:true,subtree:true});window.addEventListener("click",function listener(){if(app.childNodes.length){window.removeEventListener("click",listener);fallback()}})}window.NexusSplashReady=true})();
</script>
"""


def build_splash(app_name: str) -> str:
    return _SPLASH_TEMPLATE.replace("__APP_NAME__", app_name or "应用")


def _inject_splash(html: str, splash: str) -> str:
    idx: int = html.find("<body")
    if idx == -1:
        return splash + html
    end: int = html.find(">", idx)
    if end == -1:
        return splash + html
    return html[: end + 1] + splash + html[end + 1 :]


class LoadingSplashMiddleware(BaseHTTPMiddleware):
    """向 text/html 响应注入品牌加载动画，替换 v-cloak 白屏。

    内联注入零网络请求，Vue 挂载到 #app 或 window.load 后自动淡出移除。
    全部页面复用，通过 setup_middleware(enable_loading_splash=True) 统一开启。
    """

    def __init__(
        self,
        app,
        app_name: str = "应用",
        enabled: bool = True,
    ) -> None:
        super().__init__(app)
        self._splash: str = build_splash(app_name) if enabled else ""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response: Response = await call_next(request)
        if not self._splash:
            return response
        ctype: str = response.headers.get("content-type", "")
        if "text/html" not in ctype:
            return response
        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        text: str = body.decode("utf-8", errors="replace")
        if "<body" in text:
            text = _inject_splash(text, self._splash)
        new_headers: dict[str, str] = dict(response.headers)
        encoded: bytes = text.encode("utf-8")
        new_headers["content-length"] = str(len(encoded))
        return Response(content=encoded, status_code=response.status_code, headers=new_headers)