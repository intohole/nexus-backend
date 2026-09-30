"""向量相似度计算：余弦相似度及批量封装。"""
from __future__ import annotations


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return max(0.0, dot / (norm_a * norm_b))


def batch_cosine_similarity(query: list[float], candidates: list[list[float]]) -> list[float]:
    if not candidates:
        return []
    return [cosine_similarity(query, c) for c in candidates]
