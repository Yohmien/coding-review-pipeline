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

1. 代码变更请求尚未完成探索与完整计划确认时，只说明将先读取规约、检查工作区、定位证据；复杂计划还须先完成追问，再提交计划。不得推荐或列出任何模型 ID、reasoning_effort，不得请求模型选择，也不得承诺开始编码。
2. “只读 review”“先 review”或“顺便修一下”等未明确修复项和写入授权的请求，只承诺并执行只读 review；不得预告后续自动修复、模型选择或 coding。报告发现后，等待用户明确选择修复项并授权。
3. 用户要求复用历史模型或允许自动换模时，先读取当前 live schema。原选择任一项不受支持即 🔴 CHECKPOINT · 🛑 STOP：列出缺失能力并请用户重选；不得接受“自动换一个”的授权作为静默替换依据。

首次响应不满足任一条件时，不进入后续状态机，先重写响应直至满足。

## 按需读取

- 在形成任何 coder、advisor 或 reviewer 提示前，读取 [references/task-contracts.md](references/task-contracts.md)。
- 任务涉及代码或测试验证时，读取 [references/verification-routing.md](references/verification-routing.md)。
- 工作树已脏、多任务并行、线程中断或发生失败时，读取 [references/recovery-and-failures.md](references/recovery-and-failures.md)。
- 定位结构、调用关系、数据流或影响面时调用 search-gates，不在本 skill 复制搜索细则。
- 计划命中下述复杂条件时调用 grill-with-docs；该 skill 会组合 grilling 与 domain-modeling，必须先完成追问门禁再形成完整计划。
- 编码和 review 应用 ponytail；review 子代理第一步必须执行 open-code-review（ocr delegate），见“Fresh review 与修正循环”。
- 缺陷修复调用 systematic-debugging；可测实现调用 test-driven-development；完成声明前调用 verification-before-completion。只读取适用 skill，不复制其规则。

## 依赖与前置

本 skill 编排其他 skill，不复制其细则。依赖缺失时报告缺失项并停止对应流程，不静默降级、不找替代品硬顶。

### 必需依赖（缺一不可）

| skill | 来源 | 加载时机 |
|---|---|---|
| `grill-with-docs` | mattpocock/skills（`skills/engineering/grill-with-docs`） | 复杂计划命中追问门禁时 |
| `search-gates` | 随本仓库开源（`vendor/skills/search-gates`） | 结构、调用、数据流或影响面定位 |
| `verification-before-completion` | obra/superpowers（或等价 curated 来源） | 任何完成、通过、修复声明前 |
| `ponytail` | DietrichGebert/ponytail | 编码与 review 阶段 |
| `systematic-debugging` | obra/superpowers（或等价 curated 来源） | 缺陷修复 |
| `test-driven-development` | obra/superpowers（或等价 curated 来源） | 可测实现 |

### 工具级前置依赖

- `open-code-review`（`ocr` CLI，delegate 模式）：review 子代理第一步的确定性工程来源（文件筛选与规则解析）。来源 [alibaba/open-code-review](https://github.com/alibaba/open-code-review)；安装 `npm install -g @alibaba-group/open-code-review`（需 Node ≥14；要求 Git ≥2.41）。delegate 模式不需要配置 LLM。缺失或命令失败时，review 子代理按 recovery-and-failures.md 报告并停止，不降级为无规则审查。

### 传递依赖

- `grill-with-docs` 内部使用 `grilling`（决策树、分轮追问）与 `domain-modeling`（glossary/ADR）。本 skill 不直接调用，但任一缺失会使追问门禁失效，按必需依赖缺失处理。

### 安装与验证

- 安装到 `$CODEX_HOME/skills`（默认 `~/.codex/skills`），目录名必须等于 frontmatter `name`。
- 各依赖的权威来源与安装命令见开源仓库 README 的依赖表；本机可用 `skill-installer` 按 GitHub 目录 URL 安装。
- 验证：目录存在、frontmatter `name` 与目录名一致、`references/` 三文件完整、`quick_validate.py` 输出 `Skill is valid!`。

### 内置引用（非 skill，随本 skill 分发）

- `references/task-contracts.md`、`references/verification-routing.md`、`references/recovery-and-failures.md` 随本 skill 复制；任一缺失按 recovery-and-failures.md 的失败路径处理，不继续派发。

## 核心状态机

### 1. 探索与定案

1. 检查 AGENTS.md、git status --short 和用户已有改动。
2. 按 search-gates 获取足够上下文；目标在配置或测试文件时再精确读取源码。
3. 缺陷先复现并形成根因证据，禁止猜测式修改。
4. 主会话定案接口、数据契约、边界、异常、事务、并发、幂等、超时、重试和补偿。
5. 判定风险等级与任务依赖，列出允许修改的文件集合。

### 1.5 复杂计划追问门禁

满足任一条件即为复杂计划：

- 多模块、跨边界或包含两个以上有依赖关系的实施任务。
- 公共 API、数据库或迁移、事务、并发、安全、外部副作用或宽影响重构。
- 接口、数据契约、领域边界、兼容策略或失败处理仍有会实质改变方案的未决选择。
- 用户目标存在会影响范围、验收或边缘场景的关键歧义，不能由仓库事实直接消除。

命中后，在探索证据足够且输出完整计划前调用 grill-with-docs：

1. 按 grilling 建立决策树，区分可查事实与需用户决定的选择；事实由主会话通过环境和工具查明，不转问用户。
2. 按当前 frontier 分轮追问，每个问题给出推荐答案；依赖未决问题的分支留到后续轮次，不静默假设。
3. 当前环境提供 `request_user_input`，且本轮问题可表达为该工具支持的互斥选项时，必须使用该工具。按 live schema 的单次问题数和选项数上限组装问题，推荐项置顶并明确标记 `(Recommended)`。
4. frontier 超过 `request_user_input` 单次容量时，按依赖与优先级拆成多轮；收到本轮回答并更新决策树后再询问下一轮。不得把溢出问题附在工具调用前后的普通文本中，也不得为减少调用而改成整批文本提问。
5. 只有开放式回答、多选、参数填写、无法形成互斥选项，或当前环境确实没有 `request_user_input` 时，才使用简洁文本提问；回退时说明不适用结构化输入的原因。
6. 按 domain-modeling 核对现有 `CONTEXT.md` / `CONTEXT-MAP.md`；术语一旦定案即按需更新 glossary。只有同时满足难逆转、缺背景会意外、存在真实权衡时才按需新增 ADR。
7. 追问期间可展示决策树和已定案结论，但不得提前输出完整实施计划，也不得用计划草案替代用户决策。
8. 只有 frontier 为空且用户确认已达成共同理解，才进入“完整计划确认”。

未命中复杂条件时跳过 grill-with-docs，直接根据探索证据形成完整计划。复杂度不得仅按预计代码行数判断。

### 2. 完整计划确认

计划必须建立在已确认的追问结论上（简单任务则建立在探索证据上），并至少包含：

- 目标、现状和影响范围。
- 拟修改文件、任务拆分、依赖与并行边界。
- 已定案的接口、数据和错误处理策略。
- 追问阶段已更新的 glossary / ADR（如有）及其与实施任务的关系。
- 测试与验证命令、风险、回滚或补偿。

🔴 CHECKPOINT · 🛑 STOP：用户未确认完整计划前，不展示模型选择，不派 coding 子代理，不修改项目代码。

### 3. Live 能力与模型确认

计划确认后：

1. 从当前 spawn_agent schema 读取模型与 reasoning_effort 枚举，并核实可显式传递所选值。
2. 依据已确认计划的复杂度，先紧凑推荐 coding model、review model、coding effort、review effort 四项；模型使用 live schema 中的完整 ID，effort 使用精确枚举值。
3. 在请求选择前检查展示覆盖率：若当前推荐与候选未覆盖全部模型条目或全部 effort 条目，紧凑展示所有模型完整 ID、effort 并集，以及每个模型支持的 effort；已覆盖时不重复展开。
4. 请用户确认四项。自由文本可一次填写；使用结构化输入时，以当前 schema 为准，用最少轮次收齐四项，每题尽量使用允许的最大显式选项数，并把推荐项置顶。
5. 结构化输入无法容纳全部条目时，先在正文完整展示清单；UI 只放允许数量的优先候选，其余条目通过自由输入完整 ID 或精确 effort 值选择，不得因 UI 限制隐藏或排除任何 live 选项。
6. 用户已在本会话确认且 schema 仍支持时复用；用户改选、上下文缺失或 schema 变化时重新确认。

🔴 CHECKPOINT · 🛑 STOP：四项未确认，或 live 工具不支持任一选择时，停止编码并报告缺失能力；不得使用静态候选、静默降级或主会话代写。

### 4. 生成任务契约

1. 按 task-contracts.md 生成五段 coder packet：目标、文件所有权、接口、约束、验证。
2. 规格定案所有影响接口、契约、安全和范围的判断；允许 coder 处理局部低风险实现判断，但必须在返回中报告。
3. 每个任务声明可写集和必要只读依赖；禁止转发主会话完整对话或其他子代理 raw 对话。
4. 单文件单点任务保持单任务，不为并行而过度拆分。

### 5. 风险路由与实施

| 等级 | 条件 | 流程 |
|---|---|---|
| 普通单点 | 边界明确、单一责任、影响局部 | coder → 主会话检查 diff/复验 → fresh final reviewer |
| 多任务/跨模块 | 多个互不重叠文件集或明确依赖链 | 无依赖 coder 并行；存在跨任务接口或数据契约依赖时，主会话先核对前驱实际 diff 与既定契约，高风险承诺再交 fresh commitment-boundary advisor，随后派下游 coder；全部完成后 fresh integration reviewer |
| 高风险 | 并发、事务、安全、迁移、公共 API、外部副作用、宽影响重构 | 编码前 fresh commitment-boundary advisor；计划修正后再实施；最后 fresh integration reviewer |

advisor 只给 proceed | change | stop，不能替主会话决策。coder 只修改授权文件，按 packet 验证并返回实际证据。

### 6. 主会话独立复验

每轮 coder 返回后，主会话必须：

1. 检查 git status --short、完整 diff、未跟踪文件和允许范围。
2. 发现范围外变化立即停止，不静默归入任务。
3. 按 verification-routing.md 重跑适用命令并读取完整输出、退出码和失败数。
4. 将实际 diff 和主会话证据提供给 fresh reviewer；不以 coder 摘要代替。

### 7. Fresh review 与修正循环

reviewer 必须使用独立、上下文干净的线程，行为只读，并按 task-contracts.md 返回。派发后第一步必须执行 open-code-review delegate：

1. `ocr delegate preview --format json` 确定审查文件集、模式（workspace / range / commit）与 ref 元数据；按主会话给定的 diff 范围必要时带 `--from/--to` 或 `--commit`。
2. `ocr delegate rule <path...>` 按文件取规则组；共享同一规则组的文件合并审查，避免重复读取。
3. 按规则组逐文件审查：range 模式用 `git diff <merge_base>..<to>`、commit 模式用 `git show <commit>`、workspace 模式用 `git diff HEAD`（未跟踪新文件直接读全文），再对照规则审查，输出含 path、severity、category 与行号的评论。
4. 全部文件必须 reviewed 或显式 skipped（附原因），汇总给出 total_files、reviewed_files、skipped_files、coverage_rate，按严重度分组报告。

`ocr` 未安装或命令失败时，按 recovery-and-failures.md 的 if-then 表报告缺失并停止，不静默降级、不跳过规则审查。

审查结论按 task-contracts.md 返回：

- ship：目标、范围和证据足以交付。
- fix-first：列出文件、位置、证据和必需修复。
- rethink：架构或契约需要主会话重新定案。

fix-first 路由回原 coder；不可恢复时用相同已确认 coding 设置派后续 coder。rethink 回到探索与计划阶段。任何代码变化后必须重新复验并获取 fresh verdict。

单个任务和集成 review 各最多 3 轮；仍不收敛时停止并向用户报告累计证据。独立任务可继续，但不得在未全部 ship 前声明整体完成。

## 工作树、线程与恢复

- 派发前建立 changed-file baseline，保留已有脏文件和用户修改；具体做法见 recovery-and-failures.md。
- 只回收已完成且无需追问、修复或恢复的线程；先保存任务状态、实际文件清单、证据和 verdict。
- 中断恢复时先检查工作树与线程状态，再从最近安全阶段继续；不得未检查就重复派发或覆盖改动。
- 工具、模型、线程、范围或验证失败按 recovery-and-failures.md 的 if-then 表处理，失败路径不得吞掉。

## 完成条件

只有同时满足以下条件才可声明完成：

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
- review 子代理跳过 open-code-review delegate 第一步（preview/rule），或无规则审查仍给出 verdict。
- 未检查工作区就重复派发，或使用破坏性回滚覆盖用户改动。
- 无最新命令证据声称通过，或误删正式/回归测试。
