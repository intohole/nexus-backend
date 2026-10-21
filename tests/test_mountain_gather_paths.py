"""moutain 客户端路径契约：前缀已由 /api/v1/crawler 更名 /api/v1/gather
（2026-10 生产 404 实锤：resumeAI/financialKG 采集全量断链 7 天）。"""
import pytest

import nexus.mountain as mountain_mod


class _CaptureClient(mountain_mod.MountainClient):
    def __init__(self):
        super().__init__(base_url="http://moutain.test")
        self.paths = []

    async def _post(self, path, payload):
        self.paths.append(path)
        return {"success": True}


@pytest.mark.asyncio

@pytest.mark.asyncio
async def test_submit_crawl_task_targets_gather_prefix():
    client = _CaptureClient()
    await client.submit_crawl_task(url="https://x/api", callback_url="https://cb")
    assert client.paths == ["/api/v1/gather/execute"]


@pytest.mark.asyncio

@pytest.mark.asyncio
async def test_submit_websearch_task_targets_gather_prefix():
    client = _CaptureClient()
    await client.submit_websearch_task(query="算法 招聘", callback_url="https://cb")
    assert client.paths == ["/api/v1/gather/websearch"]


@pytest.mark.asyncio

@pytest.mark.asyncio
async def test_submit_rss_task_targets_gather_prefix():
    client = _CaptureClient()
    await client.submit_rss_task(url="https://feed", callback_url="https://cb")
    assert client.paths == ["/api/v1/gather/fetch-rss"]
