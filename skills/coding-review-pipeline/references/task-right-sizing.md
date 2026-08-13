# Task Right-Sizing

本参考定义 Task 的定义、固定拆分顺序与程序/Agent 决策边界，并配套
`scripts/task_graph.py` 计算调度事实。

## Task 定义

> 一个拥有独立验证闭环，并值得 fresh reviewer 独立做 ship / fix-first / rethink 判断的最小交付单元。

## 拆分算法（固定顺序）

必须按以下顺序执行，禁止先按文件拆。

### 1. Deliverable

先写用户/系统可以观察到的结果。禁止先按文件拆。

### 2. Review Independence

问：Reviewer 能否合理地 ship A、同时 fix-first B？

否：优先同 Task。

### 3. Verification Independence

A 无 B 无法验证：合并，或 A → B。不得创建不可验收的半 Task。

### 4. Cohesion

同一个以下不变量默认同 Task：

```text
transaction invariant
idempotency invariant
security boundary
compatibility promise
state transition
compensation flow
```

典型 fixture：Controller + Service + Mapper 共同实现单一 idempotency
invariant 时不要按层拆成三个 Task。

### 5. Ownership

程序计算 write-set intersection：有交集即 `parallel=false`。

典型 fixture：

- 两个独立功能，disjoint writes、independent verification → `parallel-safe`。
- 同文件两个功能 → `parallel=false`。

## 程序决定 CAN，Agent 决定 SHOULD

```text
Program:  CAN_SPLIT | CAN_PARALLEL | HAS_OVERLAP | HAS_DEPENDENCY
Main:     SHOULD_SPLIT | BUSINESS_COHESION
```

程序不硬判断业务 Deliverable 与 cohesion；`CAN_SPLIT` 只在任务 schema
完整且无环时为 true，是否真正拆分由主 Agent 依据业务 cohesion 决定。

## Task Schema（固定）

每个 Task 必须包含：

```text
TASK_ID
DELIVERABLE
WHY_ONE_TASK
INDEPENDENT_ACCEPTANCE
WRITE_SET
READ_ONLY
PREDECESSORS
SUCCESSORS
VERIFICATION_UNIT
PARALLELISM
```

`PREDECESSORS` 与 `SUCCESSORS` 必须互相一致；`PARALLELISM` 是主 Agent
声明的 SHOULD 字段，程序另行计算 `CAN_PARALLEL`。

## 程序输出

`task_graph.py` 输出：

```text
has_cycle / cycles
topological_order
transitive_ancestors
ready queue
blocked / blocked_successors
dependencies
write_set_overlaps
parallel_safe
can_split
```

语义约束：

- 有环时 `status: BLOCKED` 且退出码非零，`ready` 与 `parallel_safe`
  清空；cycle 由确定性 SCC（覆盖共享节点、多 SCC、自环）给出，输出
  稳定排序。
- `parallel_safe` 仅对无环图中 unfinished + ready 的任务对：无直接或
  传递依赖（由 `transitive_ancestors` 判定）且 write set 无交集。
- `completed` 必须 predecessor-closed：已完成任务的全部祖先也必须完成，
  否则 invalid_input。
- 空 task list invalid_input；单 Task 图正常但 `can_split=false`（没有
  可拆分 pair）。
- `WRITE_SET`/`READ_ONLY` 只接受 repo-relative 安全路径：拒绝 absolute
  与越界 `..`，规范化 `.` 与分隔符，按 Windows case-insensitive 语义
  比较；拒绝规范化重复与 WRITE_SET/READ_ONLY 冲突。

Task 定义完成后，主 Agent 不再反复推理哪个 Task 可以开始，直接消费该输出。
