"""签名直达页原语（1.127.0）：免登录、可收藏、不可枚举的静态页机制层。

r31 定谳（nexus-agent 晨报归档）：in_app 通道结构性熄灭（全舰队 565 发 0 读），
签名网页是唯一不需要用户先回来的到达面——浏览器直达、零登录、可收藏。
本模块从 nexus-agent brief_archive（r31 首实现）提炼机制层，lyra 周报页
（第二个消费方）同轮接线。

机制层（本模块职责）：secret 三级管理 / HMAC 签名与校验 / slug 合法性 /
原子落盘 / 已归档路径与公网 URL 单源。
内容层（业务仓自持，不下沉）：HTML 渲染与推送正文是影子契约（前 7 教训：
归档渲染器必须与推送形态同轮演进），渲染语义各业务不同。

哲学（wanxiang r2 → r31 同源）：同 slug 恒同 sig（HMAC-SHA256 截断，不可
枚举）→ URL 是产权不是租约，收藏一个 slug 形态即可复用；私人内容决定
不能全公开——签名页=「免登录」而非「公开」；错 sig 与未归档一律同文案
404（区分两种缺席=给探测者递梯子）。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
from typing import Optional

__all__ = [
    "page_secret",
    "page_sig",
    "verify_page_sig",
    "valid_slug",
    "archive_page",
    "page_path",
    "page_url",
]


def page_secret(env_key: str, secret_file: str, fallback: str = "") -> str:
    """签名 secret 三级来源（wanxiang r2 哲学）：显式 env > 首用自生成文件 > 兜底串。

    自生成文件 0600、跨重启稳定——轮换会打断已收藏 URL，档案语义要求永续。
    secret 本就不该可预知：部署环境未配 secret 时「按兜底形态猜 sig 打生产
    404」不是故障，是防探测语义的正确表现。
    """
    env = os.getenv(env_key, "").strip()
    if env:
        return env
    try:
        with open(secret_file, "r", encoding="utf-8") as f:
            saved = f.read().strip()
        if saved:
            return saved
    except OSError:
        pass
    token = os.getenv("SERVICE_TOKEN", "").strip()
    if token:
        try:
            os.makedirs(os.path.dirname(secret_file) or ".", exist_ok=True)
            fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(token)
        except OSError:
            pass
        return token
    return fallback


def page_sig(secret: str, namespace: str, slug: str) -> str:
    """同 slug 恒同 sig；namespace 隔离同仓不同页族（brief/pulse/...）。"""
    payload = f"{namespace}:{slug}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()[:16]


def verify_page_sig(secret: str, namespace: str, slug: str, sig: str) -> bool:
    return bool(sig) and hmac.compare_digest(page_sig(secret, namespace, slug), sig)


_SLUG_RE_DEFAULT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def valid_slug(slug: str, pattern: Optional[re.Pattern[str]] = None) -> bool:
    """slug 合法性（防路径穿越/注入）；业务可用自定义正则（如日期形态）。"""
    rule = pattern or _SLUG_RE_DEFAULT
    return bool(slug) and rule.match(slug) is not None


def archive_page(page_dir: str, slug: str, html: str) -> Optional[str]:
    """推送终态落盘：tmp+os.replace 原子写（半截 HTML 不可 serve）；失败 None
    诚实降级（调用方不带链接），绝不阻断通知主链（r31 教训#879）。"""
    if not (html or "").strip():
        return None
    try:
        os.makedirs(page_dir, exist_ok=True)
        path = os.path.join(page_dir, f"{slug}.html")
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(html)
        os.replace(tmp, path)
        return path
    except OSError:
        return None


def page_path(page_dir: str, slug: str) -> Optional[str]:
    """已归档文件路径；未归档返回 None（调用方 404 诚实态）。"""
    path = os.path.join(page_dir, f"{slug}.html")
    return path if os.path.exists(path) else None


def page_url(base: str, slug: str, sig: str) -> str:
    """公网签名直达 URL（通知链接与回看行的单一来源）。"""
    return f"{base.rstrip('/')}/{slug}?sig={sig}"
