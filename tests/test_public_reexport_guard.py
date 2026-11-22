"""包根 re-export 守底：业务仓真实 import 形态逐一在场即绿，任一再被静默删除即红。

2026-11-06 r169 事故守底（波及 oneNote/challengePlanet/LifeCompass/codeBlock/
truemirror/adSmart/piying 七应用）：r169 以「全舰队零生产消费」退役
StatelessRepository/VolcArkClient，但甄别矩阵漏业务仓消费面（oneNote 等以
`from nexus import StatelessRepository` 包根形态导入；PiYing 的 volc_client
为 nexus.volc 薄转发被同名文件名误判「仓内自有实现」）。热修只回滚
repository.py 未回滚 __init__ 去名，六应用生产崩溃循环。本守底把消费形态
钉进 nexus 自身测试，删除面甄别漏网时 pytest 先红、不再依赖生产崩溃报警。

2026-11-23 r241 钉面收窄（同协议证据：AST 全工作区+词面矩阵+五节点部署体 grep，
判据=钉面形态钉该名现消费形态，摘钉+撤回+改钉捆绑同轮）：
- StatelessRepository 维持包根钉死——oneNote/LifeCompass/adSmart/codeBlock/
  challengePlanet 五仓 23 文件 `from nexus import` 包根消费实锤（AST live+
  五节点部署体 27 行命中）；r240 遗留注记「六名零包根消费」系证据误读（其自家
  ast_final.json live 名单即含本名），本轮开工重验翻案。
- paginate/paginate_skip/VolcArkClient/VolcArkError/VolcArkUnavailableError
  五名三仪零包根消费（消费面全为 `nexus.repository`/`nexus.volc` 子模块形态），
  包根 re-export 同轮退役，守底改钉被消费的子模块形态。
"""

import nexus


def test_repository_reexports_in_package_root() -> None:
    for name in ("StatelessRepository",):
        assert hasattr(nexus, name), f"nexus.{name} 被删：oneNote/LifeCompass/adSmart/codeBlock/challengePlanet 包根导入将崩"


def test_repository_submodule_exports_alive() -> None:
    from nexus.repository import paginate, paginate_skip

    assert callable(paginate) and callable(paginate_skip), (
        "nexus.repository.paginate/paginate_skip 被删：verseCraft/moutain/gezhi/WisePath/"
        "userFeedback/wanxiang/promptGenius/adSmart 子模块导入将崩"
    )


def test_volc_submodule_exports_alive() -> None:
    from nexus.volc import VolcArkClient, VolcArkError, VolcArkUnavailableError

    assert VolcArkClient is not None and issubclass(VolcArkError, Exception) and issubclass(VolcArkUnavailableError, VolcArkError), (
        "nexus.volc Volc 三名被删：PiYing volc_client 薄转发/arcade sprites 子模块导入将崩"
    )


def test_mountain_batch_websearch_method_alive() -> None:
    from nexus.mountain import MountainClient

    assert hasattr(MountainClient, "submit_batch_websearch"), "moutain 部署体(apps/spider)真消费面，勿删"


def test_reexports_listed_in_all() -> None:
    assert "StatelessRepository" in nexus.__all__
    for name in ("paginate", "paginate_skip", "VolcArkClient", "VolcArkError", "VolcArkUnavailableError"):
        assert name not in nexus.__all__, f"nexus.__all__ 残留 {name}：r241 已撤回的包根面，复挂须先立消费证据"


def test_database_manager_add_missing_columns_alive() -> None:
    from nexus.database import DatabaseManager

    assert hasattr(DatabaseManager, "add_missing_columns"), (
        "DatabaseManager.add_missing_columns 被删：truemirror/PiYing/yiwei/adSmart 启动链真消费，删=生产崩溃循环"
    )


def test_memorycache_public_cache_api_alive() -> None:
    """moutain search 链真实消费 get_entry（缓存软过期刷新面），peek/cleanup 向后兼容保留。"""
    from nexus.utils import MemoryCache

    for name in ("get_entry", "peek", "cleanup_expired"):
        assert hasattr(MemoryCache, name), f"MemoryCache.{name} 被删：moutain search 缓存链 AttributeError（r169 第四枚误删实锤）"
