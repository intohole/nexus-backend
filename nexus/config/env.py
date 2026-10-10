"""配置环境变量解析：${VAR:-default} 替换引擎。"""
from __future__ import annotations

import os
import re
from typing import Callable

_ENV_SUB = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}")
_ENV_FULL = re.compile(r"^\$\{(\w+)(?::-([^}]*))?\}$")


def _substitute_env(value: str, *, anchored: bool, missing: str) -> str:
    if anchored:
        m = _ENV_FULL.match(value)
        if m is None:
            return value
        val = os.environ.get(m.group(1))
        if val is not None:
            return val
        return m.group(2) if m.group(2) is not None else ""

    def _repl(m: re.Match) -> str:
        val = os.environ.get(m.group(1))
        if val is not None:
            return val
        if m.group(2) is not None:
            return m.group(2)
        return "" if missing == "empty" else m.group(0)

    return _ENV_SUB.sub(_repl, value)


def _walk_env(data: object, resolve: Callable[[str], str]) -> object:
    if isinstance(data, dict):
        return {k: _walk_env(v, resolve) for k, v in data.items()}
    if isinstance(data, list):
        return [_walk_env(i, resolve) for i in data]
    if isinstance(data, str):
        return resolve(data)
    return data


def resolve_env_string(value: str) -> str:
    if not isinstance(value, str):
        return value
    return _substitute_env(value, anchored=False, missing="keep")


def resolve_env_tree(data: object) -> object:
    return _walk_env(data, resolve_env_string)


def _resolve_env(value: object) -> object:
    if isinstance(value, str):
        return _substitute_env(value, anchored=True, missing="empty")
    return value


def _resolve_dict(data: dict[str, object]) -> dict[str, object]:
    return _walk_env(data, _resolve_env)
