"""S88 extract_json_rows 单源守底：数组型 ask × json_mode 信封化的五态解包。

verseCraft r17 生产实锤的信封形态（deepseek 新代际 {"type":"json_object",
"value":[...]}）是本原语的存在理由；五态（envelope/键/裸数组/单对象/垃圾）
每个分支至少一个真触发用例（教训 #753③：只测主流形态=非主流形态永久裸奔）。
"""
import pytest

from nexus import extract_json_rows
from nexus.llm_json import parse_llm_json
from nexus.llm_utils import extract_json_rows as reexported_extract_json_rows


ENVELOPE = {"type": "json_object", "value": [{"name": "a"}, {"name": "b"}]}
KEYED = {"directions": [{"title": "x"}, {"title": "y"}]}
BARE = [{"id": 1}, {"id": 2}]
SINGLE = {"id": 7, "verdict": "keep"}


def test_envelope_unwrapped():
    assert extract_json_rows(ENVELOPE, "directions") == ENVELOPE["value"]


def test_envelope_array_type_alias():
    assert extract_json_rows({"type": "array", "value": BARE}, "items") == BARE
    assert extract_json_rows({"type": "json_array", "value": BARE}, "items") == BARE


def test_key_preferred_over_envelope():
    assert extract_json_rows(KEYED, "directions") == KEYED["directions"]


def test_bare_array_passthrough():
    assert extract_json_rows(BARE, "directions") == BARE


def test_single_dict_wrapped():
    assert extract_json_rows(SINGLE, "results") == [SINGLE]


def test_garbage_returns_empty():
    assert extract_json_rows("not-json", "x") == []
    assert extract_json_rows(None, "x") == []
    assert extract_json_rows(42, "x") == []


def test_key_present_but_not_list_falls_to_wrap():
    assert extract_json_rows({"directions": "oops"}, "directions") == [{"directions": "oops"}]


def test_envelope_value_not_list_not_unwrapped():
    inner = {"type": "json_object", "value": "oops"}
    assert extract_json_rows(inner, "items") == [inner]


def test_reexport_is_single_source():
    assert reexported_extract_json_rows is extract_json_rows


def test_combined_with_parse_llm_json_envelope_chain():
    raw = '{"type": "json_object", "value": [{"name": "甲"}]}'
    parsed = parse_llm_json(raw)
    rows = extract_json_rows(parsed, "items")
    assert rows == [{"name": "甲"}]


def test_combined_with_parse_llm_json_keyed_chain():
    raw = '{"events": [{"title": "t"}]}'
    rows = extract_json_rows(parse_llm_json(raw), "events")
    assert rows == [{"title": "t"}]


def test_combined_single_object_chain_roundtrip():
    raw = '{"id": "e1", "verdict": "soften"}'
    rows = extract_json_rows(parse_llm_json(raw), "results")
    assert rows == [{"id": "e1", "verdict": "soften"}]


@pytest.mark.parametrize("bad", [
    {"type": "json_object", "value": [{"name": "a"}]},
    "not-json",
    None,
])
def test_must_red_guard_shapes(bad):
    if isinstance(bad, dict):
        assert extract_json_rows(bad, "items") == bad["value"]
    else:
        assert extract_json_rows(bad, "items") == []
