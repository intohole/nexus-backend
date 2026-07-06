"""LLM 调用指标收集器（A4.1）。

进程内单例 + 线程安全字典聚合，记录 latency / tokens / error / model 维度指标。
通过 /api/_internal/llm-metrics 端点暴露 JSON，供 miniDeploy 监控聚合。

设计决策：
- 不引入 prometheus_client（避免新依赖 + 多进程兼容问题）
- 后续如需 Prometheus 接入，写 adapter 转换 snapshot() 即可
"""
from __future__ import annotations

from collections import defaultdict
from threading import Lock
from typing import Optional


class LLMMetrics:
    """进程内 LLM 调用指标收集器（单例）。"""

    _instance: Optional["LLMMetrics"] = None
    _singleton_lock: Lock = Lock()

    def __new__(cls) -> "LLMMetrics":
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._init()
        return cls._instance

    def _init(self) -> None:
        self._call_lock: Lock = Lock()
        self._calls: int = 0
        self._errors: int = 0
        self._total_latency: float = 0.0
        self._total_tokens: int = 0
        self._by_app: dict[str, dict[str, float]] = defaultdict(
            lambda: {"calls": 0, "errors": 0, "tokens": 0, "latency": 0.0}
        )
        self._by_model: dict[str, dict[str, float]] = defaultdict(
            lambda: {"calls": 0, "errors": 0, "tokens": 0, "latency": 0.0}
        )
        self._by_status: dict[str, int] = defaultdict(int)

    def record(
        self,
        app_name: str,
        model: str,
        latency: float,
        tokens: int = 0,
        error: Optional[str] = None,
    ) -> None:
        """记录一次 LLM 调用。

        Args:
            app_name: 应用名（来自 get_init_app_name()）
            model: 模型名（暂为 "unknown"，后续解析 response.usage）
            latency: 调用耗时（秒）
            tokens: token 用量（暂为 0，后续解析 response.usage）
            error: 错误类型名（None 表示成功）
        """
        with self._call_lock:
            self._calls += 1
            self._total_latency += latency
            self._total_tokens += tokens
            if error:
                self._errors += 1
                self._by_status[error] += 1

            app_stats = self._by_app[app_name]
            app_stats["calls"] += 1
            app_stats["latency"] += latency
            app_stats["tokens"] += tokens
            if error:
                app_stats["errors"] += 1

            model_stats = self._by_model[model]
            model_stats["calls"] += 1
            model_stats["latency"] += latency
            model_stats["tokens"] += tokens
            if error:
                model_stats["errors"] += 1

    def snapshot(self) -> dict[str, object]:
        """返回当前指标的快照（JSON 可序列化）。"""
        with self._call_lock:
            avg_latency: float = self._total_latency / self._calls if self._calls else 0.0
            error_rate: float = round(self._errors / self._calls, 4) if self._calls else 0.0
            return {
                "total_calls": self._calls,
                "total_errors": self._errors,
                "total_tokens": self._total_tokens,
                "avg_latency_ms": round(avg_latency * 1000, 2),
                "error_rate": error_rate,
                "by_app": dict(self._by_app),
                "by_model": dict(self._by_model),
                "by_status": dict(self._by_status),
            }

    def reset(self) -> None:
        """重置所有指标（仅用于测试）。"""
        with self._call_lock:
            self._init()


def get_llm_metrics() -> LLMMetrics:
    """获取 LLM 指标收集器单例。"""
    return LLMMetrics()
