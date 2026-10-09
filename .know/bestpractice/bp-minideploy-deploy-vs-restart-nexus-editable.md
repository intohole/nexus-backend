---
id: bp-minideploy-deploy-vs-restart-nexus-editable
title: miniDeploy部署成功不等于进程重启，nexus库经editable install生效
summary: miniDeploy返回'deploy成功'仅表示代码/配置更新，进程可能未重启（手册明示须核对pid）；nexus-backend以pip install -e指向master的~/workspace/nexus-backend，库改动生效=master git pull+应用进程重启
type: bestpractice
project: nexus-backend
date: 2026-10-08
tags: [bestpractice, pattern]
scope: project
related: []
---

# miniDeploy部署成功不等于进程重启，nexus库经editable install生效

## 场景
改 nexus 中间件代码并推送后，调用 miniDeploy deploy 目标应用，发现新逻辑未生效。

## 做法与原因
1. miniDeploy POST /api/apps/{name}/deploy 返回 success 仅代表代码同步成功；进程重启可能被跳过。必须核对 ps 的 pid/启动时间，未变则强制 POST /api/apps/{name}/restart（手册 09 第三节明示此坑）
2. nexus-backend 在 edge-01 master 以 editable install（pip install -e）装在共享 venv312，direct_url 指向 /home/cool/workspace/nexus-backend；各应用（如 beeMemory 8700）通过该 venv 引用 nexus 包，应用 PYTHONPATH 中并无 nexus-backend 路径
3. 库改动上线流程：本地 commit+push → ssh master 执行 cd ~/workspace/nexus-backend && git pull --ff-only origin main → miniDeploy 部署/重启引用该库的应用进程
4. 确认生效方法：ps -o lstart 核对进程启动时间 + 直接调目标端点验证行为（如 GET /api/auth/discover）

## 反例
- 只看 deploy 返回 success 就宣告完成 → 旧进程继续跑旧代码，问题'未修复'
- 误以为改 miniDeploy/apps/nexus-backend 目录即可 → 真实生效路径是 ~/workspace/nexus-backend（editable 直指）
