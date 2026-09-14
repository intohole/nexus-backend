"""策略注册表：按类型名注册/查找 Trigger、Condition、Action。"""
from __future__ import annotations

import threading
from typing import Generic, Optional, TypeVar

from nexus.automation.base import Action, Condition, Trigger

T = TypeVar("T", Trigger, Condition, Action)


class StrategyRegistry(Generic[T]):
    def __init__(self, kind: str) -> None:
        self._kind = kind
        self._items: dict[str, T] = {}
        self._lock = threading.Lock()

    def register(self, item: T) -> T:
        with self._lock:
            self._items[item.name] = item
        return item

    def get(self, name: Optional[str]) -> Optional[T]:
        if not name:
            return None
        with self._lock:
            return self._items.get(name)

    def list_names(self) -> list[str]:
        with self._lock:
            return list(self._items.keys())

    def require(self, name: str) -> T:
        item = self.get(name)
        if item is None:
            raise KeyError(f"{self._kind} '{name}' 未注册，可用: {self.list_names()}")
        return item