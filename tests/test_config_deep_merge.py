from nexus.config import deep_merge, resolve_env_tree


def test_deep_merge_nested_override_wins():
    base = {"a": {"x": 1, "y": 2}, "b": 1}
    override = {"a": {"y": 3, "z": 4}, "c": 5}
    assert deep_merge(base, override) == {"a": {"x": 1, "y": 3, "z": 4}, "b": 1, "c": 5}


def test_deep_merge_non_dict_override_replaces_whole_value():
    base = {"a": {"x": 1}}
    override = {"a": [1, 2]}
    assert deep_merge(base, override) == {"a": [1, 2]}


def test_deep_merge_does_not_mutate_base():
    base = {"a": {"x": 1}}
    deep_merge(base, {"a": {"y": 2}})
    assert base == {"a": {"x": 1}}


def test_deep_merge_with_local_override_config_pattern():
    """业务仓 config.py 的 config.yaml + local.yaml 合并姿势。"""
    base = resolve_env_tree({"server": {"port": 8010}, "nodes": {"self": "edge-01"}})
    local = resolve_env_tree({"nodes": {"cluster_token": "${MD_TEST_UNSET_VAR}"}})
    merged = deep_merge(base, local)
    assert merged["server"]["port"] == 8010
    assert merged["nodes"]["self"] == "edge-01"
    assert merged["nodes"]["cluster_token"] == "${MD_TEST_UNSET_VAR}"
