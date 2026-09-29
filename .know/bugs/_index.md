# bugs Index
> Total: 2 entries

- bug-asyncio-lock-selfdeadlock | 根因：持锁期间经嵌套调用链再次获取同一把非重入锁（asyncio.Lock/threading.Lock），永久挂起且无日志；修复：异步锁按 current_task 重入 + 刷新期哨兵回退缓存 + threading 改 RLock | 2026-09-29
- bug-service-auth-bearer-bypass |  | 
