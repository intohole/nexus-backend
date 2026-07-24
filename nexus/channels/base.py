from __future__ import annotations

from abc import ABC, abstractmethod


VALID_CHANNELS: list[str] = ["in_app", "email", "webhook"]


class NotificationChannel(ABC):
    def __init__(self, name: str) -> None:
        self._name: str = name

    @property
    def name(self) -> str:
        return self._name

    @abstractmethod
    async def send(self, notification: dict[str, object]) -> bool:
        ...

    def to_dict(self) -> dict[str, str]:
        return {"channel": self._name}


__all__ = ["NotificationChannel", "VALID_CHANNELS"]
