import ast
from pathlib import Path

VOLC = Path(__file__).resolve().parent.parent / "nexus" / "volc.py"


def _async_funcs(tree: ast.Module) -> list[ast.AsyncFunctionDef]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]


def _in_to_thread(node: ast.AST, func: ast.AsyncFunctionDef) -> bool:
    for sub in ast.walk(func):
        if isinstance(sub, ast.Await) and isinstance(sub.value, ast.Call):
            call = sub.value
            if (
                isinstance(call.func, ast.Attribute)
                and call.func.attr == "to_thread"
                and any(isinstance(a, ast.Call) for a in ast.walk(call))
                and node in ast.walk(call)
            ):
                return True
    return False


def test_volc_write_bytes_all_offloaded() -> None:
    tree = ast.parse(VOLC.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for func in _async_funcs(tree):
        for sub in ast.walk(func):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "write_bytes"
                and not _in_to_thread(sub, func)
            ):
                offenders.append(f"volc.py:{sub.lineno}")
    assert not offenders, f"async 内裸 write_bytes 阻塞事件环: {offenders}"


def test_volc_offload_count() -> None:
    src = VOLC.read_text(encoding="utf-8")
    assert src.count("asyncio.to_thread(out_path.write_bytes") == 3
