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

1. 主会话不得直接编写项目源码；修复规格后重新委派，不静默代写。
2. 计划和模型选择都由主会话与用户确认，子代理不得重复前置审批。
3. 不从历史记忆猜测模型、effort 或工具能力；每次编码任务读取当前 live schema。
4. 不静默替换模型、降低 effort、跳过 reviewer 或把部分验证说成全部通过。
5. 并行 coding 子任务的可写文件集合必须互不重叠；依赖链和共享文件必须串行。
6. reviewer 行为只读，不实现修复；任何修复都使旧 verdict 失效。

## 首次响应硬门禁

对用户的首次响应只能承诺当前获准阶段，并满足以下可观察条件：

1. 代码变更请求尚未完成探索与完整计划确认时，只说明将先读取规约、检查工作区、定位证据；G1（见 [references/routing-gates.md](references/routing-gates.md)）命中还须先完成追问，再提交计划。不得推荐或列出任何模型 ID、reasoning_effort，不得请求模型选择，也不得承诺开始编码。
2. “只读 review”“先 review”或“顺便修一下”等未明确修复项和写入授权的请求，只承诺并执行只读 review；不得预告后续自动修复、模型选择或 coding。报告发现后，等待用户明确选择修复项并授权。
3. 用户要求复用历史模型或允许自动换模时，先读取当前 live schema。原选择任一项不受支持即 🔴 CHECKPOINT · 🛑 STOP：列出缺失能力并请用户重选；不得接受“自动换一个”的授权作为静默替换依据。

首次响应不满足任一条件时，不进入后续状态机，先重写响应直至满足。

## 按需读取

Gate 与路由以 `scripts/route_context.py` 的 G1-G5 输出为准（可读契约见 [references/routing-gates.md](references/routing-gates.md)）；只加载输出命中的 reference 与 skill，不复制其规则。

- 在形成任何 coder、advisor 或 reviewer 提示前，读取 [references/task-contracts.md](references/task-contracts.md)。
- verify/complete 阶段或 completion_claim 命中时，读取 [references/verification-routing.md](references/verification-routing.md)。
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

- `open-code-review`（`ocr` CLI）：optional rule enrichment。来源 [alibaba/open-code-review](https://github.com/alibaba/open-code-review)；安装 `npm install -g @alibaba-group/open-code-review`（需 Node ≥14；要求 Git ≥2.41）。可用性与消费口径见第 7 节。
- CodeGraph 图谱索引：search-gates 的图谱层。缺失时 search-gates 按自身兜底表降级 rg 锁定（显式路径）或报告缺失，不假装命中。

### 安装与验证

- 安装到 `$CODEX_HOME/skills`（默认 `~/.codex/skills`），目录名必须等于 frontmatter `name`。
- 各依赖的权威来源与安装命令见开源仓库 README 的三分类依赖表；本机可用 `skill-installer` 按 GitHub 目录 URL 安装。
- 验证：目录存在、frontmatter `name` 与目录名一致、`references/` 全部文件与 `scripts/` 全部脚本完整、兄弟 skill `contract-executor` 已同步安装（缺失 fail closed）、`python scripts/validate.py skills/coding-review-pipeline skills/contract-executor` 全部输出 `Skill is valid!`。

## 核心状态机

主流程：1 探索与定案 → 1.5 G1 追问门禁 → 2 完整计划确认 → 3 Live 模型确认 → 4 生成任务契约 → 5 风险路由与实施 → 6 主会话独立复验 → 7 Fresh review 与修正循环 → 完成条件（completion_gate 放行）。

### 1. 探索与定案

1. 用 `scripts/change_facts.py` 收集 change facts，并检查 AGENTS.md、git status --short 和用户已有改动。
2. 按 search-gates 获取足够上下文；目标在配置或测试文件时再精确读取源码。
3. 缺陷先复现并形成根因证据，禁止猜测式修改。
4. 主会话定案接口、数据契约、边界、异常、事务、并发、幂等、超时、重试和补偿。
5. 判定风险等级与任务依赖，列出允许修改的文件集合。

输出：change facts、根因证据、已定案接口与边界、允许修改的文件集合。

### 1.5 G1 追问门禁（one-hop）

是否追问由 `scripts/route_context.py` 的 G1 User Decision Gate 决定，可读契约见 [references/routing-gates.md](references/routing-gates.md)：只有输出 `REQUIRES_USER_DECISION` 才路由 grill-with-docs（tools 附带 request_user_input）；多文件、多模块、CodeGraph、rg、工具/测试/阅读数量等一律不是 G1 触发条件。命中后按 grill-with-docs（及传递的 grilling / domain-modeling）执行追问，细节不复制到本 skill；G1 输出 `NONE` 时跳过，直接根据探索证据形成完整计划。

输出：G1 判定与已确认的用户决策（未命中则直接进入完整计划）。

### 2. 完整计划确认

计划必须建立在已确认的追问结论上（简单任务则建立在探索证据上），并至少包含：

- 目标、现状和影响范围。
- 拟修改文件、任务拆分、依赖与并行边界。
- 已定案的接口、数据和错误处理策略。
- 追问阶段已更新的 glossary / ADR（如有）及其与实施任务的关系。
- 测试与验证命令、风险、回滚或补偿。

🔴 CHECKPOINT · 🛑 STOP：用户未确认完整计划前，不展示模型选择，不派 coding 子代理，不修改项目代码。

输出：已确认的完整计划。

### 3. Live 能力与模型确认

计划确认后：

1. 从当前 spawn_agent schema 读取模型与 reasoning_effort 枚举，并核实可显式传递所选值；读不到 live schema（spawn_agent 工具缺失、枚举为空或读取失败）时同样触发 🔴 CHECKPOINT · 🛑 STOP：报告缺失能力，不得回退到历史记忆、静态清单或上次会话记录猜测。
2. 依据已确认计划的复杂度，先紧凑推荐 coding model、review model、coding effort、review effort 四项；模型使用 live schema 中的完整 ID，effort 使用精确枚举值。
3. 在请求选择前检查展示覆盖率：若当前推荐与候选未覆盖全部模型条目或全部 effort 条目，紧凑展示所有模型完整 ID、effort 并集，以及每个模型支持的 effort；已覆盖时不重复展开。
4. 请用户确认四项。自由文本可一次填写；使用结构化输入时，以当前 schema 为准，用最少轮次收齐四项，每题必须使用允许的最大显式选项数，并把推荐项置顶。
5. 结构化输入无法容纳全部条目时，先在正文完整展示清单；UI 只放允许数量的优先候选，其余条目通过自由输入完整 ID 或精确 effort 值选择，不得因 UI 限制隐藏或排除任何 live 选项。
6. 用户已在本会话确认且 schema 仍支持时复用；用户改选、上下文缺失或 schema 变化时重新确认。

🔴 CHECKPOINT · 🛑 STOP：四项未确认，或 live 工具不支持任一选择时，停止编码并报告缺失能力；不得使用静态候选、静默降级或主会话代写。

输出：已确认的 coding model、review model、coding effort、review effort 四项。

### 4. 生成任务契约

1. 按 task-contracts.md 生成五段 coder packet：目标、文件所有权、接口、约束、验证。
2. 派发前用 `scripts/validate_task_packet.py` 校验 packet；输出 BLOCKED 时按 evidence 修正，不得绕过派发。
3. 规格定案所有影响接口、契约、安全和范围的判断；允许 coder 处理局部低风险实现判断，但必须在返回中报告。
4. 每个任务声明可写集和必要只读依赖；禁止转发主会话完整对话或其他子代理 raw 对话。
5. 单文件单点任务保持单任务，不为并行而过度拆分；拆分与并行可行性以 task_graph.py 的 CAN 输出为准。

输出：通过 `validate_task_packet.py` 校验的 coder packet。

### 5. 风险路由与实施

风险等级取 route_context.py 的 G2 输出（NORMAL / ELEVATED / HIGH），并行模式取 G4 输出（single / serial / parallel-safe）；生命周期动作只允许 agent-lifecycle.md 的固定 8 actions，收敛路由只允许 task-convergence.md 的固定 7 routes。

| 等级 | 条件 | 流程 |
|---|---|---|
| 普通单点 | 边界明确、单一责任、影响局部 | coder → 主会话检查 diff/复验 → fresh final reviewer |
| 多任务/跨模块 | 多个互不重叠文件集或明确依赖链 | 无依赖 coder 并行；存在跨任务接口或数据契约依赖时，主会话先核对前驱实际 diff 与既定契约，高风险承诺再交 fresh commitment-boundary advisor，随后派下游 coder；全部完成后 fresh integration reviewer |
| 高风险 | 并发、事务、安全、迁移、公共 API、外部副作用、宽影响重构 | 编码前 fresh commitment-boundary advisor；计划修正后再实施；最后 fresh integration reviewer |

advisor 只给 proceed | change | stop，不能替主会话决策。coder 只修改授权文件，按 packet 验证并返回实际证据。

输出：按 G2/G4 路由的实施计划与派发指令。

### 6. 主会话独立复验

每轮 coder 返回后，主会话必须：

1. 检查 git status --short、完整 diff、未跟踪文件和允许范围。
   存在未跟踪新文件时，先对每个新文件执行 `git add -N <file>`（intent-to-add，仅让 diff 可见）
   再运行 `git diff --check`；不得因此顺手 `git add` 提交。
2. 发现范围外变化立即停止，不静默归入任务。
3. 按 verification-routing.md 重跑适用命令并读取完整输出、退出码和失败数。
4. 将实际 diff 和主会话证据提供给 fresh reviewer；不以 coder 摘要代替。

输出：复验证据包（完整 diff、命令输出、退出码、失败数）。

### 7. Fresh review 与修正循环

reviewer 必须使用独立、上下文干净的线程，行为只读，并按 task-contracts.md 返回。派发后第一步必须运行 `scripts/review_preflight.py`（确定性前置，不调用 LLM；完整口径见 [references/review-routing.md](references/review-routing.md)）：

1. 以 `--facts <change facts>` 加可选 `--task-facts` / `--verification` 运行 review_preflight.py：detect-and-reuse 可用 analyzer、归一化 finding、diff 归因与去重、构建 negative coverage、打包 P0-P3 review context。
2. 消费 preflight 输出审查：attributable 的机器阻断（new secret、known vulnerable dependency、verification exit_code != 0、project-configured analyzer hard failure）直接采信；MACHINE COVERAGE 的 clean/skipped/failed/unsupported 决定 FOCUS ON 与预算分配；`review_context` 按 P0-P3 逐级消费，不在一启动就搜全仓。
3. ocr 只是 optional rule enrichment：preflight 检测到 `ocr` 时，`ocr.rule_context` 作为附加规则源参考；不可用时 preflight 输出 `ocr.state=skipped` 并继续，review 照常按第 1-2 步完成，绝不 STOP、绝不跳过规则审查。

审查结论按 task-contracts.md 返回：

- ship：目标、范围和证据足以交付。
- fix-first：列出文件、位置、证据和必需修复。
- rethink：架构或契约需要主会话重新定案。

fix-first 路由回原 coder；不可恢复时用相同已确认 coding 设置派后续 coder。rethink 回到探索与计划阶段。任何代码变化后必须重新复验并获取 fresh verdict。

单个任务和集成 review 各最多 3 轮；仍不收敛时停止并向用户报告累计证据。独立任务可继续，但不得在未全部 ship 前声明整体完成。

输出：fresh verdict（ship / fix-first / rethink）。

## 工作树、线程与恢复

- 派发前建立 changed-file baseline，保留已有脏文件和用户修改；具体做法见 recovery-and-failures.md。
- 只回收已完成且无需追问、修复或恢复的线程；先保存任务状态、实际文件清单、证据和 verdict。
- 中断恢复时先检查工作树与线程状态，再从最近安全阶段继续；不得未检查就重复派发或覆盖改动。
- 工具、模型、线程、范围或验证失败按 recovery-and-failures.md 的 if-then 表处理，失败路径不得吞掉。

## 完成条件

🔴 CHECKPOINT · 🛑 STOP：`scripts/completion_gate.py` 未输出 `COMPLETE_ALLOWED` 不得声明完成、不得使用完成措辞；输出 `BLOCKED` 时按 reasons 逐项闭环。

只有同时满足以下条件才可声明完成（确定性检查以 `scripts/completion_gate.py` 输出为准，`COMPLETE_ALLOWED` 才放行）：

1. 全部任务获得当前 diff 对应的 fresh ship。
2. 主会话确认最终实际改动文件均在范围内，并标注在原有脏文件上继续的修改。
3. 按 verification-before-completion 获得最新完整验证证据。
4. 明确列出未执行验证及客观原因。
5. 只清理明确标注的 ephemeral harness；用户要求的测试、回归测试和正式测试属于交付物，禁止清理。

最终回复使用四行：目标、改动、验证、风险/未执行项。验证行必须包含命令、退出码和失败数；证据不足时只报告实际状态，不使用完成措辞。

## 核心黑名单

- 主会话直接编辑项目源码或静默修补失败 patch。
- 未确认计划或四项模型/effort 就派发 coding。
- 信任 worker 自报而不检查实际 diff 和重跑验证。
- 并行修改重叠文件，或把 raw 子代理对话转给其他代理。
- reviewer 自行修复，或修复后继续沿用旧 verdict。
- review 子代理跳过 review_preflight.py 确定性前置（detect/normalize/negative coverage/P0-P3），或无证据审查仍给出 verdict。
- 未检查工作区就重复派发，或使用破坏性回滚覆盖用户改动。
- 无最新命令证据声称通过，或误删正式/回归测试。
- 把 run 台账写入 .git 元数据区或项目工作树；台账只允许落在 $CODEX_HOME/state/coding-review-pipeline/<workspace-id>/runs/（legacy 用 run_ledger migrate 迁移，不手工拷贝）。
