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

## 收尾处置（第三波，2026-09-17）
### 节点侧代码对齐（已完成）
- 判定「节点本地改动是否为独有工作」的决定性方法：导出节点 `git ls-files -s` 的 blob 哈希，逐个 `git cat-file --batch-check`
  在上游对象库校验——**全部命中即证明内容来自上游历史**（本次 317/329/328 个文件 0 独有），可安全对齐。
- 对齐流程：`git diff HEAD > 备份.patch` → `git stash push` → `git checkout -B main origin/main`；仅 3 个与 main 内容
  完全一致的未跟踪产物文件（portal-apps.js/robots.txt/sitemap.xml）需先移除。重启平台服务后节点生成器即带收口规则
  （apps.conf 中 `return 404` 计数：edge-02 126 / edge-03 105 / edge-04 91）。
- 平台自身重启的授权现实：worker 用 `sudo -n systemctl restart minideploy-{god,edge03}.service`；master 的 sudo 白名单
  只有 nginx，靠 `Restart=always` + `kill <MainPID>`（进程属主即登录用户）自动拉起。重启后平台会自动拉起其托管应用，
  应用恢复需数十秒（期间 health 000/404 属启动窗口，非故障）。

### 云侧网络收敛（已完成/部分）
- edge-03 从共用安全组 `sg-hfu0bvqa`（与 master 共享）拆出独立组 `sg-bvcrptm2`：公网仅 22/51820，80/443 限 `10.100.0.0/24`。
- **API 陷阱**：本账号 tccli 内置模型缺少 `VpcId`/`AssociateSecurityGroups`，且 VPC 服务返回 `InvalidAction`；
  绑定/解绑安全组实际由 **cvm 服务** 的同名动作提供（`AssociateSecurityGroups`/`DisassociateSecurityGroups`），
  直接用 TC3 签名脚本调用可绕开模型缺失；`CreateSecurityGroupPolicies` 不支持同时传 Ingress+Egress，需分两次调用。
  另注意 tccli 不传 `--region` 会落到默认地域（曾把安全组误建到广州，已删除）。
- 同一账号内 CVM 仅 edge-01(ins-mdisasdp)/edge-03(ins-8x58dcw1)、轻量云 edge-02(lhins-kfpge7my)；
  **edge-04 不在该账号**（另有 82.156.91.172 北京轻量实例，非集群节点），其公网 80/443 只能靠 iptables 兜底，需在所属控制台收敛。

### 「占位符未被解析 → 密钥退化为公开常量」（新发现的同类高危）
- 机制：`minideploy.yaml` env 的 `${VAR}` 需要平台 env/secrets 提供真值；worker 侧 `merge_yaml_env_into`
  仅在**已存在真值**时跳过占位符，否则把 `${VAR}` 字面量写入 `.minideploy_env`，应用照常读环境变量。
- 命中案例（`adSmart`）：`.minideploy_env` 里 `ADSMART_JWT_SECRET="${ADSMART_JWT_SECRET}"`，
  而应用代码「env 优先、Lion 兜底」→ 扩展令牌实际用公开字符串签名，任何人可伪造扩展身份。
  修复：从 minideploy.yaml 移除该 env/secrets 声明（密钥收归 Lion `business/extension_secrets` 单一来源）→
  轮换 Lion 密钥 → 清理 env 文件 → 重启；验证 8 项（新密钥签发/旧密钥与占位符签名全部 401、新密钥正常 200）。
- 同批排查（配置卫生，实测未被鉴权链路消费）：`aiPet`/`verseCraft` 的 JWT/SECRET_KEY（鉴权走 nexus/UC，
  SECRET_KEY 无消费点）、`golden` 残留行、`challengePlanet` 的 `PM_GATEWAY_API_KEY`（网关鉴权统一走 Lion infra）、
  `financialKG` 的回调地址（真值在应用 config.yaml）。均已清理；aiPet/verseCraft 已补随机真值并重启。
- 固化门禁：`miniDeploy/app/standards/plugins/env_placeholder_resolved.py`（block 级，扫 `.env`/`.minideploy_env`
  的纯 `${VAR}` 值），已登记 `config/standards.yaml` 并部署到四节点；增量部署中因文件不在变更集自动降级为 warn，
  全量审计场景为 block。

### 网关测试 key 与残留
- 停用 `gw-9dfe`(e2e-test)/`gw-7bad`(vision-e2e)（DB `is_active=0` + 重启 promptManager 刷进程内缓存）；
  仅 `gw-df4e…`(default-20260917) 为在用生产 key，lion 中 129 条配置 0 条引用旧 key。
- `gw-25f4`(default-llm) 仍启用但无任何配置引用、末次调用 2026-09-14，保留待人工确认。
- **不做**「lion llm 组明文 key 改 `${PM_GATEWAY_API_KEY}`」：占位符在 worker 侧无解析器（见上），
  改造会让全站 LLM 调用拿字面量 key 直接 401；Lion 本身即权威配置源，风险由「轮换 + 入口收紧 + 门禁」覆盖。

### 未覆盖（需用户侧）
- ~~智谱 provider key（lion `promptManager/business/provider_keys`）：需智谱控制台换 key 后回填。~~ 已轮换+旧 key 下线（2026-09-17）。
- 腾讯云 API 密钥（本次任务在对话中以明文传递）：控制台轮换。
- edge-04 云安全组公网 80/443（不在账号内）——已解决（见下第五波）。

## 第四波复核（2026-09-17，网信办文档送达后系统化复查）
### 复核结论：三波处置全部落地生效，公网零暴露
- 公网实测：`songguokr.com:9527` 直连超时（安全组收敛）；`/lion/*` 公网 404（is_public=false 只入内网 server block）；
  全 14 公开应用 `/docs` `/openapi.json` 公网 404（nginx 收口规则 + 大小写规范化 301→404，verseCraft 已验证无泄露）。
- 认证行为：master 本机 lion 9527 与经 nginx 路径——伪 Bearer test/无 token/`/docs` 全 401；真 SERVICE_TOKEN 200（非占位符，len=43）。
- 四节点 nexus-backend 中间件代码全部修复（allow_user_tokens=True / allow_bearer_passthrough=False），
  master 用系统 python3（editable apps/nexus-backend），worker 用 venv312（editable ~/workspace/nexus-backend）。
- nginx 收口规则数：edge-02=126 / edge-03=105 / edge-04=91；edge-03/04 iptables 80/443 限 10.100.0.0/24 生效。
- 全节点敏感端口公网扫描（8900/8901/8910/8250/8999/8700/8100/9527/8200/51820）：全部 closed；仅 master 22/80/443 与各节点 22 开放。
- 腾讯云 API（TC3 签名直查，注意安全组接口在 vpc 服务、Limit 需字符串）：
  master sg-hfu0bvqa 入站仅 22/80/443/51820；edge-03 sg-bvcrptm2 入站 22/51820 + 80/443 限 10.100.0.0/24；
  edge-02 轻量云防火墙 22/51820/ICMP + 80/443 限 10.100.0.0/24；广州 sg-d6wp76tj 为无实例默认组（可清理，无害）。
- lion 库密钥盘点：仅 `gw-df4ee79a900…`（轮换后新 key）出现 9 次；旧泄露 key（gw-3007c3e3/gw-9dfe/gw-7bad/gw-25f4）零残留；
  UC app_secret 全量轮换（audit 2026-09-17 03:52 UTC）。门禁插件 env_placeholder_resolved 已注册四节点。
- LLM 网关（8400）healthz 200 正常。

### 新增发现（上轮遗漏，需用户侧处理）——已轮换（2026-09-17 第五波后续）
- `promptManager/business/provider_keys` 曾明文存两条**模型直连 key**（网信办通报的同类泄露面）：
  - 智谱 zhipu：`5f0f260f…`、DeepSeek：`sk-e0ee85d3…`
  - 该配置最后修改 2026-09-15 13:05（UTC），早于 9-17 gw- 轮换波次，两 key 均视为泄露。
- **已处理**：走 lion API（保留审计 id=1052）替换为智谱/DeepSeek 控制台新 key → 平台 agent 重启 promptManager(8400)
  → 真实调用验证 glm-4.5-air 与 deepseek-flash 均 200（新 key 生效，网关 DB Fernet 加密存储）。
- **已下线**：智谱旧 key `5f0f260f…`、DeepSeek 旧 key `sk-e0ee85d3…` 已于 2026-09-17 在厂商控制台完成下线，第三方侧旧凭证全部清理。
- 轮换清单剩余：腾讯云 API 密钥（对话明文传递）、SERVICE_TOKEN（见下，未轮换）。

### 经验补充
- 安全组/防火墙类 API 的坑：安全组查询走 `vpc.tencentcloudapi.com`（非 cvm）；`Limit`/`Offset` 参数类型因接口而异
  （DescribeSecurityGroups 要字符串、DescribeSecurityGroupPolicies 不支持分页参数）；误传参数会被静默当空结果，
  必须打印原始 Response 校验 `Error`。
- 复核敏感配置盘点要用**脱敏脚本**（值只显示前缀+长度），且掩码正则要覆盖 `"keyname": "value"` 形态
  （只匹配 key/secret/token 字段名会漏掉 zhipu/deepseek 这类 provider 名）。

## 第五波：edge-04 云侧收敛（2026-09-17）
- edge-04（106.54.15.201）为**独立腾讯云账号**的轻量云 `lhins-k42ft2tk`（ap-shanghai，用户新提供密钥）。
  云侧防火墙原状：22/80/ICMP 全 0.0.0.0/0 开放，80 公网可达（此前仅靠节点 iptables 兜底）。
- 收敛动作（`DeleteFirewallRules`+`CreateFirewallRules` 增量，避开 ModifyFirewallRules 整表替换陷阱）：
  80/443 → 仅 `10.100.0.0/24`；补 `UDP 51820 → 0.0.0.0/0`（WireGuard mesh 入向）；保留 22/ICMP。
- 验证：edge-04 公网 80/443 均 closed；master↔edge-04 WG 双 peer 活跃（handshake <2min、GB 级流量）；
  nginx 内网 200；7 个业务端口（8001/8002/8004/8005/8220/8610/8900）正常。节点 iptables 兜底保留（双保险）。
- 至此四节点云侧+节点侧全部收敛：master sg-hfu0bvqa、edge-03 sg-bvcrptm2、edge-02 轻量防火墙、edge-04 轻量防火墙。
- 备注：第一账号另有北京轻量 `lhins-1bkcdlbe`(82.156.91.172) 非集群节点，8080/80 公网开放，未擅自改动（非业务资源，待用户决定）。
