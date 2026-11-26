"""nexus HarnessProfile：r260 B1 薄壳化——装配基座上收 ironman.kernel.KernelProfile（contracts.md §5）。

本类保留为继承扩展点（nexus 未来可在子类加业务字段）；字段集与 ironman 基座
逐字段等价（行为等价不变式=tests/test_r260_kernel_equivalence.py 金样对表）。
"""
from __future__ import annotations

from ironman.kernel.profile import KernelProfile


class HarnessProfile(KernelProfile):
    pass


__all__ = ["HarnessProfile"]
