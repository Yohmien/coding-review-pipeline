# 任务级确定性收敛治理

`scripts/task_convergence.py` 管理 task 级 review 收敛、rethink、material
re-contract 与升级。它不管理 agent slot，也不把 task route 加入
`agent_lifecycle.py` 的固定 8 actions。

## 固定任务路由

```text
CONTINUE_FIX
ENTER_RETHINK
RESUME_SAME
SPAWN_SUCCESSOR
SHIP
STOP
TASK_ESCALATION_REQUIRED
```

## 封闭 Material Contract

`old_contract` 与 `new_contract` 必须包含全部 8 个字段；missing 与 `null` 都是
invalid input：

| 字段 | 类型与约束 |
|---|---|
| `DELIVERABLE` | nonblank string |
| `INTERFACES` | nonblank string |
| `WRITE_SET` | nonempty list of nonblank strings |
| `DEPENDENCIES` | list of nonblank strings，可空 |
| `CONSTRAINTS` | nonblank string |
| `ACCEPTANCE` | nonblank string |
| `VERIFICATION` | nonempty list of nonblank strings |
| `DECISIONS` | list of nonblank strings，可空 |

仅允许额外 metadata `task_id`、`name`，且两者为 nonblank string；metadata
不参与 fingerprint。其余未知 contract key（包括拼写错误）一律拒绝。

fingerprint 使用 UTF-8 canonical JSON（key 排序、稳定 separators）与 SHA-256，
只覆盖上述 8 个 material 字段。task id/name 变更不能重置轮次。

## Counter 不变量

- `contract_revision`：整数 1..2；禁止 revision 3。
- `fix_round`：整数 0..3。
- `total_fix_rounds`：整数 0..6。
- revision 1：`total_fix_rounds == fix_round`；正常 review 时
  `rev1_rounds` 必须缺失或 `null`，diagnosis 完成时必须显式等于当前
  `fix_round`。
- revision 2：`rev1_rounds` 必须显式为整数 0..3，且
  `total_fix_rounds == rev1_rounds + fix_round`。
- counter 不允许跳轮、回退或用 7/9 等超限值绕过上限。

路由：

- revision 1/2 的 `fix_round < 3` + `fix-first` → `CONTINUE_FIX`。
- revision 1 的任意 round 收到 `rethink` → `ENTER_RETHINK`，coder 进入
  `PARKED_FOR_RETHINK`；`fix-first` 只有到第 3 轮才进入该路由。
- revision 2 的任意 round 收到 `rethink` → `TASK_ESCALATION_REQUIRED`；
  `fix-first` 只有到第 3 轮才升级。
- 任意一致 counter 状态下、且 `diagnosis_complete=false` 的 `ship`：
  仅当 `old_fingerprint == new_fingerprint` 时 → `SHIP`；否则 → `STOP` 并
  附 `changed_contract_fields` 证据，material change 未经 re-contract 不得
  静默放行。`ship + diagnosis_complete=true` 是矛盾输入（退出码 2）。

## Diagnosis 前态与 re-contract

`diagnosis_complete=true` 只允许以下精确前态：

```text
contract_revision = 1
fix_round = 0..3
total_fix_rounds = fix_round
rev1_rounds = fix_round
prior_route = ENTER_RETHINK
coder_status = PARKED_FOR_RETHINK
```

`diagnosis_complete=false` 时不得携带 `prior_route` / `coder_status`。

material fingerprint 未变化 → `STOP`，保持 revision 1 当前 round；仅改 task
名称或 id 不能重置。fingerprint 变化后输出固定为：

```text
contract_revision = 2
fix_round = 0
total_fix_rounds = 原 fix_round
rev1_rounds = 原 fix_round
```

- 原 coder 先保持 `PARKED_FOR_RETHINK`（agent action 为 `KEEP`，不可 close）。
- successor 证据与 agent 层一致：`coder_unavailability` 枚举
  `unavailable` / `runtime_gone` / `unrecoverable`；
  `original_coder_available=false` 单独出现不再派 successor。诊断完成后按
  以下顺序取第一个命中：
  1. `coder_unavailability` 有值且 `original_coder_available` 不为 `true` →
     `SPAWN_SUCCESSOR`；
  2. 否则 `original_coder_available=true` 且
     `implementer_continuity=preserve` → `RESUME_SAME`；
  3. 否则 `implementer_continuity=successor_recommended` 且
     `responsibility_boundary_changed=true` → `SPAWN_SUCCESSOR`；
  4. 否则 → `STOP`（禁止默认 successor）。
- `coder_unavailability` 仅在 `diagnosis_complete=true` 时允许携带；
  与 `original_coder_available=true` 同现为矛盾输入（退出码 2）。

## 封闭 Failure Context 与 Capsule

revision 2 任意 round 收到 `rethink`，或第 3 轮仍为 `fix-first` 时，
`failure_context` 必填；其他路由禁止携带。
对象必须精确包含以下字段，不允许缺失、空对象或未知字段：

- `goal`：nonblank string。
- `revision_rounds`：必须精确等于实际
  `{"1": rev1_rounds, "2": fix_round}`。
- `diagnosis`：精确字段 `status`、`summary`；`status` 只能为
  `proceed|change|stop`，`summary` 为 nonblank string。
- `changed_assumptions`：list of nonblank strings，可空。
- `current_diff_fingerprint`：nonblank string。
- `verification`：精确字段 `command`（nonblank string）、`exit_code`
  （integer）、`failure_count`（nonnegative integer）、`freshness`
  （固定字符串 `fresh`；`stale` 等其他值拒绝）。
- `current_blockers`、`repeated_signatures`、`unresolved_decisions`：list of
  nonblank strings，可空。
- `safe_workspace_state`：精确字段 `dirty`（bool）、`coder_status`
  （固定 `PARKED_FOR_RETHINK`）、`write_set_preserved`（bool）。

任何层级的 key 只要包含 `raw`、`conversation`、`history`、`secret`
（大小写不敏感）即 invalid input。Failure Capsule 只复制上述已校验结构化字段，
不读取 raw conversation。

## CLI 顶层 allowlist

`route_task` 使用显式参数签名；CLI 未知顶层字段（如 `runtime_stte`）触发
invalid input，不存在 `**context`/`**extra` 吞收路径。允许的顶层字段为：

```text
task_id
contract_revision
fix_round
total_fix_rounds
review_verdict
original_coder_available
implementer_continuity
old_contract
new_contract
diagnosis_complete
responsibility_boundary_changed
coder_unavailability
rev1_rounds
prior_route
coder_status
failure_context
```

stdin/stdout 均为 UTF-8 JSON object。退出码固定：

| 退出码 | 含义 |
|---:|---|
| 0 | 正常路由 |
| 2 | invalid input |
| 3 | `STOP`（policy blocked） |
| 4 | `TASK_ESCALATION_REQUIRED` |

相同输入必须产生逐字段相同输出与相同退出码。
