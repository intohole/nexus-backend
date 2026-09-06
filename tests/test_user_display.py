import pytest

from nexus.user_display import DEFAULT_DISPLAY_NAME, resolve_display_name


def test_prefers_nickname():
    user = {"user_id": "7", "display_name": "user_7", "nickname": "lxz", "username": "user_7"}
    assert resolve_display_name(user) == "lxz"


def test_drops_auto_display_name_then_falls_back():
    user = {"user_id": "393", "display_name": "用户393", "username": "user_393"}
    assert resolve_display_name(user) == DEFAULT_DISPLAY_NAME


def test_drops_uc_prefix_username():
    user = {"user_id": "393", "username": "uc_393"}
    assert resolve_display_name(user) == DEFAULT_DISPLAY_NAME


def test_drops_numeric_username():
    user = {"user_id": "393", "username": "393"}
    assert resolve_display_name(user) == DEFAULT_DISPLAY_NAME


def test_drops_username_equal_to_user_id():
    user = {"user_id": "88", "username": "88"}
    assert resolve_display_name(user) == DEFAULT_DISPLAY_NAME


def test_masks_email_fallback():
    user = {"user_id": "7", "email": "demo@example.com"}
    assert resolve_display_name(user) == "d***o@example.com"


def test_masks_phone_fallback():
    user = {"user_id": "7", "phone": "13812341234"}
    assert resolve_display_name(user) == "138****1234"


def test_empty_returns_default():
    user = {"user_id": "7"}
    assert resolve_display_name(user) == DEFAULT_DISPLAY_NAME


def test_full_name_used_when_username_auto():
    user = {"user_id": "7", "username": "user_7", "full_name": "张三"}
    assert resolve_display_name(user) == "张三"


def test_username_priority_over_full_name():
    user = {"user_id": "7", "username": "demo_user", "full_name": "张三"}
    assert resolve_display_name(user) == "demo_user"