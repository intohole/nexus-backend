# nexus-backend

全工作区 Python 后端的统一公共基础设施库，为本仓库所有 FastAPI 服务提供认证、LLM、配置中心、数据库、日志、中间件与监控等标准能力，消除各应用重复造轮子。

## 项目简介

nexus-backend 是 `remoteWork` 工作区的基础依赖包，以 `nexus.xxx` 命名空间对外暴露能力。它抽象了跨应用通用的横切关注点：统一的用户认证（对接 User Center）、大模型调用（对接 ironman）、动态配置（对接 Lion 配置中心）、数据库会话管理、日志与请求链路、服务间鉴权中间件、SSE 推送、限流与熔断、成本预算与监控等。业务后端只需 `import nexus` 即可获得标准化能力，符合"中间件统一使用、禁止自实现"的规范约束。

## 核心能力

- 认证统一：`nexus.auth.AuthDependencies` 对接 User Center SDK 校验 JWT，支持必选/可选用户、本地用户同步、Token 缓存
- LLM 统一：`nexus.llm.LLMService` 封装 chat/ask/ask_json/stream/embed，内置重试、熔断、预算(stringify)、JSON 解析
- 配置中心：`nexus.lion` 封装 LionSDK，支持 chat/embed/image/infra/business 配置获取与 60s 缓存热更新
- 数据库层：`nexus.database` 提供 SQLAlchemy 异步会话管理、`db_manager`、`Base`
- 中间件：CORS、ServiceAuth（服务间鉴权）、Logging、RequestId、NoCache、异常处理、Splash
- 通知能力：`nexus.notify.NotifyClient`、`nexus.channels`（in_app/email/webhook 三方推送通道）
- 运维能力：`nexus.boot` 健康检查/静态资源挂载、`nexus.scheduler` 定时任务、`nexus.rate_limit`、`nexus.circuit_breaker`、`nexus.llm_metrics`、SSE 管理
- 其他：日志（loguru）、请求上下文、限流、结构化输出校验、web_search、deep_research 等工具

## 技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| 语言 | Python 3.11+ | 全异步实现 |
| Web | FastAPI | 中间件与依赖注入 |
| LLM | ironman | LLM/Embedding 统一网关 |
| 认证 | User Center SDK | JWT 校验与用户信息 |
| 配置 | Lion SDK | 动态配置热更新 |
| ORM | SQLAlchemy 2.0 | 异步会话管理 |
| 日志 | loguru | 统一日志管道 |

## 快速开始

nexus-backend 作为 Python 包被各业务应用依赖，通过 `requirements.txt` 安装：

```bash
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
# 或直接安装本包
pip install -e ./nexus-backend
```

接入示例：

```python
from nexus.llm import get_llm_service
from nexus.auth import get_current_user_id_required
from nexus.lion import get_chat_config

llm = get_llm_service()
text = await llm.ask(prompt="你好", system="你是助手")

cfg = await get_chat_config()  # 从 Lion 拉取动态配置
```

## 项目结构

```
nexus-backend/
└── nexus/
    ├── auth*.py          # 认证依赖与用户信息
    ├── llm*.py           # LLM 服务、预算、缓存、优化器、限流
    ├── lion.py           # Lion 配置中心集成
    ├── boot.py           # 健康检查、SPA 静态挂载、服务鉴权注册
    ├── database.py       # SQLAlchemy 异步会话
    ├── middleware*.py    # CORS/ServiceAuth/日志/异常/请求ID 等
    ├── channels/         # 通知通道（in_app/email/webhook）
    ├── notify.py         # 通知客户端
    ├── scheduler.py      # 定时任务
    ├── context.py        # 请求上下文
    ├── errors.py         # 统一异常
    └── ...               # 其余基建模块
```

## 服务依赖

- **依赖**：`ironman`（LLM 协议）、`usercenter`（UC SDK/认证）、`lion`（LionSDK/动态配置）
- **被依赖**：本仓库几乎全部 Python 后端（chroma-embedding-server、lion、notifyCenter、fastRPC、usercenter、beeMemory、promptManager 等）均通过 `nexus-backend>=1.6.0` 引用

## 部署

nexus-backend 不是独立可部署服务，而是作为依赖随业务应用一起部署。其敏感配置（UC app_secret、SERVICE_TOKEN 等）由各应用通过环境变量 `${VAR}` 注入，不在此包内硬编码。

## 许可证

MIT License