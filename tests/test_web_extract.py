"""nexus.web_extract 原语单测：SSRF 拒绝面 / 逐跳重定向校验契约。"""
import pytest

from nexus.web_extract import WebExtractError, extract_content, resolve_and_check


@pytest.mark.asyncio
async def test_reject_non_http_scheme():
    with pytest.raises(WebExtractError, match="http/https"):
        await resolve_and_check("file:///etc/passwd")


@pytest.mark.asyncio
async def test_reject_private_ip():
    with pytest.raises(WebExtractError, match="内网"):
        await resolve_and_check("http://10.100.0.1:9527/config")


@pytest.mark.asyncio
async def test_reject_loopback():
    with pytest.raises(WebExtractError, match="内网"):
        await resolve_and_check("http://127.0.0.1:8000/")


@pytest.mark.asyncio
async def test_reject_unresolvable():
    with pytest.raises(WebExtractError, match="无法解析"):
        await resolve_and_check("http://this-host-does-not-exist.invalid/")


def test_extract_content_fallback_strips_tags():
    html = "<html><head><title>测试页</title></head><body><script>bad()</script>" + \
        "<p>" + "正文内容" * 40 + "</p></body></html>"
    title, text = extract_content(html, "http://example.com/a")
    assert title == "测试页"
    assert "正文内容" in text and "bad()" not in text


def test_extract_content_raises_when_too_short():
    with pytest.raises(WebExtractError, match="有效正文"):
        extract_content("<html><body>hi</body></html>", "http://example.com/")
