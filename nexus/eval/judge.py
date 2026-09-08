from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from nexus.llm import get_llm_service
from nexus.logging import get_logger

logger = get_logger("nexus.eval.judge")

JUDGE_PROMPT = """你是AI课堂质检裁判。请基于对话记录判定回答质量,只输出JSON。
# 对话记录
{{conversation}}
# 判定维度(0/1)
- first_turn_answers: 首个AI发言是否直接实质回答了用户问题(简短acknowledgment如"好的/有道理"不算)
- route_ok: 首个发言角色是否为 {{first_speaker}}
- dedup: 后续发言是否重复了已解释透的内容(0=未重复,1=有重复)
- vocality: 是否有空转(说空话无实质内容)
# 输出格式
{"first_turn_answers":0或1,"route_ok":0或1,"dedup":0或1,"vocality":0或1,"summary":"一句话说明"}
注意:依据对话记录的role标签判断发言角色,不要凭内容前缀猜测。"""


@dataclass
class JudgeResult:
    first_turn_answers: bool = False
    route_ok: bool = False
    dedup: bool = False
    vocality: bool = False
    summary: str = ""


async def judge_conversation(
    conversation: List[Dict[str, str]],
    expected_speaker: str,
) -> JudgeResult:
    conv_text = "\n".join(
        f"[{m.get('role', '')}] {m.get('content', '')}" for m in conversation
    )
    prompt = (
        JUDGE_PROMPT.replace("{{conversation}}", conv_text)
        .replace("{{first_speaker}}", expected_speaker)
    )
    svc = get_llm_service()
    try:
        raw: Dict[str, Any] = await svc.ask_json(
            prompt=prompt,
            temperature=0.1,
            max_tokens=300,
            task_type="eval",
        )
    except Exception as exc:
        logger.warning("judge failed: %s", exc)
        return JudgeResult(summary=f"judge error: {exc}")
    return JudgeResult(
        first_turn_answers=bool(raw.get("first_turn_answers")),
        route_ok=bool(raw.get("route_ok")),
        dedup=bool(raw.get("dedup")),
        vocality=bool(raw.get("vocality")),
        summary=str(raw.get("summary", "")),
    )