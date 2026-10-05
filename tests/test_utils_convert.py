"""loads_or 原语契约：空值/脏 JSON/JSON null 回 default，合法 falsy 保留。"""
from nexus import loads_or


def test_empty_and_none_return_default():
    assert loads_or(None, {}) == {}
    assert loads_or("", []) == []
    assert loads_or("   ", "x") == "x"


def test_dirty_json_returns_default():
    assert loads_or("not json", {}) == {}
    assert loads_or("{broken", []) == []
    assert loads_or(b"\xff\xfe", {}) == {}


def test_json_null_returns_default():
    assert loads_or("null", {}) == {}
    assert loads_or('"null"', {}) == "null"


def test_valid_falsy_values_preserved():
    assert loads_or("[]", {}) == []
    assert loads_or("0", 5) == 0
    assert loads_or("false", True) is False
    assert loads_or('"0"', "") == "0"


def test_valid_values_roundtrip():
    assert loads_or('{"a": 1}', {}) == {"a": 1}
    assert loads_or('["x", "y"]', []) == ["x", "y"]
    assert loads_or(b'{"k": "v"}', {}) == {"k": "v"}
