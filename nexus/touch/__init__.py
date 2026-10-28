"""触达诚实化原语：内容指纹 + 触达决策状态机（纯函数，零 IO）。

五仓同构判例的收归（codeBlock r17 增量指纹 / goldenStock r19 幽灵受众守卫 /
suki r9 增量判据+revive 续航 / geniusStudent r15 内容指纹聚合 / goldenFish r19
改期票据）：同一内容换个说法重发=对用户骚扰且对漏斗自欺，静默是对单次广播的
卫生而非对用户关系的了断（静默满 revive_days 给一次行动导向重触，满 sunset_days
诚实退场）。

决策语义（decide_touch）：
- 首次触达 / 内容身份变化 -> SEND
- 同内容且不足 revive_days -> SILENCE（没有新话，静默优于重发）
- 同内容且满 revive_days -> REVIVE（允许重发一次，文案用 revive 变体「门开着」，
  指纹保持内容身份不变——revive 文案不是新内容）
- 同内容且 max(last_sent, user_last_active) 距今满 sunset_days -> SUNSET
  （用户长期未回流且无新话，诚实退场；内容变了则不受落日限制，有新话照发）
- 存在历史触达但无指纹（历史行平滑切进守卫）-> 视为首次放行一条

未读退避（unread_streak + backoff_schedule，1.106.0）：
周期触达（周报/周灵感/月度盘点）按固定节拍重发，对不读的用户是结构性噪音：
travelMate 周灵感 3 天 0 已读仍每 7 天一发。调用方回传「连续未读次数」
（read-status 证据缺失时不计，宁宽勿冤），频率下限取 max(min_interval, 档位值)，
REVIVE 门槛同步放大——连击越深节拍越稀，读一次即清零回常态。退避拦在
min_interval 位（先于内容变化判定）：周期选品类触达每期内容天然不同
（新指纹），只拦同内容等于退避永远被 content_changed 旁路——「你不读，
我就连新话也少说」才是退避的产品语义；确有强时效内容走独立高优通道，
不与周期灵感共用节拍。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

__all__ = ["TouchAction", "TouchDecision", "fingerprint", "decide_touch", "revive_delta"]


class TouchAction(str, Enum):
    SEND = "send"
    SILENCE = "silence_same_content"
    SILENCE_TOO_SOON = "silence_too_soon"
    REVIVE = "revive"
    SUNSET = "sunset"


@dataclass(frozen=True)
class TouchDecision:
    action: TouchAction
    reason: str

    @property
    def allowed(self) -> bool:
        return self.action in (TouchAction.SEND, TouchAction.REVIVE)


def fingerprint(*parts: object) -> str:
    """内容身份指纹：空值剔除后规范化拼串，md5 前 16 位。

    锚「内容身份」而非「文案措辞」——模板换措辞、字段顺序变化不产生新指纹；
    业务状态变化（层级/形态/参数/数字）自然产生新指纹。
    """
    normalized = "|".join(str(p).strip() for p in parts if str(p or "").strip())
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()[:16]


def decide_touch(
    *,
    new_fingerprint: str,
    now: datetime,
    last_fingerprint: str | None = None,
    last_sent_at: datetime | None = None,
    min_interval: timedelta | None = None,
    revive_days: int = 7,
    sunset_days: int = 30,
    user_last_active_at: datetime | None = None,
    unread_streak: int = 0,
    backoff_schedule: tuple[timedelta, ...] | None = None,
) -> TouchDecision:
    """触达决策。now 由调用方传入（纯函数，不读墙钟）。

    user_last_active_at：用户最近一次真实写行为时间，仅参与 sunset 锚点——
    触达后用户回流过则落日计时顺延（活跃用户不会被落日）；同内容仍静默
    （回流但内容无增量 ≠ 重发同句的理由）。

    unread_streak：连续未读次数（上次已读/首次=0；read-status 证据缺失时
    调用方不计数，宁宽勿冤）。backoff_schedule：未读退避间隔档位表，第 N 次
    连击取第 min(N, len) 档；频率下限 = max(min_interval, 档位值)，REVIVE
    门槛同步放大——先于内容变化判定（周期选品每期新指纹，不拦新内容则
    退避恒被旁路），读一次即回常态。
    """
    if not last_sent_at:
        return TouchDecision(TouchAction.SEND, "first_touch")
    backoff = timedelta(0)
    if unread_streak > 0 and backoff_schedule:
        backoff = backoff_schedule[min(unread_streak, len(backoff_schedule)) - 1]
    if min_interval and now - last_sent_at < max(min_interval, backoff):
        reason = "within_min_interval" + (f"_unread_backoff_{unread_streak}" if backoff else "")
        return TouchDecision(TouchAction.SILENCE_TOO_SOON, reason)
    if last_fingerprint and new_fingerprint != last_fingerprint:
        return TouchDecision(TouchAction.SEND, "content_changed")
    if not last_fingerprint:
        return TouchDecision(TouchAction.SEND, "no_prior_fingerprint")

    anchor = last_sent_at
    if user_last_active_at and user_last_active_at > anchor:
        anchor = user_last_active_at
    if now - anchor > timedelta(days=sunset_days):
        return TouchDecision(TouchAction.SUNSET, f"no_response_over_{sunset_days}d")
    if now - last_sent_at >= max(timedelta(days=revive_days), backoff):
        return TouchDecision(
            TouchAction.REVIVE,
            f"same_content_over_{revive_days}d" + (f"_unread_backoff_{unread_streak}" if backoff else ""),
        )
    return TouchDecision(TouchAction.SILENCE, "same_content")


def revive_delta(*, now: datetime, last_sent_at: datetime | None, revive_days: int = 7) -> bool:
    """是否处于 revive 窗口语义的便捷判定（供文案渲染分支用，不发决策）。"""
    if not last_sent_at:
        return False
    return now - last_sent_at >= timedelta(days=revive_days)
