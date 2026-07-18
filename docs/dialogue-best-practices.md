# 对话能力最佳实践

基于 2026 上下文工程研究结论与本项目（aiPet / oneNote / goldenFish / verseCraft）实战经验沉淀。

---

## 1. 对话历史策略选择

`nexus.ConversationHistory` 提供三种策略，按轮次自动切换：

| 策略 | 触发条件 | 适用场景 | 代价 |
|---|---|---|---|
| **全量保留** | ≤10 轮（默认） | 短对话、客服咨询、单次问答 | 上下文 token 线性增长 |
| **滑动窗口** | >10 轮 | 多轮追问、长对话 | 早期信息丢失，需调用方主动总结 |
| **摘要压缩** | >30 轮（可配） | 长程对话、心理咨询、谈判 | LLM 调用增加（一次性摘要） |

### 选择标准

- **单轮 Q&A 项目**（如 geniusStudent、WisePath、codeBlock）：**不需要**对话历史，直接 `ask()` 即可
- **多轮追问项目**（如 aiPet 宠物健康咨询）：使用 `max_turns=10, summarize_threshold=30`
- **谈判/长程对话**（如 goldenFish 闲鱼谈判）：使用 `max_turns=20, summarize_threshold=50`，配合业务状态机持久化

### 使用示例（来自 aiPet）

```python
from app.services.history_store import get_user_history

history = await get_user_history(user_id)
persistent_history = await history.get_messages()  # OpenAI messages 格式
# ... 调用 LLM ...
await history.add(user_msg=user_message, assistant_msg=llm_response)
```

---

## 2. 流式 SSE 协议规范

### 后端（FastAPI）

所有流式端点应使用 `nexus.sse_chat_stream` 包装异步生成器，统一 SSE 事件格式：

```python
from nexus import sse_chat_stream

@router.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    async def _gen():
        async for chunk in llm_service.stream_chat(messages=[...]):
            yield chunk
    return sse_chat_stream(_gen())
```

**事件格式**（遵循 SSE 规范）：
```
data: {"event": "delta", "content": "..."}\n\n
data: {"event": "done", "content": "完整文本"}\n\n
data: {"event": "error", "error": "..."}\n\n
```

### LLM Service 流式接口

`nexus.LLMService.stream_chat()` 因 ironman SDK 无原生 streaming，采用 `chunked_text_stream` 模拟打字机效果（先 `chat()` 拿完整文本，再切块 yield）。

```python
from nexus import get_llm_service

async for chunk in get_llm_service().stream_chat(
    messages=[{"role": "user", "content": "你好"}],
    system="你是助手",
    chunk_size=8,    # 默认 8 字符/块
    delay=0.03,      # 默认 30ms 延迟
):
    print(chunk, end="", flush=True)
```

### 前端

使用 `EventSource` 或 `fetch` + `ReadableStream` 消费。关键 header：
- `Content-Type: text/event-stream`
- `Cache-Control: no-cache`
- `X-Accel-Buffering: no`（禁用 nginx 缓冲，保证实时推送）

---

## 3. ironman + nexus 调用规范

### 强制：禁止 httpx 直连 LLM 端点

所有 LLM 调用必须走 `nexus.get_llm_service()` → ironman 网关，享受：
- 熔断保护（`circuit_breaker`）
- 速率限制（`rate_limit`）
- 自动重试（`with_retry`，网关模式下重试降为 1）
- 指标埋点（`llm_metrics`）

```python
# ✅ 正确
from nexus import get_llm_service
response = await get_llm_service().chat(messages=[...], system="...")

# ❌ 错误 — 绕过网关，无熔断/限流/重试
import httpx
resp = await httpx.post("https://api.openai.com/v1/chat/completions", ...)
```

### 各 app 的 ironman 集成

每个 app 在 `app/infra/ironman_bootstrap.py` 中初始化（5-15 行薄壳，设置 `APP_NAME`）：

```python
# apps/<app>/app/infra/ironman_bootstrap.py
from nexus import init_ironman, is_ironman_available

_APP_NAME = "aiPet"  # 每个 app 不同

async def init_ironman():
    await init_ironman(app_name=_APP_NAME)

def is_ironman_available() -> bool:
    return is_ironman_available()
```

**不要**强行合并 16 个 `ironman_bootstrap.py` — 每个 app 需要自己的 `APP_NAME`，合并会破坏隔离性。

---

## 4. 何时引入对话历史

### 判断标准

| 信号 | 是否引入历史 |
|---|---|
| 用户问题独立，不依赖前序对话 | ❌ 不需要（如翻译、单次问答） |
| 用户会基于上一轮回答追问 | ✅ 需要（如 aiPet 症状追问） |
| 业务有显式"会话"概念（session_id/conversation_id） | ✅ 需要 |
| 需要跨会话记忆用户偏好 | ✅ 需要，配合 beeMemory 长期记忆 |
| 对话轮次可能超过 10 轮 | ✅ 需要，且必须配摘要策略 |

### 反模式

- ❌ 给单轮 Q&A 加历史（增加 token 成本，无收益）
- ❌ 历史仅存内存（重启即丢，用户体验差）
- ❌ 全量保留 100+ 轮（token 爆炸，模型注意力涣散）
- ❌ 在 route 层直接操作历史（应封装在 service 层）

---

## 5. 上下文工程要点（2026）

### 结构化上下文 > 拼接字符串

```python
# ✅ 结构化（aiPet 的 chat_context.py）
context_parts = build_llm_context(
    user_message, conversation_history, pet_info, user_memories, symptom_info
)
final_context = "\n\n".join(context_parts)

# ❌ 字符串拼接（难维护、难扩展）
prompt = f"用户说：{msg}\n历史：{history}\n宠物：{pet}\n..."
```

### 上下文优先级

1. **系统提示词**（角色、能力、合规约束）
2. **用户长期记忆**（偏好、身份）
3. **业务上下文**（宠物信息、订单状态）
4. **对话历史**（滑动窗口）
5. **当前用户消息**

靠前的内容对模型影响更大，把不变的系统提示词放最前，频繁变化的用户消息放最后。

### 摘要压缩回调

```python
async def _summarize_old_turns(messages: list[dict]) -> str:
    """LLM 总结前 N 轮为 ≤200 字背景信息。"""
    history_text = "\n".join(f"{m['role']}: {m['content'][:200]}" for m in messages)
    return await get_llm_service().ask(
        prompt=f"总结以下对话的关键信息：\n{history_text}",
        temperature=0.3, max_tokens=300,
    )

history = await get_history(
    session_id=f"user_{uid}",
    summarize_threshold=30,
    summarize_fn=_summarize_old_turns,
)
```

---

## 6. 项目实践索引

| 项目 | 历史策略 | 流式 | 提示词外置 | 备注 |
|---|---|---|---|---|
| aiPet | SQLite + 摘要(30) | ✅ SSE | `app/core/prompts.py` | 拆分为 chat_service/triage_service/safety_checker |
| oneNote | DB（SQLAlchemy） | ✅ NDJSON | `prompts/*.md` + PromptManager | Skill 架构，意图路由 |
| goldenFish | SQLite（negotiation_repository） | ❌（谈判场景不需要） | 内联（待外置） | 谈判状态机 + LLM 价格提取 |
| verseCraft | 无（单轮创作） | ❌ | `app/ai/prompts/` | 已清理死代码，走 nexus |

---

## 7. 常见陷阱

1. **loguru 不支持 `exc_info=True`**：用 `logger.exception(msg)` 或 `logger.opt(exception=True).warning(msg)` 替代
2. **ironman memory.get_history() 是 async**：必须 `await` 后再迭代
3. **FastAPI 默认 `redirect_slashes=True`**：`/templates` 会 307 到 `/templates/`，前端需处理
4. **流式响应必须禁用 nginx 缓冲**：`X-Accel-Buffering: no` header 不可少
5. **SQLite 持久化的并发安全**：使用 `threading.Lock` + `check_same_thread=False`，或改用 `aiosqlite`