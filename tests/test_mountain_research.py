"""深度研究客户端契约：提交路径/结果解包/轮询终态与失败语义。"""
import pytest

import nexus.mountain as mountain_mod


class _ScriptedClient(mountain_mod.MountainClient):
    def __init__(self, posts=None, gets=None):
        super().__init__(base_url="http://moutain.test")
        self.posts = list(posts or [])
        self.gets = list(gets or [])
        self.post_paths = []
        self.get_paths = []

    async def _post(self, path, payload):
        self.post_paths.append(path)
        return self.posts.pop(0)

    async def _get(self, path, params=None):
        self.get_paths.append(path)
        return self.gets.pop(0)


@pytest.mark.asyncio
async def test_research_task_posts_tasks_endpoint():
    client = _ScriptedClient(posts=[{"code": 200, "task_id": "t1", "status": "pending"}])
    out = await client.research_task("问题", max_iterations=2, extra_urls=["https://a"])
    assert out["task_id"] == "t1"
    assert client.post_paths == ["/api/deepresearch/tasks"]


@pytest.mark.asyncio
async def test_research_task_result_unwraps_task_field():
    client = _ScriptedClient(gets=[{"code": 200, "task": {"status": "running"}}])
    out = await client.research_task_result("t1")
    assert out == {"status": "running"}
    assert client.get_paths == ["/api/deepresearch/tasks/t1"]


@pytest.mark.asyncio
async def test_research_polls_until_succeeded():
    client = _ScriptedClient(
        posts=[{"code": 200, "task_id": "t1", "status": "pending"}],
        gets=[
            {"code": 200, "task": {"status": "running", "progress": 1}},
            {"code": 200, "task": {"status": "succeeded", "report": {"summary": "s"}}},
        ],
    )
    out = await client.research("问题", poll_interval=0.0, timeout=5.0)
    assert out["success"] is True
    assert out["report"]["summary"] == "s"


@pytest.mark.asyncio
async def test_research_failed_maps_error():
    client = _ScriptedClient(
        posts=[{"code": 200, "task_id": "t2", "status": "pending"}],
        gets=[{"code": 200, "task": {"status": "failed", "error": "engine exploded"}}],
    )
    out = await client.research("问题", poll_interval=0.0, timeout=5.0)
    assert out["success"] is False
    assert "engine exploded" in out["error"]


@pytest.mark.asyncio
async def test_research_submit_failure_short_circuits():
    client = _ScriptedClient(posts=[None])
    out = await client.research("问题", poll_interval=0.0, timeout=5.0)
    assert out == {"success": False, "error": "research task submit failed"}
    assert client.get_paths == []


@pytest.mark.asyncio
async def test_research_timeout_reports():
    client = _ScriptedClient(
        posts=[{"code": 200, "task_id": "t3", "status": "pending"}],
        gets=[
            {"code": 200, "task": {"status": "running"}},
            {"code": 200, "task": {"status": "running"}},
        ],
    )
    out = await client.research("问题", poll_interval=0.0, timeout=0.0)
    assert out["success"] is False
    assert out["error"] == "research timeout"
