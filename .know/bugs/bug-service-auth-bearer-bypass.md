# 服务间认证网关「任意 Bearer 放行」高危绕过（网信办通报）

## 现象
`nexus.ServiceAuthMiddleware` 对 `Authorization: Bearer <任意字符串>` 一律放行，导致公开暴露的 lion 配置中心
（songguokr.com:9527）匿名可读全量配置（43 命名空间 / 227 条配置，含网关 api_key、31 个 UC app_secret），
且 FastAPI `/docs`、`/openapi.json`、`/redoc` 在默认白名单中匿名可读。

## 根因（两类，同一模式）
1. `allow_bearer_passthrough=True` 默认值：只判断"存在 Bearer 头"就放行，不验签、不比对服务令牌。
2. usercenter `app/middleware/service_token.py`：Bearer 仅检查 `split('.')==3` 结构即放行（伪 JWT `a.b.c` 可过闸）。

## 修复
- `nexus/middleware_auth.py`：
  - 新增 `UserTokenVerifier`（RS256 走 UC JWKS，HS256 用 UC_JWT_SECRET 兜底，验签通过结果 60s TTL 缓存，未配置即拒绝）；
  - `allow_user_tokens`（默认 False）语义化替代 `allow_bearer_passthrough`，仅放行真实 UC 用户令牌；
  - 默认白名单移除 `/docs` `/openapi.json` `/redoc`；`/api/_internal` 移出公共前缀；
  - `require_service_token` 依赖保护 `/api/_internal/*`（reload-llm / llm-metrics / llm-circuit）。
- usercenter：Bearer 改为 `jwt_manager.decode_token()` 真实验签（RS256 公钥 + HS256 兜底）。
- 应用接线：lion 关文档且不放行用户令牌；beeMemory/promptManager/challengePlanet/LifeCompass 放行验签用户令牌；
  promptManager 额外放行 `/api/gateway/v1`（网关自有 gw- api_key 鉴权）。

## 验证要点（可复现）
- 伪造 `Bearer test` / `a.b.c` → 401；真服务令牌（X-Service-Token / Bearer）→ 200；
  真 UC 令牌（`POST /api/auth/token` client_credentials 获取）→ 200；篡改令牌 → 401。
- `nexus-backend/tests/test_service_auth_middleware.py` 11 项回归覆盖伪造/过期/伪签名/alg=none/白名单边界。
- 端到端：LLM 网关 `gw-` key 调用 200；应用经 lion 读配置正常（LionSDK 用 Bearer 服务令牌）。

## 网络层（同等重要）
edge-01/edge-03 安全组为「全端口 0.0.0.0/0 放行」，内网服务端口（8xxx、9527）公网可直连。
收敛为仅 TCP 22/80/443 + UDP 51820(WireGuard)，公网入口统一走 nginx；收敛后跨节点 WireGuard 与门户代理均正常。
管理入口：miniDeploy 已收敛到根路径（`/`、`/api/*`），旧的 `/minideploy/*` 前缀别名由 nginx 重生成时移除。

## 密钥轮换
- 网关 `gw-` key：新建（同名额度克隆）→ 全量替换 lion 129 条含 key 配置 → 重启应用载入 → 停用旧 key（hash 校验，重启 promptManager 刷新进程内缓存）。
- UC app_secret：31 命名空间 / 30 app_key 全量重置（usercenter DB 权威 + lion `business/uc_auth` + 平台 env 副本），
  轮换后逐一用 `client_credentials` 校验通过。
- 未覆盖：智谱 provider key（需智谱控制台）、adSmart 扩展 jwt/hmac secret（会使已发放扩展令牌失效）。

## 经验
- 认证网关的「结构校验」（存在头/三段式）等价于无认证；必须验签或常量时间比对登记态令牌。
- 敏感配置一律 `${VAR}` 占位符（lion 模板已如此），禁止落字面量；配置中心自身也是泄露面。
- 内部端口不要暴露公网：单点鉴权失效即全量数据泄露。

## 全站入口收口（第二波）
- 生成器统一拦截：`_security_guard_blocks`（miniDeploy/app/core/nginx_security_blocks.py）为每个应用前缀、门户根路径、
  biz 域名生成 `/docs` `/redoc` `/openapi.json` 与 `/api/_internal/` 的 404 规则；文档仍可经门户代理（需登录）查看。
- 验证：主控入口全站 404；992 次匿名敏感路径探测（31 应用 × 16 路径 × 匿名/伪JWT）零泄露。
- **节点侧坑**：worker 节点（edge-02/03/04）自带 nginx 也会被公网直连（曾直连 IP 拿到 `/wisepath/docs` 200）。
  根因是节点上平台代码存在**未提交的本地改动**（nginx_location_blocks / nginx_biz_builder / nginx_config_builder 等 24~45 个文件），
  无法与上游 fast-forward，收口规则没进节点生成器。
- 处置：① edge-02 走轻量云防火墙把 80/443 收敛到内网 CIDR（`ModifyFirewallRules` 是**整表替换**语义，误删了 22，
  已用 CreateFirewallRules 恢复 22/51820/ICMP）；② edge-03/04 用 iptables 仅放行 `10.100.0.0/24` 访问 80/443，
  并用 `md-net-guard.service`(oneshot + iptables-restore) 做开机持久化。
- 待办：节点侧本地改动需与上游对齐（否则节点生成器长期缺收口规则）；对齐后应把公网 80/443 在云侧关闭（对外统一走主控 nginx）。