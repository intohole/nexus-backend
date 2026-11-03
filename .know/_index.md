# Knowledge Index
> Global | Updated: 2026-11-04 | Total: 7 entries

## bestpractice
- bp-minideploy-deploy-vs-restart-nexus-editable | miniDeploy返回deploy成功仅表示代码/配置更新，进程可能未重启（手册明示须核对pid）；nexus-backend以pip install -e指向master的~/workspace/nexus-backend，库改动生效=master git pull+应用进程重启 | 2026-10-08
- bp-notify-batch-send |  | 
- q-20261104-001827 | 超 400 行文件拆分判据是职责块数量不是行数：单类内聚文件（nexus llm.py 413 行单类/voice_session.py 419 行单类）拆分=职责单元搬家零收益判留；多职责块文件（streaming.py 498 行四块同居）才立拆分案，facade 三层单向依赖（primitives←机制←facade）+ 全符号 re-export 保导入面，测试收集器 ImportError 就是导入面守底（漏 re-export 当场红） | 2026-11-04

## bugs
- bug-asyncio-lock-selfdeadlock | 根因：持锁期间经嵌套调用链再次获取同一把非重入锁（asyncio.Lock/threading.Lock），永久挂起且无日志；修复：异步锁按 current_task 重入 + 刷新期哨兵回退缓存 + threading 改 RLock | 2026-09-29
- bug-lion-swallow-error-empty-discover | LionSDK把httpx异常吞成success:False返回值而非抛异常，_fetch_business_config只捕异常导致错误dict静默透传，get_uc_auth拿到空app_key，前端报认证服务未配置且后端零日志；修复：lion.py business/infra路径加last-good回退+失败warning日志 | 2026-10-08
- bug-service-auth-bearer-bypass |  | 

## refactor
- rf-dup-scan-self-blindspot-fix | 克隆检测器归一化四件套修 r154 盲区（簇 4→15 全真）；AST 归一化改动必须标本四连先行，unparse 回环假名须合法标识符 | 2026-11-04
