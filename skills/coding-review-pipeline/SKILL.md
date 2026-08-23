---
name: coding-review-pipeline
description: 项目源码与自动化测试变更的架构、编码、复审和验证流水线。Use when implementing, fixing, refactoring, or testing project code，或用户要求“实现功能”“修复缺陷”“重构代码”“补回归测试”；“审查并修复”仅在已明确授权修复的编码阶段使用。Do not use for explanation, repository search, command execution, documentation or configuration-only edits, or read-only review.
---

# Coding-Review Pipeline

本 skill 只编排项目代码变更。主会话负责需求、架构、契约、风险决策、任务拆分、实际 diff、独立复验和最终验收；coding 子代理只在已定案边界内实施。子代理报告只是 claims，不能替代工作树和命令证据。

## 触发边界

触发：

- 新增或修改项目源码、脚本、组件、模块或自动化测试。
- 修复缺陷、实现功能、重构代码、补回归测试。
- “审查并修复”中已获用户明确修复授权的编码阶段。

不触发：

- 解释、检索、只读探索、运行命令或测试。
- 仅修改文档、README、数据文件或纯配置。
- 只读 review、diff 审阅、风险检查。

只读 review 只返回发现，不进入计划确认、模型选择或 coding。用户同时说“审查并修复”时，先报告审查发现；未获明确修复授权前不得编码。

## 不可变原则

1. 除下述 `DIRECT_PATCH` / `SIMPLE_PATCH` 外，主会话不得直接编写项目源码；修复规格后重新委派，不静默代写。
2. 计划和模型选择都由主会话与用户确认，子代理不得重复前置审批。
3. 不从历史记忆猜测模型、effort 或工具能力；每次编码任务读取当前 live schema。
4. 不静默替换模型、降低 effort、跳过 reviewer 或把部分验证说成全部通过。
5. `task_graph.py` 判定为 ready、parallel-safe 且写集两两不重叠的任务必须由主会话主动并发派发；只局部串行依赖链和冲突分量。
6. reviewer 行为只读，不实现修复；任何修复都使旧 verdict 失效。

## 执行经济性预路由

进入完整状态机前先判定以下短路；命中即跳过与结果无关的计划、模型、packet、代理和恢复动作：

- `DIRECT_PATCH`：用户明确要求主会话快速修改；目标、紧耦合源码/测试写集和验收均已明确；且不涉及公共 API、schema、持久化数据、事务、并发、权限、依赖或外部副作用。主会话保护工作树后直接完成最小修改、行为 RED、GREEN、`git diff --check` 和定向验证，不要求模型选择，不派 coder/reviewer。
- `SIMPLE_PATCH`（复杂度门控，无需用户点名）：满足全部条件时自动进入主对话自修分支——①单文件或紧耦合双文件写集；②纯应用层逻辑，无公共 API/schema/事务/并发/迁移/依赖变化；③修改意图一句话可说清且验收标准明确（已有测试或一行命令可验）；④预估 diff ≤30 行。主对话按 ponytail 阶梯完成最小修改 + 定向验证 + 标准提交；复用判断（ladder 复用层级）由主对话在 READ 时完成并记录到提交信息。任一条件不满足 → 回完整状态机。
- `CONFIRMED_CONTINUATION`：完整计划、当前任务 packet 和四项模型选择已经确认，前驱已 ship。只核对最新检查点、写集无漂移且既有选择仍受 live schema 支持，然后直接派发当前任务；不得重做探索、计划、模型询问或无关搜索。
- `KNOWN_DIRTY_BASELINE`：已有修改已归属用户、与当前写集不重叠，且没有中断 run、运行中代理或来源不明变化。只记录 changed-file baseline；不得仅因此进入 G5 recovery、重做历史审计或创建平行事实制品。

任一条件无法由当前证据证明时不猜测，回到完整状态机。
- `SUBAGENT_UNAVAILABLE_FALLBACK`：coding/review 子代理因外部原因（provider 额度耗尽、provider 运行时故障、账号限制）全部不可用时，经用户明确选择"主会话直改直审"后启用。主会话承担 coder + reviewer 双角色，但仍执行完整 RED→GREEN、独立复验、diff check、台账记录和 completion_gate；不得因子代理不可用而跳过任何验证步骤或降低验收标准。

## 首次响应硬门禁

对用户的首次响应只能承诺当前获准阶段，并满足以下可观察条件：

1. 未命中执行经济性短路，且代码变更请求尚未完成探索与完整计划确认时，只说明将先读取规约、检查工作区、定位证据；G1（见 [references/routing-gates.md](references/routing-gates.md)）命中还须先完成追问，再提交计划。不得推荐或列出任何模型 ID、reasoning_effort，不得请求模型选择，也不得承诺开始编码。
2. “只读 review”“先 review”或“顺便修一下”等未明确修复项和写入授权的请求，只承诺并执行只读 review；不得预告后续自动修复、模型选择或 coding。报告发现后，等待用户明确选择修复项并授权。
3. 用户要求复用历史模型或允许自动换模时，先读取当前 live schema。原选择任一项不受支持即 🔴 CHECKPOINT · 🛑 STOP：列出缺失能力并请用户重选；不得接受“自动换一个”的授权作为静默替换依据。

首次响应不满足任一条件时，不进入后续状态机，先重写响应直至满足。

## 按需读取

Gate 与路由以 `scripts/route_context.py` 的 G1-G5 输出为准（可读契约见 [references/routing-gates.md](references/routing-gates.md)）；只加载输出命中的 reference 与 skill，不复制其规则。

- 在形成任何 coder、advisor 或 reviewer 提示前，读取 [references/task-contracts.md](references/task-contracts.md)。
- verify/complete 阶段或 completion_claim 命中时，读取 [references/verification-routing.md](references/verification-routing.md)；进入阶段 3 或处理模型选择时，读取 [references/model-selection.md](references/model-selection.md)。
- G5 输出 `required`（incomplete ledger、running agent、dirty baseline、interrupted run、context recovery、unknown mutation 任一成立）时，读取 [references/recovery-and-failures.md](references/recovery-and-failures.md)。
- 定位结构、调用关系、数据流或影响面时调用 search-gates；CodeGraph 图谱层缺失时由 search-gates 自身降级 rg 锁定，不在本 skill 复制搜索细则。
- G1 输出 `REQUIRES_USER_DECISION` 时调用 grill-with-docs（传递 grilling / domain-modeling），追问口径见第 1.5 节；G1 输出 `NONE` 时跳过。
- 编码和 review 应用 ponytail；review 子代理第一步必须运行 `scripts/review_preflight.py`，消费口径见第 7 节。
- 缺陷修复调用 systematic-debugging；可测实现调用 test-driven-development；完成声明前调用 verification-before-completion。只读取适用 skill，不复制其规则。

## 依赖与前置

本 skill 编排其他 skill，不复制其细则。依赖按 CORE / CONDITIONAL / OPTIONAL 三档处理：CORE 缺失立即报告并停止；CONDITIONAL 在 Gate/阶段命中条件时缺失，报告并停止对应流程；OPTIONAL 缺失不阻断。任何档位都不静默降级、不找替代品硬顶。

### CORE（任何编码任务必需）

| skill / 组件 | 来源 | 说明 |
|---|---|---|
| `search-gates` | 随本仓库开源（`vendor/skills/search-gates`） | 结构、调用、数据流或影响面定位；图谱层缺失时自身降级 rg 锁定 |
| `verification-before-completion` | obra/superpowers（或等价 curated 来源） | 任何完成、通过、修复声明前 |
| `ponytail` | DietrichGebert/ponytail | 编码与 review 阶段的最简可行解纪律 |
| 内置引用（非 skill） | 随本 skill 分发 | `references/` 全部文件 + `scripts/` 全部脚本；任一缺失按 recovery-and-failures.md 失败路径处理，不继续派发 |
| `contract-executor`（兄弟 skill） | 随本仓库分发（`skills/contract-executor`） | coding 子代理的机械执行状态机；必须与本 skill 同步安装，缺失时 fail closed，不派 coding 子代理 |

### CONDITIONAL（按 Gate/阶段命中加载）

| skill / 引用 | 来源 | 命中条件 |
|---|---|---|
| `grill-with-docs`（传递 `grilling`、`domain-modeling`） | mattpocock/skills（`skills/engineering/grill-with-docs`） | 仅 G1 输出 `REQUIRES_USER_DECISION`；传递依赖任一缺失使追问门禁失效，按 CORE 缺失处理 |
| `systematic-debugging` | obra/superpowers（或等价 curated 来源） | 缺陷修复且 root cause 未建立 |
| `test-driven-development` | obra/superpowers（或等价 curated 来源） | 可测实现 |
| `references/recovery-and-failures.md` | 内置引用（文件本身属 CORE 分发清单） | 仅 G5 输出 `required` 时加载 |

### OPTIONAL（增强，可缺失）

 - CodeGraph 图谱索引：search-gates 的图谱层。缺失时 search-gates 按自身兜底表降级 rg 锁定（显式路径）或报告缺失，不假装命中。

### 安装与验证

- 安装到 `$CODEX_HOME/skills`（默认 `~/.codex/skills`），目录名必须等于 frontmatter `name`。
- 各依赖的权威来源与安装命令见开源仓库 README 的三分类依赖表；本机可用 `skill-installer` 按 GitHub 目录 URL 安装。
- 验证：目录存在、frontmatter `name` 与目录名一致、`references/` 全部文件与 `scripts/` 全部脚本完整、兄弟 skill `contract-executor` 已同步安装（缺失 fail closed）、`python scripts/validate.py skills/coding-review-pipeline skills/contract-executor` 全部输出 `Skill is valid!`。
   另须预检 canonical ledger 目录（`$CODEX_HOME/state/coding-review-pipeline/<workspace-id>/runs/`）的写权限：不可写时在进入状态机前提示修复，不得跑到中途才发现台账写入失败。

## 核心状态机

主流程：执行经济性预路由 → 1 探索与定案 → 1.5 G1 追问门禁 → 2 完整计划确认 → 3 Live 模型确认 → 4 生成任务契约 → 5 风险路由与实施 → 6 主会话独立复验 → 7 Fresh review 与修正循环 → 完成条件（completion_gate 放行）。

### 1. 探索与定案

1. 用 `scripts/change_facts.py` 收集 change facts（同 run 多次进入时用 `--cache-file/--cache-ttl` 复用，指纹未变直接命中），并检查 AGENTS.md、git status --short 和用户已有改动；多任务需要 facts+路由+任务图三份输出时改用 `scripts/route_all.py` 一次调用取合并 JSON。
2. 按 search-gates 获取足够上下文；目标在配置或测试文件时再精确读取源码。READ 时由主对话完成 ponytail 阶梯复用判断（本库已有实现 / stdlib / 原生特性 / 已有依赖），结论以权威锚点写入 packet 的 DECIDED 清单——coder 执行既定复用，不自行做复用级判断。
3. 缺陷先复现并形成根因证据，禁止猜测式修改。
4. 主会话只定案可由仓库事实、已确认用户决定或既有权威契约唯一确定的接口与边界；出现互斥高影响方案时进入 G1，不得自行选择。
5. 判定风险等级与任务依赖，列出允许修改的文件集合。
6. 改动涉及用户可感知的业务流程变化时，在形成完整计划前必须主动向用户询问冒烟测试场景（端到端业务链路），并逐项确认：每步的触发条件、输入数据、预期结果、通过标准、边界情况（部分失败/重试/并发）；不得凭代码阅读自行推断链路语义后直接写入计划，也不得等用户提供——必须主动问。确认结论作为场景验证条件的权威来源写入计划。

输出：change facts、根因证据、已定案接口与边界、允许修改的文件集合。

分析笔记纪律（全阶段适用）：每个阶段转换时，主会话把已确认符号、已判定语义和未决问题以紧凑条目追加写入 canonical ledger 的 analysis_notes（每条 ≤500 token）。单轮推理超过约 30K 字符仍未产出 diff 或笔记时，先把当前分析检查点到 analysis_notes 再继续。中断恢复时从最近笔记续接，不重做已确认判断。

### 1.5 G1 追问门禁（one-hop）

是否追问由 `scripts/route_context.py` 的 G1 User Decision Gate 决定，可读契约见 [references/routing-gates.md](references/routing-gates.md)：只有输出 `REQUIRES_USER_DECISION` 才路由 grill-with-docs（tools 附带 request_user_input）；多文件、多模块、CodeGraph、rg、工具/测试/阅读数量等一律不是 G1 触发条件。命中后按 grill-with-docs（及传递的 grilling / domain-modeling）执行追问，细节不复制到本 skill；G1 输出 `NONE` 时跳过，直接根据探索证据形成完整计划。

advisor 返回 `change` 后必须重新检查 G1：只要它新增或改变公共 API、schema、事务、并发、外部副作用、兼容性、部署切换或验收口径的互斥选择，就设置 `user_decision_required=true` 并重新运行路由，强制进入 grill-with-docs；主会话不得挑选偏好后继续 advisor。仅把已确认决定机械写回文档、修正错字或统一不改变语义的措辞时，才保持 G1=`NONE`。

输出：G1 判定与已确认的用户决策（未命中则直接进入完整计划）。

### 2. 完整计划确认

计划必须建立在已确认的追问结论上（简单任务则建立在探索证据上），并至少包含：

- 目标、现状和影响范围。
- 拟修改文件、任务拆分、依赖与并行边界。
- 已定案的接口、数据和错误处理策略。
- 追问阶段已更新的 glossary / ADR（如有）及其与实施任务的关系。
- 测试与验证命令、风险、回滚或补偿。

🔴 CHECKPOINT · 🛑 STOP：除 `DIRECT_PATCH`、`SIMPLE_PATCH` 和 `CONFIRMED_CONTINUATION` 外，用户未确认完整计划前，不展示模型选择，不派 coding 子代理，不修改项目代码。

输出：已确认的完整计划。

### 3. Live 能力与模型确认

计划确认后：立即读取 spawn_agent schema 并核实四项可选（读不到 live schema 时触发下方 STOP）；紧凑推荐 coding/review model 与 effort 四项请用户确认；确认后写入 canonical ledger（model_selection 事件）。展示覆盖率、结构化输入收口、复用条件、schema 快照与探针合并的完整口径见 [references/model-selection.md](references/model-selection.md)（进入本阶段时加载）。

🔴 CHECKPOINT · 🛑 STOP：四项未确认，或 live 工具不支持任一选择时，停止编码并报告缺失能力；不得使用静态候选、静默降级或主会话代写。

输出：已确认的 coding model、review model、coding effort、review effort 四项。

### 4. 生成任务契约

1. 按 task-contracts.md 生成五段 coder packet：目标、文件所有权、接口、约束、验证。
    以探索阶段（第 1 节第 6 条）向用户询问并确认的冒烟测试场景为唯一权威来源，写入 packet 验证段；格式、判定和 reviewer 审查口径见 [references/verification-routing.md](references/verification-routing.md) 场景验证节。自动化测试全绿但场景未全部执行 = 验证不完整。每条场景检查必须携带 executable 字段（指向可执行测试名或命令），人工推演只能作为补充证据。
    计划声明 MUST 约束时，packet 必须通过 CONSTRAINT_MAPPINGS 把每条约束映射到具体验证条目；validate_task_packet.py --plan-constraints 校验覆盖完整性，缺一条即 BLOCKED。高风险任务（并发、事务、迁移、外部副作用）的计划必须建立约束注册表。
2. 派发前用 `scripts/validate_task_packet.py` 校验 packet；输出 BLOCKED 时按 evidence 修正，不得绕过派发。
3. 规格定案所有影响接口、契约、安全和范围的判断；允许 coder 处理局部低风险实现判断，但必须在返回中报告。
4. 每个任务声明可写集和必要只读依赖；禁止转发主会话完整对话或其他子代理 raw 对话。
5. 单文件单点任务保持单任务，不为并行而过度拆分；拆分与并行可行性以 task_graph.py 的 CAN 输出为准。
6. 可测任务的 RED 必须实际执行测试且因目标行为断言失败；`testCompile`、`cannot find symbol`、测试收集、语法、依赖或环境失败均为 `INVALID_RED`。先修复测试夹具或建立最小可编译壳，直到 `tests_run > 0` 且失败签名符合预期，才允许进入 GREEN。
7. run 状态只写 canonical ledger；不得另建 `ledger-state`、`task-facts`、`verification-*` 或 `completed-*` 平行事实副本。任务包单一事实源：packet 与验证记录以 ledger 目录为唯一权威落点，不为可视化或展示目的再生成镜像副本；平台自动生成的投影文件不主动维护或更新。

输出：通过 `validate_task_packet.py` 校验的 coder packet。

### 5. 风险路由与实施

风险等级取 route_context.py 的 G2 输出（NORMAL / ELEVATED / HIGH），并行模式取 G4 输出（single / serial / parallel-safe）；生命周期动作只允许 agent-lifecycle.md 的固定 8 actions，收敛路由只允许 task-convergence.md 的固定 7 routes。

| 等级 | 条件 | 流程 |
|---|---|---|
| 普通单点 | 边界明确、单一责任、影响局部 | coder → 主会话检查 diff/复验 → fresh final reviewer |
| 多任务/跨模块 | 多个互不重叠文件集或明确依赖链 | 按下述调度循环持续填满可用 slot；存在跨任务接口或数据契约依赖时，主会话先核对前驱实际 diff 与既定契约，高风险承诺再交 fresh commitment-boundary advisor，随后解锁下游 coder；全部收口后 fresh integration reviewer |
| 高风险 | 并发、事务、安全、迁移、公共 API、外部副作用、宽影响重构 | 编码前 fresh commitment-boundary advisor；计划修正后再实施；最后 fresh integration reviewer |

多任务并发调度的五步循环、slot 优先序与 advisor 增量收敛协议（converged_dimensions、第 3 轮聚焦模式）见 [references/agent-lifecycle.md](references/agent-lifecycle.md)，进入阶段 5 或启动承诺边界审查时加载。多任务只对公共契约、状态机、SQL、事务或外部副作用边界做独立 task review；机械叶子任务由主会话复验，最终 integration reviewer 统一收口。coder 只修改授权文件，按 packet 验证并返回实际证据。

输出：按 G2/G4 路由的实施计划与派发指令。

### 6. 主会话独立复验

每轮 coder 返回后，主会话必须：

1. coder 终态返回后先读取子代理阶段报告（scripts/task_report.py read --run-id <id> --task-id <tid>）确认其自述状态：仅当报告为 completed 才展开 git status --short、完整 diff、未跟踪文件和允许范围的复验；报告为 blocked/in_progress 时按 gaps/summary 处理，不展开 diff。进入复验后报告与 diff 不一致时以 diff 为准并按范围外变化处理。不得在读取报告前用文件变更扫描判断子代理进度。
   存在未跟踪新文件时，先对每个新文件执行 `git add -N <file>`（intent-to-add，仅让 diff 可见）
   再运行 `git diff --check`；不得因此顺手 `git add` 提交。
2. 发现范围外变化立即停止，不静默归入任务。
3. 按 verification-routing.md 重跑适用命令并读取完整输出、退出码和失败数。fix-first 修正轮按 delta 口径：只跑受影响文件的定向命令 + git diff --check，全量验证留给 final integration review 前最后一次。
4. 将实际 diff 和主会话证据提供给 fresh reviewer；不以 coder 摘要代替。

输出：复验证据包（完整 diff、命令输出、退出码、失败数）。

### 7. Fresh review 与修正循环

reviewer 必须使用独立、上下文干净的线程，行为只读，并按 task-contracts.md 返回。派发后第一步必须运行 `scripts/review_preflight.py`（确定性前置，不调用 LLM）。


reviewer spawn prompt 构造规则（ROLE_LOCK 三要素、禁止二次派发叙事）与 preflight 输出消费口径见 [references/review-routing.md](references/review-routing.md)，派发 reviewer 时加载。

审查结论按 task-contracts.md 返回：

- ship：目标、范围和证据足以交付。
- fix-first：列出文件、位置、证据和必需修复。
- rethink：架构或契约需要主会话重新定案。

fix-first 路由回原 coder；不可恢复时用相同已确认 coding 设置派后续 coder。rethink 回到探索与计划阶段。有界重探索：fix-first 的原 coder 允许做只读重定位（读 packet 只读依赖清单内的文件、重跑定向测试定位根因），不视为范围扩大、不升级 rethink；但写集、接口契约或验收标准的任何变化仍必须走 BLOCKED 回抛主会话。任何代码变化后必须重新复验并获取 fresh verdict。

单个任务和集成 review 各最多 3 轮；同类/相同问题复发 → 立即 rethink 收窄重派（不等第3轮，把复发场景补入 DECIDED 降低 coder 决策难度）；第 3 轮仍有任何待修发现 → rethink 上限 2 轮后 grill-me 追问计划并刷新轮次为 0 重循环；escalation_total 达 9 次熔断等人工决策。禁止重置轮次绕过计数；歧义随时 grill-me，禁代码推测式实现（细则见 [references/review-routing.md](references/review-routing.md) 不收敛升级与歧义处理节）。独立任务可继续，但不得在未全部 ship 前声明整体完成。reviewer 作用域分层：task review 只看该任务 diff+接口签名+preflight 索引，integration review 看跨模块契约+verdict 摘要+diff 统计概览。

输出：fresh verdict（ship / fix-first / rethink）。

#### Provider 连续失败降级

同一 provider 的 reviewer/coder 派发连续 3 次以相同非业务错误（协议错误、额度拒绝、运行时不可达）失败时，判定该 provider 对当前角色不健康：

1. 停止重试；把三次失败的模型、effort、错误摘要写入 canonical ledger。
2. 从 live schema 中排除该 provider，生成剩余可用模型/effort 组合的紧凑替代列表。
3. 向用户报告：失败证据表（尝试×模型×结果）、根因判断、替代选项列表；等待用户选择后继续。不得静默换模型、降 effort 或放弃 fresh review。
4. 若排除后无任何可用组合，触发 `SUBAGENT_UNAVAILABLE_FALLBACK`。

## 工作树、线程与恢复

- 派发前建立 changed-file baseline，保留已有脏文件和用户修改；具体做法见 recovery-and-failures.md。
- 已归属、与写集不重叠且无中断/运行代理的用户修改属于 `KNOWN_DIRTY_BASELINE`，不单独触发 recovery。
- 只回收已完成且无需追问、修复或恢复的线程；先保存任务状态、实际文件清单、证据和 verdict。
- 恢复协议（压缩/中断后按固定顺序执行，禁止通读长上下文找回状态）：① 读 canonical ledger——最近 model_selection（schema 仍支持时展示四项请求一行确认即复用）、analysis_notes 最近条目、各任务最新 verdict 与 fingerprint；② 用 task_report.py read 确认各 active 任务最新阶段报告的 completed/blocked/in_progress 真实位置；③ 核对 git status --short 与 ledger diff_fingerprint，一致则从第一个未闭环阶段继续，不一致先做范围核对再继续。不得未检查就重复派发或覆盖改动。
- 工具、模型、线程、范围或验证失败按 recovery-and-failures.md 的 if-then 表处理，失败路径不得吞掉。

长时间异步工作只在状态转换时通知用户：`blocked/input-required`、`completed`、`errored`、`fix-first` 或 `ship`。`running` 且无新事实的等待超时不是进度事件：不得发送“仍在运行/继续等待”，不得读取行数、哈希或写集验活，不得催促代理。等待时长与测试执行模式由 `scripts/wait_strategy.py` 程序化决定，主会话不自行估算；模式阈值、公式与上下限以该脚本输出为准（细节见 agent-lifecycle.md）。终端 session 的空 `write_stdin` 轮询至少 180000 ms、优先 300000 ms；非空交互输入不应用长等待。

## 标准提交规则（固定动作）

代码发生变更时（coder 返回后主会话复验通过、或主会话 SIMPLE_PATCH/DIRECT_PATCH 完成验证），提交是固定动作而非判断项：只要本次 run 产生了项目源码/测试/SQL 变更且通过复验，必须立即创建一个标准 commit 说明修改内容，供后续回滚与会话快速回顾。提交信息格式：`fix|feat|refactor|test(范围): 一句话核心修改`，正文列出关键文件与验证命令结果。禁止询问“是否需要提交”、禁止把多个逻辑变更合并成含糊的单一提交；文档与台账类变更不触发此规则。

## 完成条件

🔴 CHECKPOINT · 🛑 STOP：`scripts/completion_gate.py` 未输出 `COMPLETE_ALLOWED` 不得声明完成、不得使用完成措辞；输出 `BLOCKED` 时按 reasons 逐项闭环。

只有同时满足以下条件才可声明完成（确定性检查以 `scripts/completion_gate.py` 输出为准，`COMPLETE_ALLOWED` 才放行）：

1. 要求独立 task review 的边界任务获得当前 diff 对应的 fresh ship；机械叶子任务完成主会话复验，整体获得当前 diff 对应的 fresh integration ship。`SIMPLE_PATCH` / `DIRECT_PATCH` 以主会话最新验证代替 reviewer verdict。
2. 主会话确认最终实际改动文件均在范围内，并标注在原有脏文件上继续的修改。
3. 按 verification-before-completion 获得最新完整验证证据。
4. 明确列出未执行验证及客观原因。
5. 只清理明确标注的 ephemeral harness；用户要求的测试、回归测试和正式测试属于交付物，禁止清理。

最终回复使用四行：目标、改动、验证、风险/未执行项。验证行必须包含命令、退出码和失败数；证据不足时只报告实际状态，不使用完成措辞。

## 核心黑名单

- 未命中 `DIRECT_PATCH` / `SIMPLE_PATCH` 时主会话直接编辑项目源码，或静默修补失败 patch。
- 未确认计划或四项模型/effort 就派发 coding。
- 信任 worker 自报而不检查实际 diff 和重跑验证。
- 并行修改重叠文件，或把 raw 子代理对话转给其他代理。
- reviewer 自行修复，或修复后继续沿用旧 verdict。
- review 子代理跳过 review_preflight.py 确定性前置（detect/normalize/negative coverage/P0-P3），或无证据审查仍给出 verdict。
- 未检查工作区就重复派发，或使用破坏性回滚覆盖用户改动。
- 无最新命令证据声称通过，或误删正式/回归测试。
- 把 run 台账写入 .git 元数据区或项目工作树；台账只允许落在 $CODEX_HOME/state/coding-review-pipeline/<workspace-id>/runs/（legacy 用 run_ledger migrate 迁移，不手工拷贝）。
- 把缺类、编译、测试收集、语法、依赖或环境错误称为 RED，或在 `tests_run == 0` 时进入 GREEN。
- 对无状态变化的 wait timeout 输出 commentary、读取工作区验活、催促代理或短间隔轮询。
- ready 非空且存在空闲 slot 时只派一个任务、等待整批 coder 后才启动 task review、因一组冲突全局串行、或逐个等待 active agent。
- advisor 暴露新的高影响互斥选择后由主会话自行定案，或在聚焦复核只剩措辞时继续 advisor 自循环。
- 为同一 run 创建 canonical ledger 之外的 task/verification/completed 平行事实文件。
- 子代理不可用时未经用户选择"主会话直改直审"就由主会话代写代码，或在报告替代选项前静默放弃 fresh review。
