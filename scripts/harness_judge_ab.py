"""真链判官 A/B：evaluation 档思考双通道（thinking on/off）对判官质量的实证。

走 nexus 生产同链路（configure_ironman→LLMService.chat，规范门禁合规）：
A 组=task_type evaluation 显式 max_tokens 600（现状档位形态）；B 组=2000+enable_thinking
True+thinking_budget 2048（候选档位形态）。模型与隧道口参数化。

用法：python3 -I scripts/harness_judge_ab.py --api-key-file /tmp/gwkey_r75 \
      --base-url http://localhost:18941/api/gateway/v1 --out out/r75_judge_ab.json
判定：B 组区分度 > A 组 +0.5 且 json_ok(B) >= json_ok(A) - 10pp => thinking有益
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from pathlib import Path
from typing import Optional

RUBRIC = (
    "你是严格的技术评审。对照任务要求给候选答案打分（0-10，一位小数）。评分维度：\n"
    "- 正确性（0-4）：无事实错误、无逻辑 bug、结论成立\n"
    "- 完整性（0-3）：覆盖任务要求的全部要点\n"
    "- 可操作性（0-3）：结论可直接落地执行\n"
    '只输出 JSON：{"score": <0-10>, "reasons": "<=60字"}'
)

TASK_ASYNC = "任务：评审以下 Python 异步函数是否正确实现了「带超时的批量抓取，失败重试一次，全部失败返回空列表」。"

SAMPLES: list[dict[str, str]] = [
    {"name": "code_good1", "good": True, "kind": "async", "answer": (
        "import asyncio\n\n"
        "async def fetch_all(urls: list[str], timeout: float = 5.0) -> list[str]:\n"
        "    async def fetch_one(url: str) -> str | None:\n"
        "        for attempt in range(2):\n"
        "            try:\n"
        "                async with asyncio.timeout(timeout):\n"
        "                    reader, _ = await asyncio.open_connection(url.split('/')[0], 80)\n"
        "                    return url\n"
        "            except (OSError, TimeoutError):\n"
        "                if attempt == 1:\n"
        "                    return None\n"
        "        return None\n"
        "    results = await asyncio.gather(*(fetch_one(u) for u in urls))\n"
        "    return [r for r in results if r is not None]")},
    {"name": "code_bad1", "good": False, "kind": "async", "answer": (
        "import asyncio\n\n"
        "async def fetch_all(urls: list[str], timeout: float = 5.0) -> list[str]:\n"
        "    results = []\n"
        "    for url in urls:\n"
        "        try:\n"
        "            async with asyncio.timeout(timeout):\n"
        "                reader, _ = await asyncio.open_connection(url, 443)\n"
        "                results.append(await reader.read())\n"
        "        except OSError:\n"
        "            continue\n"
        "    return results")},
    {"name": "code_good2", "good": True, "kind": "async", "answer": (
        "async def fetch_all(urls: list[str], timeout: float = 5.0) -> list[str]:\n"
        "    semaphore = asyncio.Semaphore(10)\n"
        "    async def one(u: str) -> str | None:\n"
        "        async with semaphore:\n"
        "            for i in range(2):\n"
        "                try:\n"
        "                    async with asyncio.timeout(timeout):\n"
        "                        return await do_get(u)\n"
        "                except (OSError, asyncio.TimeoutError):\n"
        "                    if i:\n"
        "                        return None\n"
        "        return None\n"
        "    done = await asyncio.gather(*(one(u) for u in urls))\n"
        "    return [x for x in done if x is not None]")},
    {"name": "code_bad2", "good": False, "kind": "async", "answer": (
        "async def fetch_all(urls: list[str], timeout: float = 5.0) -> list[str]:\n"
        "    async def retry(u: str):\n"
        "        return await asyncio.wait_for(do_get(u), timeout)\n"
        "    out = []\n"
        "    for u in urls:\n"
        "        r = await retry(u)\n"
        "        if r is not None:\n"
        "            out.append(r)\n"
        "    return out")},
    {"name": "plan_good1", "good": True, "kind": "plan", "answer": (
        "方案：1) 用 asyncio.gather 并发全部请求，单请求用 asyncio.timeout 包住；"
        "2) 失败立刻重试一次（重试仍走同一超时），两次都失败记 None；"
        "3) gather 全部完成后过滤 None 返回剩余，并发上限用 Semaphore(10) 防打满对端；"
        "4) 整体加 30s 总兜底超时，超时部分按失败处理。")},
    {"name": "plan_bad1", "good": False, "kind": "plan", "answer": (
        "方案：写一个 for 循环把所有 url 逐个 await 抓取，try/except 包住，"
        "出异常就 print 一下错误然后 continue，最后 return 结果列表。重试可以以后再加。")},
    {"name": "plan_good2", "good": True, "kind": "plan", "answer": (
        "方案：1) 每 url 封装 fetch_one，内部 for attempt in (0,1) 两次尝试；"
        "2) 每次尝试都用 asyncio.timeout(timeout) 独立计时；"
        "3) 二层全失败返回 None；4) 顶层 asyncio.gather 一次性并发收集；"
        "5) 返回前过滤 None；6) 用 Semaphore 限制并发数，避免瞬时连接数过大。")},
    {"name": "plan_bad2", "good": False, "kind": "plan", "answer": (
        "方案：用 gather 并发跑所有请求即可，asyncio 本身很健壮，不需要超时；"
        "失败的话异常会自然抛出，让调用方自己处理；全部失败的场景不存在，"
        "因为网络请求总会成功一部分。")},
]

TASK_PLAN = "任务：评审以下技术方案是否正确实现了「带超时的批量抓取，失败重试一次，全部失败返回空列表」的要求。"


def write_ironman_yaml(api_key: str, base_url: str, path: Path) -> None:
    cfg = (
        "llm_default:\n"
        "  provider: zhipu\n"
        "  model: deepseek-flash\n"
        f'  api_key: "{api_key}"\n'
        f'  base_url: "{base_url}"\n'
        "  max_tokens: 4096\n"
        "  temperature: 0.1\n"
        "middleware:\n"
        "  logging: false\n"
        "  cache:\n"
        "    enabled: false\n"
        "  rate_limit:\n"
        "    enabled: false\n"
    )
    path.write_text(cfg)
    path.chmod(0o600)


async def call_gateway(model: str, group: str, sample: dict[str, str], timeout: float = 120.0) -> tuple[Optional[str], float]:
    import time
    from nexus import get_llm_service
    task = TASK_ASYNC if sample["kind"] == "async" else TASK_PLAN
    messages = [{"role": "user", "content": f"{task}\n\n候选答案：\n{sample['answer']}"}]
    svc = get_llm_service()
    kwargs: dict = dict(
        system=RUBRIC, temperature=0.2, max_tokens=2000 if group == "b" else 600,
        json_mode=True, task_type="evaluation", model=model, no_cache=True,
        timeout=timeout,
    )
    if group == "b":
        kwargs["enable_thinking"] = True
        kwargs["thinking_budget"] = 2048
    t0 = time.time()
    try:
        content = await svc.chat(messages, **kwargs)
        return str(content), time.time() - t0
    except Exception as exc:
        print(f"  CHAT-ERR {type(exc).__name__}: {str(exc)[:120]}")
        return None, time.time() - t0


def score_of(content: Optional[str]) -> tuple[Optional[float], bool]:
    if not content:
        return None, False
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None, False
    if not isinstance(parsed, dict) or "score" not in parsed:
        return None, False
    raw = parsed.get("score")
    if not isinstance(raw, (int, float)) or isinstance(raw, bool) or not (0 <= float(raw) <= 10):
        return None, False
    return float(raw), True


async def run_group(model: str, group: str, rounds: int) -> dict:
    scores: dict[str, list[float]] = {}
    latencies: list[float] = []
    json_ok = 0
    total = 0
    for rnd in range(rounds):
        for sample in SAMPLES:
            content, latency = await call_gateway(model, group, sample)
            value, ok = score_of(content)
            total += 1
            latencies.append(latency)
            if ok:
                json_ok += 1
            else:
                value = 0.0
            scores.setdefault(sample["name"], []).append(value)
            print(f"  {group}/{model}/{sample['name']} r{rnd}: score={value} ok={ok} {latency:.1f}s")
    good_flag = {s["name"]: s["good"] for s in SAMPLES}
    good_all = [v for s, vals in scores.items() if good_flag[s] for v in vals]
    bad_all = [v for s, vals in scores.items() if not good_flag[s] for v in vals]
    spread = max((max(v) - min(v) for v in scores.values()), default=0.0)
    return {
        "model": model, "group": group,
        "good_mean": round(statistics.mean(good_all), 2) if good_all else 0.0,
        "bad_mean": round(statistics.mean(bad_all), 2) if bad_all else 0.0,
        "separation": round(statistics.mean(good_all) - statistics.mean(bad_all), 2) if good_all and bad_all else 0.0,
        "json_ok_rate": round(json_ok / total, 3) if total else 0.0,
        "latency_mean_s": round(statistics.mean(latencies), 1) if latencies else 0.0,
        "max_within_sample_spread": round(spread, 2),
        "scores": scores,
    }


def verdict_of(a: dict, b: dict) -> str:
    sep_gain = b["separation"] - a["separation"]
    ok_floor = a["json_ok_rate"] - 0.10
    if sep_gain > 0.5 and b["json_ok_rate"] >= ok_floor:
        return "thinking_beneficial"
    return "thinking_not_beneficial"


async def amain(models: list[str], rounds: int, out: Path, api_key: str, base_url: str) -> None:
    from nexus.llm_config import configure_ironman
    yaml_path = Path("/tmp/r75_judge_ab_ironman.yaml")
    write_ironman_yaml(api_key, base_url, yaml_path)
    await configure_ironman(str(yaml_path))
    results = [await run_group(m, g, rounds) for m in models for g in ("a", "b")]
    pairs = [(results[i], results[i + 1]) for i in range(0, len(results), 2)]
    verdicts = {a["model"]: verdict_of(a, b) for a, b in pairs}
    report = {"samples": len(SAMPLES), "rounds": rounds, "results": results, "verdicts": verdicts}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(json.dumps([{k: v for k, v in r.items() if k != "scores"} for r in results], ensure_ascii=False, indent=1))
    print("VERDICT:", verdicts)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-key-file", required=True)
    parser.add_argument("--base-url", default="http://localhost:18941/api/gateway/v1")
    parser.add_argument("--models", default="GLM-4.5-Air,deepseek-flash")
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--out", default="out/r75_judge_ab.json")
    args = parser.parse_args()
    raw = Path(args.api_key_file).read_text().strip()
    api_key = json.loads(raw).get("api_key", raw) if raw.startswith("{") else raw
    asyncio.run(amain(args.models.split(","), args.rounds, Path(args.out), api_key, args.base_url))


if __name__ == "__main__":
    main()
