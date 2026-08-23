---
title: CRP v3 Token 优化完整计划
date: 2026-08-23
baseline: main@803efde（518 tests OK）
goal: 中等复杂任务编排层 token 消耗降低 ≥30%
---

# CRP v3 Token 优化完整计划

## 1. 背景与实证依据

本计划基于对真实会话 01a0235b-b8e7-7662-bb8e-b69fb033de3b 的全量扫描得出。
该会话 25 轮、约 5.6 小时、131 次文件变更、可见推理文本合计 40.8 万字符，
是 V2 状态机在中等偏上复杂度 Java 改造上的完整运行样本。

### 1.1 消耗结构画像

按轮次推理字符数排序的前六大消耗轮：

| 轮次 | 推理字符 | 文件变更 | 内容 |
|---|---|---|---|
| 01a02376 | 112K | 39 | 大规模实施 |
| 01a0235b | 67K | 8 | 确认模型后继续实施 |
| 01a0242d | 44K | 13 | 集成复审缺陷修复 |
| 01a02489 | 38K | 19 | 冒烟测试发现缺陷后的修复 |
| 01a02481 | 34K | 4 | 手动冒烟测试全链路推演 |
| 01a0236c | 29K | 0 | "继续"轮——大量推理无产出 |

六个轮次全部是实施/修复/冒烟类，无一来自 packet 生成或 preflight。
结论：V2 的编排脚本开销不是主要矛盾；实施轮内部的重复推导和约束漂移才是。

### 1.2 会话暴露的三个结构性漏点

1. **上下文压缩后的重新推导**：多个"继续"轮出现 67K–112K 字符的巨型推理，内容是在重走已覆盖的分析。skill 目前只要求把"决策"写进 ledger，不要求把"中间分析"落盘。
2. **计划约束与实现断言脱节**：集成复审红灯的两项（T7 认领事务外调用设备、T8 同源统计）都是"计划写了、packet 没接住、代码没实现"，直到全部代码写完、测试全绿之后才被 reviewer 发现——这一来一回至少烧掉实施 + 复审 + 返工三轮。
3. **冒烟场景交互式执行而非自动化**：用户手动发起 34K 推理的全链路推演，发现问题后又花 38K 推理修复。V2 已强制询问场景并写入 packet 验证段，但没有强制它落地为可执行测试工件。

## 2. 优化项清单（按优先级排序）

### P1：约束→断言机械校验（阶段 4 拦截）

**问题**：计划中的 MUST 约束没有机械手段确保映射到 packet 验证段的具体断言。漂移要到集成复审才被发现，返工成本最高。

**方案**：

- 从计划提取每条 MUST/关键约束，要求在 packet 的 VERIFICATION 段映射为具体测试名或可判定检查项。
- validate_task_packet.py 新增 --require-constraint-mapping 校验：计划声明了 N 条 MUST 约束 → packet 必须包含 N 条对应验证条目，缺一条即 BLOCKED。
- ledger 新增 plan 级字段 constraints（含 id、text、mapped_tests），completion_gate 在收尾时复核 mapped_tests 是否全绿。

**预期收益**：消除 T7/T8 这类漂移导致的完整返工循环（该会话中至少三轮、约 15 万推理字符）。

**涉及文件**：validate_task_packet.py、run_ledger.py（schema）、completion_gate.py、task-contracts.md、SKILL.md 第 4 节。

### P2：分析笔记落盘与增量恢复

**问题**：上下文压缩后重新推导已确认语义（seed 幂等性、upsert 重置风险等），同一结论在同会话内被反复推导 2–3 次。

**方案**：

- canonical ledger 新增 analysis_notes 区：每个阶段转换时主会话把已确认符号、已判定语义、未决问题以紧凑结构追加写入（≤500 token/次）。
- SKILL.md 第 1 节新增规则：单轮推理超过阈值（建议 30K 字符）仍未产出 diff 或笔记时，先把当前分析检查点到 analysis_notes 再继续，防止蒸发。
- 中断恢复时从最近笔记续接，不重做已确认判断。

**预期收益**：直接消解 112K 巨型推理轮的重复部分，估算节省 20%–35% 实施轮消耗。

**涉及文件**：run_ledger.py、SKILL.md 第 1 节与恢复节。

### P3：冒烟场景自动化落地

**问题**：场景条件写入 packet 后仍以人工推演方式执行，一次手动冒烟烧掉 34K+38K 推理。

**方案**：

- SKILL.md 第 4 节强化：场景验证条件必须编码为自动化测试进入交付物；"人工执行"只能作为补充证据，不作为唯一通过标准。
- verification-routing.md 场景验证节同步：scenario_checks 每条必须指向可执行的测试名或脚本命令。
- validate_task_packet.py 校验 scenario_checks 每条都有 executable 字段。

**预期收益**：68K 交互式冒烟循环压缩为一次 GREEN 运行。

**涉及文件**：verification-routing.md、validate_task_packet.py、completion_gate.py（已有基础）、test-prompts.json。

### P4：复验 delta 化

**问题**：fix-first 循环中 coder 只改 2 个文件但主会话重跑全部验证命令 + 全量 diff 检查。

**方案**：

- 首轮全量；后续轮只跑受影响文件的定向命令 + git diff --check。
- 前提：ledger 记录上一轮已通过的命令指纹；final integration review 前最后一次全量兜底。

**预期收益**：每轮修正省 3–5K token，fix 循环越多乘法效应越大。

**涉及文件**：verification-routing.md、run_ledger.py。

### P5：review_preflight 输出落盘 + 指针传递

**问题**：preflight 把 P0-P3 review context、归一化 finding、negative coverage 全部打进 stdout，主会话读完再转给 reviewer——同一份数据被消费两次。

**方案**：

- 脚本把完整包写到 canonical ledger 目录，stdout 只输出 ≤200 token 索引（finding 总数、P0/P1 数、FOCUS ON 关键词、包路径）。
- reviewer 收到路径后按需读取自己关注的 section。

**预期收益**：每次审查省 5–8K token。

**涉及文件**：review_preflight.py、review-routing.md、task-contracts.md。

### P6：Packet 共享前言抽离

**问题**：多 coder 场景下 AGENTS.md 摘要、项目约定、通用 BLOCKED 条件、RED/GREEN 判定标准在每个 packet 里重复内联。

**方案**：

- 这些内容写一次到 ledger 目录 shared-preamble.md，packet 只引用路径 + 任务特有五段。

**预期收益**：多任务时省 2–4K。

**涉及文件**：task-contracts.md、validate_task_packet.py（允许 preamble 引用格式）。

### P7：合并路由脚本为单次调用

**方案**：新增 route_all.py 入口串联 change_facts → route_context → task_graph，输出一份合并后紧凑 JSON，去重共享字段。每次调度循环少解析两份冗余输出。

### P8：change_facts 缓存指纹

**方案**：同 workspace 短时间内多次进入 pipeline 时若 HEAD + dirty baseline 指纹未变，直接从 ledger 读缓存结果跳过重新收集。条件：距上次 <10 分钟且无新 commit/stash。fix 循环回探索阶段时省 1–2K/次。

### P9：模型探针合并到首次派发

**方案**：探针语义合并到首个正式任务 spawn prompt（首行要求先回复 OK 再执行），失败走降级。省一整轮独立探针调度成本（会话中观察到四个近空转轮次与此相关）。

### P10：Reviewer 作用域分层

**方案**：单边界任务 reviewer 只看该 task 的 diff + 触及接口签名 + preflight 索引；integration reviewer 看跨模块契约 + 各 task verdict 摘要 + 最终全量 diff 统计概览。审查阶段省 3–6K/次。

### P11：调度循环增量更新

**方案**：ledger 维护图状态，agent 终态事件到达时只重算该节点后继闭包并输出 delta。多任务场景每次调度省 1–2K。

### P12：SKILL.md 文本瘦身（次要）

**方案**：将第 3 节 Live 模型确认的 UI 覆盖率细则下沉到 references/model-selection.md；调度循环细节下沉到 agent-lifecycle.md。目标主文件 ≤22KB。收益绝对值小且 Darwin 对结构变更有回归成本，放最后单独一轮处理，避免归因困难。

## 3. 实施顺序与验证策略

建议分四批，每批完成后跑完整回归（当前基线 518 tests OK）+ 双 skill validate + 一组真实场景对比 token 消耗：

| 批次 | 内容 | 预估工作量 | 回归重点 |
|---|---|---|---|
| 第一批 | P1 + P3（packet/gate 层拦截） | 中 | validate_task_packet/completion_gate 测试模块 + 新增用例 |
| 第二批 | P2（ledger 笔记区 + SKILL.md 规则） | 中 | run_ledger 测试 + 恢复场景测试 |
| 第三批 | P4 + P5 + P6（复验/review 层效率） | 小-中 | review_preflight 测试 + verification-routing 口径一致性 |
| 第四批 | P7-P11 + P12（路由合并、缓存、探针合并、分层审查、增量图、文本瘦身） | 小 | route/task_graph 测试 + Darwin 结构回归 |

每批完成后的对比验证方法：

1. 选一个已完成的中等任务（如 VNM20 JIT 改造），按新 skill 重放编排决策点（不实际改代码，只记录每阶段输入输出 token）。
2. 对比旧路径与新路径的编排层 token 总量。
3. 达标（≥30% 降低）则合入下一批；不达标逐项归因调整。

## 4. 不做的事

- 不改变状态机阶段顺序和 G1-G5 正交性——这些是 V2 的核心资产。
- 不引入新的外部依赖。
- 不为极端 corner case 加防御逻辑（遵循全局 SCOPE LIMITS）。
- P12 文本瘦身不与其他改动混批，避免 Darwin 归因污染。

## 5. 成功标准

1. 全部批次合入后，完整回归 ≥518 tests 通过且双 skill validate 通过。
2. 重放对比显示中等任务编排层 token 降低 ≥30%。
3. 真实新任务中不再出现"计划 MUST 约束在集成复审才红灯"的情况（P1 生效判据）。
4. 上下文压缩恢复后不再重做已确认语义判断（P2 生效判据）。
5. 场景验证以自动化测试交付，人工推演仅作补充（P3 生效判据）。
