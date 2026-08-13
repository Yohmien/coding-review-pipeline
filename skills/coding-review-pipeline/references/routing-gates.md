# Routing Gates（G1-G5）

本文件是五个正交 Gate 的可读契约，替代旧的「复杂计划总开关」。程序路由以
`scripts/route_context.py` 输出为准；本文件不增加程序之外的新触发条件，也不允许把多个
Gate 的职责合并成一个总判断。

## 使用方式

`route_context.py` 输入：`stage`、`change facts`（`change_facts.py` 输出）、`task facts`、
`ledger state`。输出：`references`、`skills`、`tools`、`reasons`。输出里的 `reasons`
逐项给出 G1-G5 的结果与依据；未命中的 reference 不加载。

## G1 User Decision Gate

唯一问题：

> 是否存在仓库事实无法确定，并且会改变实现结果、范围、接口或验收标准的用户决定？

输出：`NONE` | `REQUIRES_USER_DECISION`。

- 只有 `REQUIRES_USER_DECISION` 才路由 `grill-with-docs`（tools 附带
  `request_user_input`）。
- 以下任何一项单独出现或同时出现，都永远不能触发 G1：

  ```text
  多文件
  多模块
  CodeGraph
  rg
  工具调用多
  Skill 加载多
  测试多
  文件阅读多
  ```

- `UNKNOWN` 文件分类本身不触发 G1；未知扩展名只影响分类，不影响用户决定。
- 这些被拒绝的非触发项会以 `reasons.g1_non_triggers` 原样回显，证明程序没有把它们当作
  触发条件。
- 文件数、模块数不进入 G1，也不进入 G2；它们只可作为 G3（Task Right-Sizing）的输入。

## G2 Risk Gate

输出：`NORMAL` | `ELEVATED` | `HIGH`。

程序先产生风险候选，最终高影响语义仍由主 Agent 确认。高风险候选包括：

```text
public API
migration
schema
persistent data
transaction
concurrency
authentication
authorization
security boundary
external side effect
compatibility contract
```

候选状态使用 `confirmed` | `candidate` | `not_detected` | `unknown` 四态，不掩盖不确定性。

- `candidate` 只能把风险升到 `ELEVATED`，并给出 `advisor_candidate` 信号，不能直接决定
  `HIGH`。
- 只有 `confirmed` 事实（或 task facts 中主 Agent 已确认的 `risk`）才决定 `HIGH`。

Risk Gate 决定：

```text
advisor?
verification tier?
review tier?
```

Risk Gate 不决定：

```text
grill?
task count?
parallelism?
```

## G3 Decomposition Gate

只负责 Task Right-Sizing（`single` | `multiple`），不负责模型选择，不参与 G1，也不参与 G2。

## G4 Execution Gate

只回答：

```text
single
serial
parallel-safe
```

禁止自由策略。写集相交或有依赖链时只能 `serial`；多写集互不相交且无依赖时才允许
`parallel-safe`。`parallel-safe` 只在至少两个完整、有效、两两无交集的显式 write sets 且无
dependency 时输出；write sets 缺失、为空或状态未知时一律 `serial`，结构非法时
`invalid_input`。

`change_facts` 的 `write_set_overlap` 只对显式 task write sets 做 `set(A) & set(B)`；没有显式
write sets 时输出 `unknown`，禁止用 same_directory / same_module 等猜测冒充集合交集。

## G5 Recovery Gate

只有存在以下任一状态才加载恢复 reference（`recovery-and-failures.md`）：

```text
incomplete Ledger
running agent
dirty baseline
interrupted run
context recovery
unknown mutation
```

正常任务不加载恢复 reference。

## 正交性

每个 Gate 只回答自己的问题，任一 Gate 的输出不得用作另一 Gate 的触发条件：

```text
G1  是否有必须由用户决定的仓库不可判定问题
G2  风险等级（只决定 advisor / verification tier / review tier）
G3  任务数量（Task Right-Sizing）
G4  执行模式（single / serial / parallel-safe）
G5  是否需要恢复
```

文件数量、模块数量、搜索方式、工具数、Skill 数、测试数与阅读量只作为上下文：不进入 G1、
G2 的判断；文件数/模块数可作为 G3 right-sizing 输入。真正需要人决定的只有 G1 一类问题。
