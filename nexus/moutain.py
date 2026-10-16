"""过渡别名：nexus.moutain -> nexus.mountain（仓名拼写修正随更）。

消费方（resumeAI/financialKG 等）的 `from nexus.moutain import ...` 经此 shim
继续可用；随各仓自然发版迁到新路径后本文件退役（对照 r102 别名收名先例）。
"""

from nexus.mountain import (  # noqa: F401
    MoutainClient,
    MountainClient,
    get_moutain_client,
    get_mountain_client,
)

__all__ = ["MoutainClient", "MountainClient", "get_moutain_client", "get_mountain_client"]
