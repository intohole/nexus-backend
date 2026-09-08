import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from nexus.logging import get_logger

logger = get_logger("nexus.eval.runner")

SCENARIOS_DIR = Path(__file__).resolve().parent / "scenarios"
REPORT_PATH = Path(__file__).resolve().parent / "report.md"
GATE_THRESHOLD = 1.0


@dataclass
class ScenarioResult:
    id: str
    desc: str
    scores: Dict[str, bool]
    summary: str = ""


def load_scenarios() -> List[Dict[str, Any]]:
    scenarios: List[Dict[str, Any]] = []
    for path in sorted(SCENARIOS_DIR.glob("*.json")):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data["_path"] = path.name
        scenarios.append(data)
    return scenarios


def build_conversation(conversation: List[Dict[str, str]]) -> List[Dict[str, str]]:
    result: List[Dict[str, str]] = []
    for msg in conversation:
        role = msg.get("role", "user")
        label = "Student (Human)" if role == "user" else "Agent"
        result.append({"role": label, "content": msg.get("content", "")})
    return result


def scores_passed(scores: Dict[str, bool]) -> bool:
    if not scores.get("first_turn_answers", False):
        return False
    if scores.get("route_ok", False) is False:
        return False
    if scores.get("vocality", False):
        return False
    return True


def render_report(results: List[ScenarioResult], passed: int, total: int) -> str:
    lines = ["# LLM 质检报告", ""]
    lines.append(f"通过 {passed}/{total}", "")
    for r in results:
        status = "PASS" if scores_passed(r.scores) else "FAIL"
        lines.append(f"## {r.id} - {status}")
        lines.append(f"- 描述: {r.desc}")
        lines.append(
            f"- 得分: 首答={int(r.scores.get('first_turn_answers', False))} "
            f"路由={int(r.scores.get('route_ok', False))} "
            f"去重={int(r.scores.get('dedup', False))} "
            f"空转={int(r.scores.get('vocality', False))}"
        )
        lines.append(f"- 裁判: {r.summary}")
        lines.append("")
    return "\n".join(lines)


async def run() -> List[ScenarioResult]:
    from nexus.eval.judge import judge_conversation

    results: List[ScenarioResult] = []
    for scenario in load_scenarios():
        conv = build_conversation(scenario["conversation"])
        expect: Dict[str, Any] = scenario.get("expect", {})
        judge = await judge_conversation(
            conv, str(expect.get("first_speaker", "teacher"))
        )
        results.append(
            ScenarioResult(
                id=str(scenario.get("id", "unknown")),
                desc=str(scenario.get("desc", "")),
                scores={
                    "first_turn_answers": judge.first_turn_answers,
                    "route_ok": judge.route_ok,
                    "dedup": judge.dedup,
                    "vocality": judge.vocality,
                },
                summary=judge.summary,
            )
        )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM 质检与回归门禁")
    parser.add_argument(
        "--gate",
        action="store_true",
        help="作为门禁:通过率低于阈值时 exit 非0",
    )
    args = parser.parse_args()

    results = asyncio.run(run())
    passed = sum(1 for r in results if scores_passed(r.scores))
    total = len(results)
    REPORT_PATH.write_text(render_report(results, passed, total), encoding="utf-8")
    logger.info("eval done: %s/%s passed, report at %s", passed, total, REPORT_PATH)

    rate = passed / total if total else 1.0
    if args.gate and rate < GATE_THRESHOLD:
        logger.warning("GATE FAILED: 通过率 %.2f < %.2f", rate, GATE_THRESHOLD)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())