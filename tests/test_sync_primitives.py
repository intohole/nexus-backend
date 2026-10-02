"""SyncTTLCache 与 TokenBucket 同步原语回归。"""
import threading
import time

import pytest

from nexus.rate_limit import TokenBucket
from nexus.utils import SyncTTLCache


class TestSyncTTLCache:
    def test_set_get_roundtrip(self):
        cache = SyncTTLCache(default_ttl=60)
        cache.set("k", {"v": 1})
        assert cache.get("k") == {"v": 1}
        assert cache.get("missing") is None
        assert cache.get("missing", "fb") == "fb"

    def test_per_key_ttl_override(self):
        cache = SyncTTLCache(default_ttl=60)
        cache.set("a", 1, ttl=0.05)
        cache.set("b", 2)
        time.sleep(0.08)
        assert cache.get("a") is None
        assert cache.get("b") == 2

    def test_expired_entry_deleted_on_get(self):
        cache = SyncTTLCache(default_ttl=0.05)
        cache.set("k", 1)
        time.sleep(0.08)
        assert cache.get("k") is None
        assert cache.size() == 0

    def test_lru_eviction_under_capacity(self):
        cache = SyncTTLCache(default_ttl=60, max_size=3)
        for i in range(4):
            cache.set(f"k{i}", i)
        assert cache.size() == 3
        assert cache.get("k0") is None
        assert cache.get("k1") == 1

    def test_get_touch_refreshes_lru_order(self):
        cache = SyncTTLCache(default_ttl=60, max_size=2)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.get("a")
        cache.set("c", 3)
        assert cache.get("a") == 1
        assert cache.get("b") is None

    def test_evict_expired_before_lru(self):
        cache = SyncTTLCache(default_ttl=0.05, max_size=2)
        cache.set("e1", 1)
        cache.set("e2", 2)
        time.sleep(0.08)
        cache.set("fresh", 3)
        assert cache.size() == 1
        assert cache.get("fresh") == 3

    def test_set_existing_key_does_not_evict(self):
        cache = SyncTTLCache(default_ttl=60, max_size=2)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("a", 10)
        assert cache.get("a") == 10
        assert cache.get("b") == 2

    def test_delete_returns_bool(self):
        cache = SyncTTLCache()
        cache.set("k", 1)
        assert cache.delete("k") is True
        assert cache.delete("k") is False

    def test_invalidate_prefix_and_all(self):
        cache = SyncTTLCache()
        cache.set("kb:1", 1)
        cache.set("kb:2", 2)
        cache.set("llm:1", 3)
        assert cache.invalidate("kb:") == 2
        assert cache.get("llm:1") == 3
        assert cache.size() == 1
        assert cache.invalidate() == 1
        assert cache.size() == 0

    def test_cleanup_returns_removed_count(self):
        cache = SyncTTLCache(default_ttl=0.05)
        cache.set("a", 1)
        cache.set("b", 2)
        time.sleep(0.08)
        assert cache.cleanup() == 2
        assert cache.cleanup() == 0

    def test_get_or_set(self):
        cache = SyncTTLCache(default_ttl=60)
        calls: list[int] = []

        def factory():
            calls.append(1)
            return "value"

        assert cache.get_or_set("k", factory) == "value"
        assert cache.get_or_set("k", factory) == "value"
        assert len(calls) == 1

    def test_stats_counts_hits_and_misses(self):
        cache = SyncTTLCache(default_ttl=60)
        cache.set("k", 1)
        cache.get("k")
        cache.get("k")
        cache.get("nope")
        s = cache.stats
        assert s["hits"] == 2
        assert s["misses"] == 1
        assert abs(s["hit_rate"] - 2 / 3) < 1e-9

    def test_thread_safety_smoke(self):
        cache = SyncTTLCache(default_ttl=60, max_size=100)

        def worker(n: int) -> None:
            for i in range(200):
                cache.set(f"t{n}:{i}", i)
                cache.get(f"t{n}:{i}")

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert cache.size() <= 100


class TestTokenBucket:
    def test_initial_full_capacity(self):
        bucket = TokenBucket(capacity=5, refill_rate=100)
        assert bucket.remaining() == 5

    def test_consume_drains_until_rejected(self):
        bucket = TokenBucket(capacity=3, refill_rate=0.001)
        assert bucket.consume() is True
        assert bucket.consume() is True
        assert bucket.consume() is True
        assert bucket.consume() is False
        assert bucket.remaining() < 1

    def test_partial_amount(self):
        bucket = TokenBucket(capacity=2, refill_rate=0.001)
        assert bucket.consume(2) is True
        assert bucket.consume(1) is False

    def test_refill_over_time(self):
        bucket = TokenBucket(capacity=10, refill_rate=100)
        assert bucket.consume(10) is True
        time.sleep(0.05)
        assert bucket.remaining() > 4

    def test_retry_after_zero_when_available(self):
        bucket = TokenBucket(capacity=5, refill_rate=10)
        assert bucket.retry_after() == 0.0

    def test_retry_after_positive_when_exhausted(self):
        bucket = TokenBucket(capacity=1, refill_rate=10)
        bucket.consume()
        wait = bucket.retry_after()
        assert 0.0 < wait <= 0.15

    def test_refund_restores_within_capacity(self):
        bucket = TokenBucket(capacity=2, refill_rate=0.001)
        bucket.consume(2)
        bucket.refund(5)
        assert bucket.remaining() == 2

    def test_invalid_params_rejected(self):
        with pytest.raises(ValueError):
            TokenBucket(capacity=0, refill_rate=1)
        with pytest.raises(ValueError):
            TokenBucket(capacity=1, refill_rate=0)
        bucket = TokenBucket(capacity=1, refill_rate=1)
        with pytest.raises(ValueError):
            bucket.consume(0)

    def test_thread_safety_smoke(self):
        bucket = TokenBucket(capacity=400, refill_rate=0.001)
        granted: list[bool] = []
        lock = threading.Lock()

        def worker() -> None:
            local = [bucket.consume() for _ in range(100)]
            with lock:
                granted.extend(local)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sum(granted) <= 400
