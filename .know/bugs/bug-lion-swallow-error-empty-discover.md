---
id: bug-lion-swallow-error-empty-discover
title: Lion瞬时故障致discover返回空app_key且零日志
summary: LionSDK把httpx异常吞成success:False返回值而非抛异常，_fetch_business_config只捕异常导致错误dict静默透传，get_uc_auth拿到空app_key，前端报'认证服务未配置'且后端零日志；修复：lion.py business/infra路径加last-good回退+失败warning日志
type: bug
project: nexus-backend
date: 2026-10-08
tags: [bug, fix]
scope: project
related: []
---

# Lion瞬时故障致discover返回空app_key且零日志

## 报错现象
前端登录抛 Error: 认证服务未配置，请联系管理员（app-common.js ensureSdk）。后端 error 日志完全干净，无法排查。当前重试又一切正常，难以复现。

## 根因分析
1. nexus/sdk_base.py _request 把 httpx ConnectError/Timeout/RequestError/parse error 全部吞成 {"success": False, "detail": ...} 返回，不抛异常
2. nexus/lion.py _fetch_business_config/_fetch_infra_config 只捕异常，SDK 返回的错误 dict 原样透传且零日志（只有异常路径有 logger.error）
3. get_business_config 检测到 success:False 不写缓存直接返回错误 dict
4. get_uc_auth 拿到 {"success": False} → app_key 取不到 → discover 返回 enabled:false, app_key:""
5. 前端 ensureSdk 校验 !cfg.app_key 抛错。触发条件：Lion 瞬时抖动（连接超时5s/Lion重启）撞上应用缓存过期或刚重启（缓存清空）

## 修复方法
nexus/lion.py（commit e0c4c6a）：
- LionIntegration 加 _last_good dict，成功拉取时与 _cache 同步落底（不随TTL过期）
- get_business_config/get_infra_config 拉取失败且存在 last-good 时返回旧值并 logger.warning（业务无感，可排查）
- get_business_config_sync 同步读也回退 last-good
- _fetch_business_config/_fetch_infra_config 对 success:False 的错误 dict 补 logger.warning 记录 detail
- llm 配置路径（get_config→LionConfigError）语义保持不变：拒绝静默降级是显式设计

## 防范
- 中间件层吞错误返回值时，所有消费方必须显式检查 success 字段；新写 fetch 封装优先抛异常
- 配置类读取（auth/凭证/base_url）必须有 last-good 或重试，瞬时基建故障不应直接打穿到用户界面
- 契约测试：tests/test_lion_integration_pool.py 覆盖 last-good 回退与从未成功两个分支
