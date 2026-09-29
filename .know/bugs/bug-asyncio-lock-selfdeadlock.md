---
id: bug-asyncio-lock-selfdeadlock
title: nexus 冷启动自锁：同任务对同一把锁二次 acquire
summary: 根因：持锁期间经嵌套调用链再次获取同一把非重入锁（asyncio.Lock/threading.Lock），永久挂起且无日志；修复：异步锁按 current_task 重入 + 刷新期哨兵回退缓存 + threading 改 RLock
type: bug
project: nexus-backend
date: 2026-09-29
tags: [bug, fix]
scope: project
related: []
---

# nexus 冷启动自锁：同任务对同一把锁二次 acquire

# 报错现象

服务启动永久卡在 "Waiting for application startup"，无任何错误日志（usercenter 卡在 dynamic_config.refresh；任何 nexus 应用设置 NEXUS_CONFIG 后卡在配置加载）。py-spy/_sample 显示主线程停在 lock acquire。

# 根因分析

同任务对同一把非重入锁二次 acquire（asyncio.Lock 与 threading.Lock 均不可重入），三处同型缺陷（nexus-backend，fc0ee2b 修复）：

1. LionIntegration._lock（asyncio）：get_business_config 持锁 → LionSDK._headers → get_service_token → ServiceClient._refresh → get_uc_config → get_infra_config → 再次 async with 同一把锁。
2. ServiceClient._lock（asyncio）：_refresh 内部经 LionSDK._headers 嵌套 get_token，同任务重入 _lock。
3. ConfigFactory._lock（threading）：get() 持锁调 _load_default → NEXUS_CONFIG 存在时 load_from_yaml() 再次 with cls._lock（不设 NEXUS_CONFIG 走默认构造，故仅部分环境复现，极易漏判）。

# 修复方法

- nexus/lion.py：新增 _ReentrantAsyncLock（owner=asyncio.current_task() + depth 计数，同任务重入放行、跨任务互斥），替换 LionIntegration._lock。
- nexus/service_client.py：_refresh 设置 _refreshing_task 哨兵，get_token 检测到同任务刷新中直接返回 get_cached_token()，断开递归。
- nexus/config.py：ConfigFactory._lock 改 threading.RLock。

# 防范

- 排查"启动无日志挂起"先怀疑锁自锁：用 sample <pid> 看是否停在 lock acquire。
- 持锁回调外部代码（SDK header、配置加载）前，先画出嵌套调用图确认不会回到同一把锁；跨模块的"取凭证→读配置→再取凭证"环是高发区。
- threading 用 RLock、asyncio 无原生重入锁，需要时按 current_task 自制；刷新类单飞（single-flight）逻辑要显式防同任务递归。
- 验证：嵌套链路模拟脚本（monkeypatch get_uc_config/headers）+ nexus pytest 103 全过。
