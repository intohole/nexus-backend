"""LionIntegration 共享连接池契约测试。"""
import pytest

from nexus.lion import LionIntegration


@pytest.mark.asyncio
async def test_shared_sdk_instance_reused():
    integ = LionIntegration()
    sdk1 = integ._get_lion_sdk()
    sdk2 = integ._get_lion_sdk()
    assert sdk1 is sdk2


@pytest.mark.asyncio
async def test_aclose_resets_and_rebuilds():
    integ = LionIntegration()
    sdk1 = integ._get_lion_sdk()
    await integ.aclose()
    assert integ._lion_sdk is None
    sdk2 = integ._get_lion_sdk()
    assert sdk2 is not sdk1
    await integ.aclose()


@pytest.mark.asyncio
async def test_aclose_on_never_used_instance_is_noop():
    integ = LionIntegration()
    await integ.aclose()
    assert integ._lion_sdk is None
