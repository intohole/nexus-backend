"""nexus HarnessCapabilities：r260 B1 薄壳化定谳——装配核上收 ironman.kernel（contracts.md §5）。

零外部消费判定（r260 判定书：全仓 grep 仅 agent_harness.py 一处 import）→
本名转 alias 保留一个发版周期（M8 纪律），下轮退役窗撤档。
nexus 侧真实缝=agent_harness.AgentHarness（working hooks+payload 渲染缝内联）。
"""
from ironman.kernel.capabilities import KernelCapabilities

HarnessCapabilities = KernelCapabilities

__all__ = ["HarnessCapabilities"]
