# bestpractice Index
> Total: 3 entries

- bp-minideploy-deploy-vs-restart-nexus-editable | miniDeploy返回deploy成功仅表示代码/配置更新，进程可能未重启（手册明示须核对pid）；nexus-backend以pip install -e指向master的~/workspace/nexus-backend，库改动生效=master git pull+应用进程重启 | 2026-10-08
- bp-notify-batch-send |  | 
- q-20261104-001827 | 超 400 行文件拆分判据是职责块数量不是行数：单类内聚文件（nexus llm.py 413 行单类/voice_session.py 419 行单类）拆分=职责单元搬家零收益判留；多职责块文件（streaming.py 498 行四块同居）才立拆分案，facade 三层单向依赖（primitives←机制←facade）+ 全符号 re-export 保导入面，测试收集器 ImportError 就是导入面守底（漏 re-export 当场红） | 2026-11-04
