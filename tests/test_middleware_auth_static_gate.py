"""ServiceAuthMiddleware 放行面收窄守底（S200·r243 差距 8）。

差距 8：静态后缀曾在任意路径放行（/api/x.png 穿透），/api/discovery 宽前缀
无精确枚举。修复=后缀放行限定静态路由前缀内（DEFAULT_STATIC_ROUTE_PREFIXES，
构造参数可覆盖）+/api/discovery 从默认前缀移除（挂载 3 仓零业务依赖普查）。
"""
from __future__ import annotations

from nexus.middleware_auth import (
    DEFAULT_PUBLIC_API_PREFIXES,
    ServiceAuthMiddleware,
)


def _mw(**kwargs: object) -> ServiceAuthMiddleware:
    return ServiceAuthMiddleware(app=None, **kwargs)


def test_static_suffix_outside_static_route_blocked() -> None:
    mw = _mw()
    assert not mw._is_public("/api/private/notes.html")
    assert not mw._is_public("/api/v1/export.json")


def test_static_suffix_inside_static_route_allowed() -> None:
    mw = _mw()
    assert mw._is_public("/assets/app.js")
    assert mw._is_public("/assets/img/logo.png")
    assert mw._is_public("/static/vendor.js")
    assert mw._is_public("/static/sub/deep.woff2")


def test_static_route_prefix_subpath_exact_not_suffix_only() -> None:
    mw = _mw()
    assert not mw._is_public("/assetsx/app.js")


def test_discovery_wide_prefix_removed() -> None:
    assert all(p != "/api/discovery" for p in DEFAULT_PUBLIC_API_PREFIXES)
    mw = _mw()
    assert not mw._is_public("/api/discovery/anything")
    assert not mw._is_public("/api/discovery")


def test_existing_public_prefixes_no_regression() -> None:
    mw = _mw()
    assert mw._is_public("/api/auth/login")
    assert mw._is_public("/api/auth/login/x")
    assert mw._is_public("/.well-known/apple-app-site-association")
    assert mw._is_public("/health")
    assert mw._is_public("/")


def test_custom_static_route_prefixes_override() -> None:
    mw = _mw(static_route_prefixes=["/custom-assets"])
    assert mw._is_public("/custom-assets/a.png")
    assert not mw._is_public("/assets/a.png")


def test_whitelist_precedence_unchanged() -> None:
    mw = _mw(whitelist_paths=["/special.html"])
    assert mw._is_public("/special.html")
    assert mw._is_public("/special.html/sub")
