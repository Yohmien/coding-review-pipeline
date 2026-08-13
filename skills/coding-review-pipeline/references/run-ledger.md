# 持久 Run Ledger（run_ledger.py）

`scripts/run_ledger.py` 负责把一次 CRP 运行的状态持久化为单一 JSON 文件，并在中断/压缩后恢复。它只做可机械确定的持久化与恢复，不替代语义审查，也不引入数据库或 daemon。

## Ledger 位置

Git 仓库与 NON_GIT 统一为全局状态目录：

- `$CODEX_HOME/state/coding-review-pipeline/<workspace-id>/runs/<run-id>/ledger.json`；`workspace-id` 是工作区绝对路径的 SHA-256（跨会话稳定）。不使用 `TEMP`，也不写入 `.git` 元数据区。
- `--codex-home` 对 Git 与 NON_GIT 场景同效，可覆盖默认 `CODEX_HOME`（缺省为 `~/.codex`）。

历史版本曾把 Git 仓库的 ledger 写入 `.git/coding-review-pipeline/runs/`；`migrate` 子命令可把 legacy 台账迁入全局状态目录（见下文）。

写入全局状态目录时，workspace-write 沙箱通常只允许写工作区根；申请仅限 `$CODEX_HOME/state/coding-review-pipeline/` 的最小升级即可，禁止 danger 模式或改 `.gitignore` 绕过。

同一仓库存在多个 run 时，用 `list` 子命令列出 `run_id / plan_summary / created_at / base / stage`，由用户决定恢复哪一个，禁止程序猜测。

## Run ID 规则

`run_id` 必须满足全部条件，任一违反 → `invalid_input`（exit 2）：

- 非空字符串，字符仅允许 `[A-Za-z0-9._-]`。
- 不是 `.` 或 `..`。
- 长度 ≤ 64。
- 不是 Windows 保留设备名：`CON` / `PRN` / `AUX` / `NUL` / `COM1-9` / `LPT1-9`，含任意扩展名形式（如 `CON.json`）同样拒绝；比较对第一个 `.` 之前的名称部分大小写不敏感。

## 主结构（schema_version = 1）

顶层键固定为：`schema_version`、`run_id`、`repo_root`、`plan`、`baseline`、`models`、`decisions`、`tasks`、`agents`、`integration`、`events`。

- `plan`：原始计划对象；`baseline.plan_fingerprint` 保存其指纹，`baseline.created_at/base/stage` 供恢复展示。
- `decisions`：决策注册表（`id/domain/owner/status/value/evidence/affects`）；`owner` 只允许 `user/main/advisor`，coder 不能成为高影响 decision owner。
- `events`：追加式事件流（每次写追加，不覆盖）。
- `integration`：字符串标量记录段，写入按 key 字段级合并到既有 object；供 completion_gate 读取顶层
  `latest_verdict` 与 `verdict_diff_fingerprint`。既有 ledger 无需迁移，`integration={}` 天然合法。

## Task Ledger

每个 task 至少含：`task_id`、`deliverable`、`write_set`、`read_only`、`dependencies`、`state`、`owner_coder`、`reuse_policy`、`actual_changed_files`、`verification`、`review_round`、`latest_verdict`、`verdict_diff_fingerprint`、`pending_fix`、`pending_audit`。

`latest_verdict` 与每条 `verification` 记录都必须关联 `diff_fingerprint`，用于新鲜度判断。

## Agent Ledger

每个 agent 至少含：`agent_id`、`role`、`task_id`、`decision_id`、`reuse_policy`、`lifecycle_state`、`close_eligible`、`last_observed_runtime_state`、`pending_fix`、`pending_review`。

`wait_observation_count` 仅允许作为诊断字段记录，绝对禁止进入任何 kill/close/redispatch 决策。

## 指纹

- `plan_fingerprint(plan)`：稳定 hash 覆盖 `objective/tasks/dependencies/interfaces/constraints/acceptance/decisions`。恢复时与当前计划不一致 → `STOP`，返回 `plan_reconfirmation_required`，由 Main 重新确认计划。
- `diff_fingerprint(change_facts)`：稳定 hash 覆盖 `changed_files/untracked_files/diff_ranges`。每次代码状态变化由 Main 重新生成；所有 verification / verdict / finding 都绑定它。

## 验证记录与新鲜度

每条验证记录固定为：`command`、`exit_code`、`failure_count`、`diff_fingerprint`、`timestamp`。

- `valid_verification(record)`：只有携带整数 `exit_code` 才算有效验证；缺失 `exit_code` 一律无效。
- `is_verification_fresh(record, current_diff_fingerprint)`：记录的 `diff_fingerprint` 等于当前指纹才新鲜；否则 STALE，必须重跑。

## 恢复语义（resume_state）

按固定优先级恢复，禁止猜测：

1. 加载并校验 Ledger（损坏 JSON → 结构化 `invalid_input` 错误，不崩溃、不重建）。
2. 校验 `plan_fingerprint`（不一致 → STOP，计划重确认）。
3. 逐 task 决定 `resume_action`：
   - 存在仍运行的 agent（`lifecycle_state` 为 ACTIVE/RUNNING/WAITING_VERIFICATION/WAITING_AUDIT/FIX_REQUIRED，或 `last_observed_runtime_state` 为 running/active）→ `query_first`：先查询该 agent，不重复派发。
   - `latest_verdict == "ship"` 且无 `pending_fix` → `no_redispatch`：已完成任务绝不重派。
   - `latest_verdict == "fix-first"` 且 `pending_fix` → 回原 `owner_coder`：`resume_same`；无 owner 则 `blocked_no_owner`。
   - 其余未完成任务 → `continue`。

## 原子写

复用 `crp_common.atomic_json_write`：写临时文件 → flush → `os.replace`。写失败时旧 Ledger 保持完整有效，绝不留下半截 JSON。脚本自身不重写该实现。

## Update 写门禁

`update --changes <json>` 先合并、再整体校验、最后落盘：

- 硬违规（合并后 ledger 结构不合法，如 `tasks`/`agents`/`decisions`/`integration` 非 object、`events` 非 list、plan 形状非法）→ `invalid_input`（exit 2），不落盘。
- integration 写入门禁：本次 update 写入 `integration` 的值必须是字符串，任何 dict / list / number / null
  值 → `invalid_input`（exit 2），不落盘（`_validate_integration_write`）。
- `tasks`/`agents`/`decisions` 软违规：本次 changes 写入这些 section 的条目中包含非 dict 值 →
  `invalid_input`（exit 2），不落盘；未写入的 section 携带的既有软违规不阻塞 update。
- 读路径（load / list / resume）容忍既有 ledger 中的遗留非标量 integration 值，不因 integration 条目类型
  拒绝读取；顶层 integration 仍必须是 object。

## 畸形可解析 Ledger 逐命令映射

可解析为 JSON 但结构不合法的 ledger 按命令分别处理：

| 命令 | 行为 | 退出码 |
|---|---|---|
| `load` | 任何结构违规 → 结构化 `invalid_input` 错误，不重建、不崩溃 | 2 |
| `resume` | `baseline` section 违规 → 结构化 `plan_reconfirmation_required`（无法证明计划新鲜度）；软违规（section 为 object 但条目含非 dict 值）→ 结构化降级，按可证明事实给 `query_first` / `no_redispatch` 等 resume_action，照常返回 resume_state；其余硬违规 → `invalid_input` | baseline 违规 3；软违规 0；硬违规 2 |
| `list` | 单个 run 损坏 → 输出 `corrupt: true` 与 error 条目，其余 run 正常列出 | 0 |
| 全部命令 | exit 1 仅保留给编程错误；任何输入问题不得产生 1 | — |

## Plan 谓词 require_tasks 分阶段口径

`validate_plan_tasks(plan, require_tasks)` 是 `plan.tasks` 形状的唯一权威校验：

- `require_tasks=False`（load / init 路径）：plan 缺 `tasks` 键时返回 None（允许 early-ledger 计划尚无任务）；一旦存在 `tasks`，其形状仍必须合法（非空 list；条目为非空 string，或可解析出非空 string task ID 的 object；mapping key 必须为 string）。
- `require_tasks=True`（completion_gate 路径）：plan 缺 `tasks` 键即 invalid。

completion_gate 对 plan 的两种失败区分：plan schema 不合法（`validate_plan_tasks` 抛错）→ `invalid_plan`；plan 合法但 `baseline.plan_fingerprint` 缺失或与当前 plan 指纹不一致 → `plan_stale`。

## migrate 子命令

`run_ledger.py migrate --repo <path> [--codex-home <home>]` 把旧版写入 Git `.git` 元数据区的 legacy 台账迁入全局状态目录：

- legacy 位置仅用 `git rev-parse --git-path coding-review-pipeline/runs` 检测；repo 非 git 或 rev-parse 失败 → 无 legacy、no-op。
- 对每个 `<run-id>/ledger.json`：全局目标已存在 → `skipped`（幂等）；不通过全量形状校验 → 记入 `corrupt`、不迁移；否则原子写副本到全局路径，并追加一条 `kind=migration` 的 events 记录（note 含来源路径与时间）。legacy 源保留、不删除。
- 输出结构化 JSON：`{"ok":true,"migrated":[...],"skipped":[...],"corrupt":[...],"noop":bool}`；成功 / no-op / skip / corrupt 均 exit 0（corrupt 与 `list` 口径一致，属报告性标记），非法参数 exit 2。

## CLI

子命令：`init` / `update` / `load` / `list` / `migrate` / `resume` / `verification-tier` / `plan-fingerprint` / `diff-fingerprint`。输出 UTF-8 JSON；退出码 `0` 正常、`2` invalid_input、`3` policy_blocked（含 plan mismatch 的 STOP）、`1` internal_error（仅编程错误）。
