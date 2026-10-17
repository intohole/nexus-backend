"""register_health_endpoints：check 返回 "ok (详情)" 不得被判 degraded。"""
from nexus.boot import _check_is_ok


def test_ok_prefix_variants():
    assert _check_is_ok("ok")
    assert _check_is_ok("ok (4 jobs)")
    assert _check_is_ok("ok(4 jobs)")
    assert not _check_is_ok("okay")
    assert not _check_is_ok("gated-off")
    assert not _check_is_ok("revived (4 jobs)")
    assert not _check_is_ok("error")
    assert not _check_is_ok("")
