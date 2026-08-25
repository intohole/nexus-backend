# 批量提醒推送：聚合 + 中间件 send_many

## 场景
业务侧需要给 N 个待办对象（挑战/任务/提醒）按用户聚合后，每用户推送一条提醒。
单条推送失败不应中断整批，否则一个用户发送异常会阻塞其余用户触达。

## 做法
业务 service 只负责：「组装」与「聚合」——

1. 把未完成对象按 `user_id` 分组（`grouped.setdefault(user_id, []).append(item)`）
2. 每组算出该用户的聚合文案/标题/优先级，组装成一条 payload `dict`
3. 收集成 `items: list[dict]`，统一交给中间件
   `client.send_many(items)` 批量发送

中间件 `nexus.notify.NotifyClient.send_many` 统一承担：
- 遍历逐条调用 `send(**item)`（复用既有 payload 序列化/HTTP/错误吞并）
- 单条异常 `except Exception` 捕获隔离，记录日志，返回 `{}`，不中断整批
- 返回每条发送结果 `list[dict]`

## 原因
- 单条失败隔离是跨项目通用诉求，收敛到中间件，避免每个项目重复写 try/except 循环
- 业务只保留「聚合+语义组装」这一业务差异点，传输/容错复用同一套

## 反例
在业务 service 里手写 `for ...: try: await client.send(...)` 循环，阻塞整个循环、
重复造轮子、异常处理口径不统一。

## 参考实现
- 中间件：`nexus/notify.py::NotifyClient.send_many`
- 业务示例：`challengePlanet/app/services/reminder_service.py::send_checkin_reminders`