# Knowledge Index
> Global | Updated: 2026-10-08 | Total: 5 entries

## bestpractice
- bp-minideploy-deploy-vs-restart-nexus-editable | miniDeploy返回deploy成功仅表示代码/配置更新，进程可能未重启（手册明示须核对pid）；nexus-backend以pip install -e指向master的~/workspace/nexus-backend，库改动生效=master git pull+应用进程重启 | 2026-10-08
- bp-notify-batch-send |  | 

## bugs
- bug-asyncio-lock-selfdeadlock | 根因：持锁期间经嵌套调用链再次获取同一把非重入锁（asyncio.Lock/threading.Lock），永久挂起且无日志；修复：异步锁按 current_task 重入 + 刷新期哨兵回退缓存 + threading 改 RLock | 2026-09-29
- bug-lion-swallow-error-empty-discover | LionSDK把httpx异常吞成success:False返回值而非抛异常，_fetch_business_config只捕异常导致错误dict静默透传，get_uc_auth拿到空app_key，前端报认证服务未配置且后端零日志；修复：lion.py business/infra路径加last-good回退+失败warning日志 | 2026-10-08
- bug-service-auth-bearer-bypass |  | 
