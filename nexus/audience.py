"""触达受众净化原语：孤儿号/测试号/停用号不再进入通知圈定（UC 三态单源）。

challengePlanet r21 同构判例的收归（reminder_guard 首创，gezhi r25 第二消费方）：
业务库里的 user_id 可以是历轮 E2E 探针号（UC 已打 is_test）或孤儿号（UC 侧已清理
而业务规则还在 active）——challengePlanet 生产实锤 106 条通知发 20 人 0 已读、
19/20 是孤儿号；gezhi 生产实锤 133 条通知 0 已读、~94% 发给探针/孤儿号。
「这个用户还值得触达吗」的判定单源是 UC /api/internal/users/status 的
exists/is_test/disabled 三态，业务侧不自建黑名单。

语义：
- sendable_users(ids) 返回可触达子集；UC 不可达时 fail-open（warning 留痕）——
  UC 不可达时 notifyCenter 大概率同样不可达，噪音发不出去，不为净化引入新故障面。
- 响应成功才按状态过滤；空 dict 也是权威「全不存在」（全滤）。
- 缓存按 id 记 (expire_at, sendable)，TTL 300s；REMINDER_GUARD_OFF=1 整体旁路
  （E2E/本地真链对测试号断言通知行为时使用）。

环境：UC__BASE_URL 缺失=无法判定，fail-open 并 warning（与 #372 env 生效性实证
纪律一致：缺配置必须可见，不许装作在工作）。
"""

from __future__ import annotations

import os
import time

import httpx

from nexus.logging import get_logger
from nexus.service_client import get_service_client

logger = get_logger("nexus.audience")

CACHE_TTL_SECONDS = 300
_status_cache: dict[str, tuple[float, bool]] = {}

__all__ = ["sendable_users", "sendable", "guard_enabled", "CACHE_TTL_SECONDS"]


def guard_enabled() -> bool:
    return os.getenv("REMINDER_GUARD_OFF", "") != "1"


def _sendable(status: dict[str, object]) -> bool:
    return (
        bool(status.get("exists"))
        and not bool(status.get("is_test"))
        and not bool(status.get("disabled"))
    )


async def sendable(user_id: str) -> bool:
    """单用户便捷判定：True=可触达。"""
    return user_id in await sendable_users([user_id])


async def sendable_users(user_ids) -> set[str]:
    """返回可触达的用户集合（语义详见模块 docstring）。"""
    ids = sorted({str(u) for u in user_ids if str(u)})
    if not ids or not guard_enabled():
        return set(ids)
    now = time.monotonic()
    out: set[str] = set()
    unknown: list[str] = []
    for uid in ids:
        cached = _status_cache.get(uid)
        if cached is not None and cached[0] > now:
            if cached[1]:
                out.add(uid)
        else:
            unknown.append(uid)
    if unknown:
        statuses = await _fetch_statuses(unknown)
        if statuses is None:
            for uid in unknown:
                _status_cache[uid] = (now + CACHE_TTL_SECONDS, True)
                out.add(uid)
        else:
            for uid in unknown:
                ok = _sendable(statuses.get(uid, {}))
                _status_cache[uid] = (now + CACHE_TTL_SECONDS, ok)
                if ok:
                    out.add(uid)
    return out


async def _fetch_statuses(ids: list[str]) -> dict[str, dict[str, object]] | None:
    """None=查询失败（调用方 fail-open）；dict=权威状态（空 dict 也是权威「全不存在」）。"""
    client = get_service_client()
    base_url = os.getenv("UC__BASE_URL", "").rstrip("/")
    if not base_url:
        logger.warning("UC__BASE_URL unset — user status guard skipped")
        return None
    try:
        from nexus.trace import outbound_headers

        headers = outbound_headers(await client.header())
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0)) as http:
            resp = await http.get(
                f"{base_url}/api/internal/users/status",
                params={"ids": ",".join(ids[:200])},
                headers=headers,
            )
            resp.raise_for_status()
            data = (resp.json() or {}).get("data") or {}
            users = data.get("users")
            return dict(users) if isinstance(users, dict) else {}
    except Exception as e:
        logger.warning(
            "user status fetch failed (%s) — fail-open for %d ids", e, len(ids),
        )
        return None
