"""包根 re-export 守底：业务仓真实 import 形态逐一在场即绿，任一再被静默删除即红。

2026-11-06 r169 事故守底（波及 oneNote/challengePlanet/LifeCompass/codeBlock/
truemirror/adSmart/piying 七应用）：r169 以「全舰队零生产消费」退役
StatelessRepository/VolcArkClient，但甄别矩阵漏业务仓消费面（oneNote 等以
`from nexus import StatelessRepository` 包根形态导入；PiYing 的 volc_client
为 nexus.volc 薄转发被同名文件名误判「仓内自有实现」）。热修只回滚
repository.py 未回滚 __init__ 去名，六应用生产崩溃循环。本守底把消费形态
钉进 nexus 自身测试，删除面甄别漏网时 pytest 先红、不再依赖生产崩溃报警。
"""

import nexus


def test_repository_reexports_in_package_root() -> None:
    for name in ("StatelessRepository", "paginate", "paginate_skip"):
        assert hasattr(nexus, name), f"nexus.{name} 被删：oneNote/challengePlanet/LifeCompass/codeBlock/truemirror/adSmart 包根导入将崩"


def test_volc_reexports_in_package_root() -> None:
    for name in ("VolcArkClient", "VolcArkError", "VolcArkUnavailableError"):
        assert hasattr(nexus, name), f"nexus.{name} 被删：PiYing volc_client 薄转发导入将崩"


def test_mountain_batch_websearch_method_alive() -> None:
    from nexus.mountain import MountainClient

    assert hasattr(MountainClient, "submit_batch_websearch"), "moutain 部署体(apps/spider)真消费面，勿删"


def test_reexports_listed_in_all() -> None:
    for name in (
        "StatelessRepository",
        "paginate",
        "paginate_skip",
        "VolcArkClient",
        "VolcArkError",
        "VolcArkUnavailableError",
    ):
        assert name in nexus.__all__, f"nexus.__all__ 缺 {name}"


def test_database_manager_add_missing_columns_alive() -> None:
    from nexus.database import DatabaseManager

    assert hasattr(DatabaseManager, "add_missing_columns"), (
        "DatabaseManager.add_missing_columns 被删：truemirror/PiYing/yiwei/adSmart 启动链真消费，删=生产崩溃循环"
    )
