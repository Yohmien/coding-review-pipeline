# 验证路由（Verification Router）

本参考将“需要跑哪些验证”尽量程序化：`run_ledger.py verification_tier` 依据 change facts + task facts 输出唯一 tier 建议与 reasons；无法程序化证明时返回 `null`，由 Main 决定。程序只决定 CAN，最终命令仍以仓库实际构建/测试设施为准。

## Verification Tier

输出四档之一：`TARGETED`、`MODULE`、`INTEGRATION`、`FULL`，按优先级取最高适用档：

| 优先级 | 触发条件 | tier |
|---|---|---|
| 1 | task facts `risk == HIGH`，或 transaction/security/concurrency/external side effect 候选为 `candidate/confirmed` | `FULL` |
| 2 | `migration_changed`，或 `dependency_manifest_changed`/`lockfile_changed`，或 contract/interface 类文件，或 `public_api_candidate` 命中 | `INTEGRATION` |
| 3 | 变更跨多个 module | `INTEGRATION` |
| 4 | 变更落在单个 module | `MODULE` |
| 5 | `tests_changed`，或仅单个文件变更 | `TARGETED` |
| — | 以上都无法证明（如无 module、无测试、多文件且无触发） | `null`（Main 决定） |

结果形如 `{"tier": "MODULE", "reasons": ["single module changed"]}`；无法证明时为 `null`。source↔test 映射可程序化时给出 `TARGETED` 建议，否则由 Main 决定。

## 验证记录

每条验证记录固定为：

```json
{
  "command": "...",
  "exit_code": 0,
  "failure_count": 0,
  "diff_fingerprint": "...",
  "timestamp": "..."
}
```

硬约束：

- 缺 `exit_code`（或非整数）的验证记录一律视为无效，不得作为通过证据。
- 验证记录必须关联 `diff_fingerprint`；当前 diff 指纹与记录不一致即 STALE，必须重跑，不得沿用旧结论。
- 部分验证不得表述为全部通过；未运行项必须逐项列出原因、影响与残余风险。

## 证据要求（verification-before-completion）

宣称“完成/修复/通过”前，必须有最新命令、完整关键输出、退出码、失败数与实际文件清单。父会话独立重跑所有用于结论的命令，不以 coder 或 reviewer 的自述代替。

## 测试三档

对每项改动归入一档并记录依据：**可测**（新增/更新测试并保留红绿证据）、**客观不可测**（说明阻断事实与替代检查）、**用户批准豁免**（记录批准范围，不写成验证通过）。

红绿证据以目标行为缺失导致的失败为准，不得用语法/依赖/环境错误充当红态；实现前无法保留自然红态时使用五步红变体并确认 diff 不含临时变体。

## 场景验证（Scenario Validation）

主会话在探索阶段向用户询问并确认的冒烟测试场景（见 SKILL.md 第 1 节第 6 条），作为 packet 验证段的独立场景验证条件。每条场景条件按以下结构写入 ledger 的 `scenario_checks`：

```json
{
  "scenario_id": "S1",
  "description": "用户链路的一句话摘要",
  "steps": [
    {"action": "操作/输入", "expected": "预期结果", "pass": true}
  ],
  "all_pass": true,
  "executed_by": "main_session",
  "timestamp": "..."
}
```

硬约束：

- 自动化测试全绿但 `scenario_checks` 缺失或 `all_pass != true` 时，视为验证不完整，completion_gate 输出 `BLOCKED`。
- 场景步骤由主会话在复验阶段逐步执行；不得以"代码逻辑上应该成立"代替实际执行。
- 场景验证结果与 diff_fingerprint 关联；diff 变化后必须重跑受影响的场景步骤。
- 每条 scenario check 必须携带 executable 字段，指向可执行的测试名或命令；packet 阶段由 validate_task_packet.py 校验（non_executable_scenario_check 即 BLOCKED）。人工推演只能作为补充证据，不能替代 executable 检查的执行。

## MUST 约束映射（Constraint Mapping）

计划声明 MUST/关键约束时，主会话在阶段 4 建立约束注册表并写入每个 packet 的 CONSTRAINT_MAPPINGS：约束 id → 具体验证条目。validate_task_packet.py --plan-constraints 校验覆盖完整性；completion_gate 在收尾时复核任务仍携带非空 constraint_mappings（plan.has_must_constraints=true 时缺失输出 missing_constraint_mappings）。

## 未跟踪文件

- 指纹与验证记录必须覆盖未跟踪新文件路径：`change_facts.py` 输出的 `untracked_files` 为路径
  字符串列表；`diff_fingerprint` 覆盖 `changed_files / untracked_files / diff_ranges`。change_facts
  的工作树快照内部会对 untracked 计算路径 + 内容 SHA-256，但仅用于快照指纹、不作为独立输出字段；
  未跟踪新文件的内容级核验由 `git add -N` 后的 diff 与主会话实际读取提供，不得仅按已跟踪 diff
  判断新鲜度。
- `git diff --check` 不覆盖未跟踪文件：存在 untracked 新文件时，先对每个新文件执行
  `git add -N <file>`（intent-to-add，仅让 diff 可见）再运行检查；不得因此顺手 `git add` 提交。
- `diff_fingerprint` 必须基于含 `untracked_files` 的最新 change facts（字段覆盖
  `changed_files / untracked_files / diff_ranges`）；任何验证记录 / verdict 不得绑定
  不含 untracked 的旧指纹。
