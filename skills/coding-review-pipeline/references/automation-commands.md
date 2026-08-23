# 自动化命令模板（固定格式，直接复制替换占位符）

占位符约定：`<RUN_ID>` 运行标识、`<TASK_ID>` 任务标识、`<REPO>` 目标仓库路径、
`<FACTS>` change facts JSON 路径。所有命令在仓库根或任意目录可执行；脚本路径相对 skill 根。
占位符约定：`<RUN_ID>` 运行标识、`<TASK_ID>` 任务标识、`<REPO>` 目标仓库路径、
`<FACTS>` change facts JSON 路径。所有命令在仓库根或任意目录可执行；脚本路径相对 skill 根。

- **收集变更事实**：`python scripts/change_facts.py --repo <REPO> --cache-file <RUN_ID>-facts.json --cache-ttl 600`
- **合并路由（多任务，优先用这个）**：`python scripts/route_all.py --facts-args "--repo <REPO>" --route-args "--stage explore --facts <FACTS>" --graph-args "--tasks tasks.json" --facts-out <FACTS> --out merged.json`
- **单任务路由**：`python scripts/route_context.py --stage explore --facts <FACTS>`
- **任务图**：`python scripts/task_graph.py --tasks tasks.json`
- **packet 校验**：`python scripts/validate_task_packet.py --packet <packet.json> --plan-constraints constraints.json`
- **模型选择请求生成**：`python scripts/model_prompt.py --schema schema-snapshot.json --recommend recommend.json`
- **等待时长与测试模式**：`echo {"tasks": N, "files": M, "risk": "HIGH"} | python scripts/wait_strategy.py`
- **读子代理阶段报告**：`python scripts/task_report.py read --run-id <RUN_ID> --task-id <TASK_ID>`
- **写阶段报告（coder 用）**：`echo {...} | python scripts/task_report.py write --run-id <RUN_ID>`
- **review 前置（完整包落盘+索引）**：`python scripts/review_preflight.py --facts <FACTS> --out-index package.json`
- **完成门禁**：`python scripts/completion_gate.py --ledger ledger.json --change-facts <FACTS>`
- **生命周期决策**：`echo {...} | python scripts/agent_lifecycle.py`
