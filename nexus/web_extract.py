"""网页 URL 抓取与正文提取（SSRF 防护标准原语）。

消费方：suki（知识导入）、lyra（ingest-url）。收编前两仓各自实现，lyra 版
follow_redirects=True 只在首跳做 IP 校验，存在 302 跳内网风险——本原语手动
逐跳跟随重定向，每一跳都重新做公网 IP 校验。

安全边界：仅公网 http/https；DNS 解析后校验目标 IP，拒绝私网/环回/链路本地。
提取链：trafilatura 正文提取，缺失时降级为标签剥离纯文本。
不可信边界：出口文本默认过 nexus.untrusted.sanitize_untrusted（剥离 Unicode Tag
外泄通道/零宽/控制字符，旗标打日志）；进 LLM 前再由消费方用 wrap_untrusted
包裹并配 UNTRUSTED_CONTENT_RULE。
"""
import asyncio
import ipaddress
import logging
import re
from urllib.parse import urlparse

import httpx

from nexus.untrusted import sanitize_untrusted

logger = logging.getLogger(__name__)

MAX_HTML_BYTES = 2 * 1024 * 1024
FETCH_TIMEOUT = 15.0
MAX_REDIRECT_HOPS = 4
USER_AGENT = "SukiKnowledgeBot/1.0 (+https://songguokr.com/suki; knowledge-import)"


class WebExtractError(Exception):
    """抓取/提取失败，message 面向用户可直接展示。"""


def _validate_url_scheme(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise WebExtractError("仅支持 http/https 链接")
    if not parsed.hostname:
        raise WebExtractError("链接缺少域名")


def _assert_public_ip(ip_text: str, host: str) -> None:
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError as exc:
        raise WebExtractError(f"域名 {host} 无法解析") from exc
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        raise WebExtractError("不允许导入内网地址")


async def resolve_and_check(url: str) -> None:
    """DNS 解析并校验目标 IP 全部为公网地址（重定向后必须重新调用）。"""
    _validate_url_scheme(url)
    host = urlparse(url).hostname
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None)
    except OSError as exc:
        raise WebExtractError(f"域名 {host} 无法解析") from exc
    for info in infos:
        _assert_public_ip(info[4][0], host)


def _strip_tags_fallback(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|svg|iframe)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", "\n", html)
    lines = [ln.strip() for ln in html.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _sanitize(text: str) -> str:
    cleaned, flags = sanitize_untrusted(text)
    if flags:
        logger.warning("untrusted content hygiene flags: %s", ",".join(flags))
    return cleaned


def extract_content(html: str, url: str) -> tuple[str, str]:
    """返回 (title, 正文)。正文过短视为提取失败；出口文本已经不可信卫生清洗。"""
    title = ""
    try:
        import trafilatura

        downloaded = trafilatura.extract(
            html, url=url, include_links=False, include_images=False,
            include_tables=True, favor_recall=True,
        )
        metadata = trafilatura.extract_metadata(html)
        if metadata and metadata.title:
            title = metadata.title.strip()[:300]
        if downloaded and len(downloaded.strip()) >= 100:
            return _sanitize(title), _sanitize(downloaded.strip())
    except ImportError:
        logger.warning("trafilatura 未安装，使用降级正文提取")
    except Exception as e:
        logger.warning("trafilatura extract failed: %s", e)
    if not title:
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
        if m:
            title = m.group(1).strip()[:300]
    text = _strip_tags_fallback(html)
    if len(text) < 100:
        raise WebExtractError("未能从该网页提取到有效正文")
    return _sanitize(title), _sanitize(text)


async def fetch_page_text(url: str) -> tuple[str, str]:
    """抓取并提取网页，返回 (title, content)。失败抛 WebExtractError。

    重定向手动逐跳跟随，每一跳都重新做公网 IP 校验（防 302 跳内网）。
    """
    current = url
    for _hop in range(MAX_REDIRECT_HOPS):
        await resolve_and_check(current)
        try:
            async with httpx.AsyncClient(
                timeout=FETCH_TIMEOUT, follow_redirects=False,
                headers={"User-Agent": USER_AGENT},
            ) as client:
                resp = await client.get(current)
        except httpx.TimeoutException as exc:
            raise WebExtractError("网页抓取超时（15 秒），可稍后重试或改用文件导入") from exc
        except httpx.HTTPError as e:
            raise WebExtractError(f"网页无法访问：{type(e).__name__}") from e
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("location", "")
            if not loc:
                raise WebExtractError("网页重定向缺少目标地址")
            current = str(httpx.URL(current).join(loc))
            continue
        break
    if resp.status_code != 200:
        raise WebExtractError(f"网页返回 {resp.status_code}，无法导入（受限站点请改用文件导入）")
    if "content-type" in resp.headers and not any(
        ct in resp.headers["content-type"].lower()
        for ct in ("text/html", "text/plain", "application/xhtml")
    ):
        raise WebExtractError("该链接不是网页（仅支持 HTML 页面）")
    html = resp.text[:MAX_HTML_BYTES]
    return extract_content(html, str(resp.url))
