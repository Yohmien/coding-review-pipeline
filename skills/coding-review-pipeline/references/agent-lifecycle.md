# Agent 生命周期治理参考（Phase 8）

所有 coder / reviewer / advisor 的生命周期决定优先由状态机给出。本文件是
`scripts/agent_lifecycle.py` 的语义说明；实现以脚本与测试为准，禁止在输出中
夹带自然语言自由建议。

## 固定复用矩阵

| 角色 | 复用策略 |
|---|---|
| Coder | sticky：fix-first 必须回原 coder |
| Advisor | sticky_within_decision：同一 decision 同一 advisor |
| Task Reviewer / Integration Reviewer | fresh_only：绝不 resume 旧 reviewer |

## 固定动作集

状态机输出只能是以下 8 个动作之一：

```text
KEEP  WAIT  RESUME_SAME  PARK  CLOSE_ALLOWED
SPAWN_SUCCESSOR  SPAWN_FRESH_REVIEWER  STOP
```

任务收敛动作由 [task-convergence.md](task-convergence.md) 单独定义；
`ENTER_RETHINK` 与 `TASK_ESCALATION_REQUIRED` 不属于 agent 动作集。

## Coder 生命周期

```text
UNSPAWNED → ACTIVE → WAITING_VERIFICATION → WAITING_AUDIT
    ├── fix-first → FIX_REQUIRED → RESUME_SAME（原 coder）
    └── ship → PARKED_REUSABLE
              →（淘汰三元组成立）→ CLOSE_ALLOWED

任一审查闭环达到 3 轮且仍未 ship：
PARKED_FOR_RETHINK → KEEP
```

- `fix_first`：只有显式 `original_coder_available=true` 才 `RESUME_SAME`；
  只有显式证据 `unavailability` 为 `unavailable` / `runtime_gone` /
  `unrecoverable` 才 `SPAWN_SUCCESSOR`；字段缺失、`null` 或证据未知时 →
  `KEEP`，绝不默认派 successor。`original_coder_available=true` 与
  `unavailability` 同时出现视为矛盾输入，报错退出码 2。
- `waiting audit`：未完成 audit 不得关闭 → `KEEP`。
- `ship`：任务级 ship → `PARK`（进入 PARKED_REUSABLE）；已 park 时重复 ship
  保持 `KEEP`。
- integration reviewer 之后发现该 Task 问题 → 恢复原 coder：
  `PARKED_REUSABLE` + `integration_fix_needed` → `RESUME_SAME`。
- coder 的 `CLOSE_ALLOWED` 只能由
  `PARKED_REUSABLE + slot_pressure + oldest + shipped + no pending dependent
  fix` 产生；不存在 `CLOSE_ELIGIBLE` 状态，也无无条件旁路。
- `PARKED_FOR_RETHINK` 的 agent action 固定为 `KEEP`；任何事件和 slot
  pressure 都不得关闭、恢复、替换或重新派发。material re-contract 完成后的
  task route 再决定后续 agent 动作。

## 关闭禁区

以下状态无论 slot 压力多大都不得关闭：

```text
ACTIVE  WAITING_VERIFICATION  WAITING_AUDIT  FIX_REQUIRED
PARKED_FOR_RETHINK  ADVISOR_RUNNING
```

## Capacity Eviction

仅当 agent slot 真正不足时，且全部满足才允许淘汰：

```text
oldest parked + task-level ship + no pending dependent fix
```

任一条件不满足 → `KEEP`。关闭前必须持久化完整 handoff（由主会话执行，
脚本只给动作）。

## Reviewer 必须 Fresh

Diff 变化（`diff_changed`）或状态为 `STALE` → `SPAWN_FRESH_REVIEWER`
（关闭旧 R1、派生 R2）。reviewer 永远不返回 `RESUME_SAME`。

Reviewer context purity：只允许 current task contract、current diff、current
file set、current verification、machine findings、risk facts、project
constraints；禁止 coder raw 对话/推理、previous reviewer narrative、main raw
history。旧 finding 是否被处理由主会话/程序追踪，新 reviewer 重新审计当前事实。

## Advisor 生命周期

- 同一 decision 内 sticky，直到 `proceed` / `change` / `stop` / explicit
  blocked / runtime failure / user cancellation。
- verdict `proceed` / `change` → `CLOSE_ALLOWED`（该 decision 结束）。
- `stop` / `blocked` / `cancelled` / `runtime_failure` → `STOP`。
- 运行中无事件 → `KEEP`。

### 等待不等于失败（硬 invariant）

```text
WAIT OBSERVATION TIMEOUT != AGENT EXECUTION FAILURE
```

`wait_observation_timeout` 只能说明 `NO_TERMINAL_EVENT_OBSERVED`，不得解释为
agent stuck。对应动作只有 `WAIT`；`wait_observation_count` 只做诊断，不参与
决策，永远不会触发 kill/close/replace。

### Runtime 锁定边界

`runtime_state` 为 `unknown` / `stale` 时，在角色分派前统一执行
fail-closed：coder、reviewer、advisor 均不得 close、replace 或 redispatch，
只能输出：

```text
KEEP  WAIT  STOP
```

典型结果：verdict 已到但 runtime 状态未知 → `STOP`（不得 `CLOSE_ALLOWED`）；
fix-first + runtime 未知 → `KEEP`（不得 RESUME_SAME / SPAWN_SUCCESSOR）；
diff 变化 + runtime 未知 → `KEEP`（不得 SPAWN_FRESH_REVIEWER）；
wait 观察超时且 runtime 未知 → `WAIT`；slot 压力 + runtime 未知 → `KEEP`。
`runtime_state` 为 `null`/缺失时等同 `known`，正常决策不受影响。

控制字段的角色/事件上下文校验先于 runtime fail-closed。错上下文的
`terminal`、`unavailability` 或 `original_coder_available` 即使搭配
`runtime_state=unknown/stale` 也属于非法输入（退出码 2），不得先触发
`STOP` / `KEEP`。

## CLI

Python 标准库、UTF-8、Windows 可运行。stdin 读一个 JSON 对象，stdout 写一个
JSON 对象；非法输入返回退出码 2 并输出 `{"action": null, "error": "..."}`。

输入字段：

```json
{
  "role": "coder",
  "status": "WAITING_AUDIT",
  "event": "fix_first",
  "original_coder_available": true,
  "unavailability": null,
  "is_oldest_parked": false,
  "task_shipped": false,
  "has_pending_dependent_fix": false,
  "wait_observation_count": 0,
  "terminal": null,
  "runtime_state": "known",
  "context": {}
}
```

- `role`：`coder` | `reviewer` | `advisor`。
- `status`：coder 用生命周期状态；reviewer 用 `RUNNING`/`DONE`/`STALE`；
  advisor 用 `RUNNING`/`DONE`。
- `event`：`fix_first` | `ship` | `slot_pressure` | `diff_changed` |
  `wait_observation_timeout` | `integration_fix_needed` | 空。
- `original_coder_available`：仅允许 `role=coder` 且 `event=fix_first`；取值
  `true` | `false` | `null`（缺失等同 `null`）。只有显式 `true` 才
  RESUME_SAME；`false` 或缺省需配合 `unavailability` 证据，否则 KEEP。
- `unavailability`：仅允许 `role=coder` 且 `event=fix_first`；取值
  `unavailable` | `runtime_gone` | `unrecoverable` | `null`。显式证据才
  SPAWN_SUCCESSOR，与 `original_coder_available=true` 同现报错。
- `terminal`：仅允许 `role=advisor`；取值 `proceed` | `change` | `stop` |
  `blocked` | `cancelled` | `runtime_failure` | `null`。
- `runtime_state`：`known` | `unknown` | `stale` | `null`；适用于
  coder、reviewer、advisor 三角色，`unknown` / `stale` 时在角色分派前统一
  fail-closed（见“Runtime 锁定边界”）。
- `is_oldest_parked`、`task_shipped`、`has_pending_dependent_fix`：仅允许
  `role=coder` 且 `event=slot_pressure`。
- `wait_observation_count`：仅允许 `role=advisor` 且
  `event=wait_observation_timeout`。
- `context`：唯一描述性顶层字段，取值必须为封闭 JSON object，可为空。
  只允许 `target`、`response`、`blocker`、`context_request`、
  `runtime_failure`、`workspace_mutation`；未知、控制或拼写错误字段拒绝。
  `target` 为 nonblank string；`response` / `blocker` / `context_request`
  为 nonblank string 或 `null`；`runtime_failure` / `workspace_mutation`
  为 bool 或 `null`。SapWorkOrderService fixture 的描述性事实均放入该对象。

类型严格校验：bool 字段只接受 `true`/`false`/`null`；`wait_observation_count`
只接受整数或 `null`；`role`/`status`/`event`/`terminal`/`runtime_state`/
`unavailability` 只接受合法字符串或允许的 `null`。任何类型或取值非法 →
退出码 2，输出 `{"action": null, "error": "..."}`；缺少必填 `role`/`status`
同样退出码 2。CLI 顶层字段使用严格 allowlist；`runtime_stte` 等未知或拼写错误
字段直接按 invalid input 返回退出码 2。

示例：

```text
echo '{"role":"advisor","status":"RUNNING","event":"wait_observation_timeout","wait_observation_count":12}' | python skills/coding-review-pipeline/scripts/agent_lifecycle.py
{"action": "WAIT"}
```

## SapWorkOrderService 真实回归

fixture：advisor 正在检查 SapWorkOrderService 本地查询分支与异常传播测试边界，
描述性事实全部位于 `context` object；
尚无回复、无 blocker、无 context request、无 runtime failure、无 workspace
mutation，wait 观察返回无终端结果。唯一允许 `KEEP` / `WAIT`，确定性输出
`WAIT`，并断言不 close、不 cancel、不 spawn advisor2、不 re-split task、
不改 plan。

## 等待时长程序化决定（Wait Strategy）

主会话派发后的单次长等待时长不由人工判断，由 `scripts/wait_strategy.py` 按任务数、
涉及文件数与风险等级（NORMAL/ELEVATED/HIGH）计算，输出 wait_agent_ms 与执行模式；
下限 600000 ms、上限 1800000 ms。测试套件执行模式同样由该脚本按测试文件数决定：
小套件 foreground_wait（前台运行直接看输出）、中套件 background_file（后台执行读结果文件）、
大套件 background_poll（后台长轮询）。禁止为探测子代理状态而缩短等待或增加轮询；
子代理状态以 phase-report（scripts/task_report.py read）为准。
