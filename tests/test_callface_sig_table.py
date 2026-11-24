"""调用面契约对表（r138 修订点 6，双仪不同源）。

pm 扫描器 `promptManager/scripts/harness_callface_scan.py` 的 NEXUS_LLM_SIGS
冻结表必须逐字==nexus LLMService 真签名——签名演进时本测试先红，pm 冻结表
同轮提交（时序契约见 handoff/101）。工作区平级布局假设：两仓 checkout 于
同一 workspace 根；独立 checkout 则本测试失联（登记在案的已知边界）。
"""
import importlib.util
import inspect
from pathlib import Path

from nexus.llm import LLMService

SCANNER_PATH = (
    Path(__file__).resolve().parents[2]
    / "promptManager" / "scripts" / "harness_callface_scan.py"
)


def _load_scanner():
    spec = importlib.util.spec_from_file_location("harness_callface_scan", SCANNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scanner_file_present():
    assert SCANNER_PATH.is_file(), (
        "工作区平级布局假设被破坏：找不到 promptManager/scripts/harness_callface_scan.py "
        "（handoff/101 契约前提，独立 checkout 需另设对表通道）"
    )


def test_frozen_table_matches_real_signatures():
    scanner = _load_scanner()
    assert set(scanner.NEXUS_LLM_SIGS) == {
        "ask", "ask_json", "ask_json_with_usage", "chat_json",
        "extract", "embed", "stream_chat", "stream_ask",
    }
    for method, (variadic, params) in scanner.NEXUS_LLM_SIGS.items():
        sig = inspect.signature(getattr(LLMService, method))
        real_variadic = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        real_params = {
            p for p, spec in sig.parameters.items()
            if p != "self" and spec.kind != inspect.Parameter.VAR_KEYWORD
        }
        assert real_variadic is variadic, f"{method} variadic 漂移：冻结表={variadic} 真值={real_variadic}"
        assert real_params == set(params), (
            f"{method} 参数漂移：冻结表={sorted(params)} 真值={sorted(real_params)}"
            "（nexus 签名已演进，pm 冻结表须同轮提交——handoff/101 时序契约）"
        )


def test_extract_is_explicit_signature_face():
    """TypeError 判例类只可能发生在显式签名面——extract/embed/stream_chat 必须无变参。"""
    scanner = _load_scanner()
    for method in ("extract", "embed", "stream_chat"):
        variadic, _ = scanner.NEXUS_LLM_SIGS[method]
        assert variadic is False, f"{method} 意外获得 **kwargs——判例类覆盖面收窄，须复核扫描器判据"
