# Coding-Review Pipeline [![skills.sh](https://skills.sh/b/Yohmien/coding-review-pipeline)](https://skills.sh/Yohmien/coding-review-pipeline)

> 作者：Yohmien；仓库名：`coding-review-pipeline`。

为真实工程而写的 Codex 技能。它编排一条完整的变更流水线：探索 → 复杂计划追问 → 计划确认 → live 模型确认 → 任务契约 → coding 子代理实施 → 主会话独立复验 → fresh review → 完成验证。

流水线把架构、契约和风险决策留给主会话与用户，把实际 diff 和命令证据留给工作树；子代理报告只是 claims，不能替代证据。依赖被刻意做成**小、可组合、与模型无关**的独立 skill，按需加载，而不是把细则全部复制进本 skill。

## 为什么存在这套依赖

这个 skill 不是把工程纪律写死，而是把纪律外包给一组可组合的 skill。每条依赖对应一个真实失败模式：

1. **计划与用户目标错位**——agent 不知道用户真正要什么就开干。修复：`grill-with-docs`（复杂计划命中时先追问全部细节，再输出完整计划）。
2. **术语与领域语言漂移**——20 个词能讲清的用了 200 个，代码命名与领域脱节。修复：`domain-modeling` 随追问同步维护 `CONTEXT.md` glossary 与 ADR。
3. **搜索与影响面靠猜**——跨模块改动没有证据支撑。修复：`search-gates` 固定「图谱 → 记忆 → rg → 子代理展开」的搜索闸门。
4. **跳过验证就声称完成**——把「我觉得通过了」当证据。修复：`verification-before-completion` 强制最新命令证据。
5. **过度工程**——为不存在的问题做抽象。修复：`ponytail` 在编码与 review 阶段强制最简可行解。
6. **缺陷乱猜修复**——症状修复掩盖根因。修复：`systematic-debugging` 强制复现-根因-假设-验证闭环。
7. **先写码后写测试**——可测实现没有红灯先行。修复：`test-driven-development`。
8. **Java 规约不一致**——团队手册与代码现实脱节。修复：`alibaba-java-development-guide`（仅 Java 项目触发）。

## 工作流程

一条流水线，九个阶段：

探索与定案 → 复杂计划追问门禁 → 完整计划确认 → live 模型确认 → 生成任务契约 → 风险路由与实施 → 主会话独立复验 → fresh review → 完成条件

1. **探索与定案**——读取项目规约与工作树状态，按 `search-gates` 定位证据；缺陷先复现根因；主会话定案接口、数据契约、事务、并发、幂等、超时、重试与补偿，列出允许修改的文件集。
2. **复杂计划追问门禁**——命中复杂条件（多模块、公共 API、数据库/迁移、事务、并发、安全、外部副作用或宽影响重构）时，调用 `grill-with-docs` 先问清全部细节再出计划；互斥选项优先用 `request_user_input` 按轮收齐，推荐项置顶。
3. **完整计划确认**——计划必须建立在已确认的追问结论上；用户确认前不展示模型选择、不派 coding 子代理。
4. **live 模型确认**——每次从 `spawn_agent` 的 live schema 读取模型与 `reasoning_effort`，推荐并请用户确认 coding/review 四项；任一项不受支持即停止报告，不静默替换或降级。
5. **生成任务契约**——按五段 packet（目标、文件所有权、接口、约束、验证）下发 coder，声明可写集与只读依赖，禁止转发 raw 对话。
6. **风险路由与实施**——单点任务直接 coder；多任务按文件集是否重叠决定并行/串行；并发、事务、安全、迁移、公共 API 等高风险先经 fresh advisor 检查再实施。
7. **主会话独立复验**——检查 `git status`、完整 diff 与未跟踪文件，重跑验证命令并读取退出码与失败数，不以 coder 摘要代替证据。
8. **fresh review**——独立干净线程的 reviewer 只读返回 ship / fix-first / rethink；任何代码变化后重新复验并获取新 verdict，单个任务与集成各最多三轮。
9. **完成条件**——全部任务获得当前 diff 对应的 fresh ship，且具备最新验证证据、明确未执行项，才允许使用完成措辞。

## 优势

- **决策与执行分离**——架构、契约与风险决策归主会话和用户，coding 子代理只在定案边界内实施；计划、模型和 effort 都需用户确认，不自动越权。
- **先对齐再动手**——复杂计划强制追问门禁，问清全部细节（优先结构化选项）后才输出计划，避免「你以为他要 A，他做出来 B」的错位。
- **不静默降级**——模型与 effort 每次以 live schema 校验；依赖缺失、能力不足时报告并停止，不用缓存清单或替代品硬顶。
- **证据驱动**——只采信实际 diff、命令输出与退出码；reviewer 独立只读、线程干净，防止自审自批。
- **小且可组合**——纪律外包给 `grilling`、`domain-modeling`、`search-gates`、`ponytail` 等独立 skill，按需加载、与模型无关，可单独替换或扩展。
- **可恢复可审计**——派发前建立工作树基线，中断后从最近安全阶段继续；失败路径按 if-then 表处理，不吞错。

## 安装（30 秒设置）

前置：Codex（CLI 或 Desktop）。本 skill 与全部依赖都安装到 `$CODEX_HOME/skills`（默认 `~/.codex/skills`），目录名必须与 SKILL.md frontmatter 的 `name` 一致。

> [!IMPORTANT]
> Codex 的 skills **没有依赖解析**：安装本 skill 不会自动安装它的依赖。下面「依赖安装」必须一起执行，否则流水线会在加载阶段报告缺失并停止，而不是静默降级。

### 方式 1：在 Codex 会话内安装（推荐）

让 Codex 使用内置 `$skill-installer`：

```text
$skill-installer install https://github.com/Yohmien/coding-review-pipeline/tree/main/skills/coding-review-pipeline
```

依赖同理，逐个给出目录 URL（见下方依赖表）。安装完成提示：skill 将在**下一轮对话**生效。

### 方式 2：skills.sh CLI

```bash
npx skills@latest add Yohmien/coding-review-pipeline
```

选择要安装的 skills 时，至少勾选 `coding-review-pipeline` 以及依赖表中的全部条目。

### 方式 3：手动复制

```bash
# macOS / Linux
mkdir -p ~/.codex/skills
cp -R skills/coding-review-pipeline ~/.codex/skills/

# Windows PowerShell
Copy-Item -Recurse skills/coding-review-pipeline -Destination "$HOME\.codex\skills\"
```

### 依赖安装

直接依赖与传递依赖如下；外部依赖建议用各来源的官方安装方式，随本仓库分发的依赖由安装脚本直接复制，或直接运行本仓库的 `scripts/install-deps` 一键装齐（见「仓库结构」）。

```bash
# macOS / Linux
bash scripts/install-deps.sh

# Windows PowerShell
powershell -ExecutionPolicy Bypass -File scripts/install-deps.ps1
```

| 依赖 skill | 角色 | 权威来源 | 安装方式 |
|---|---|---|---|
| `grill-with-docs` | 直接依赖：复杂计划追问门禁 | [mattpocock/skills](https://github.com/mattpocock/skills) | `$skill-installer install https://github.com/mattpocock/skills/tree/main/skills/engineering/grill-with-docs` 或 `npx skills@latest add mattpocock/skills` |
| `grilling` | 传递依赖：决策树与分轮追问 | [mattpocock/skills](https://github.com/mattpocock/skills) | `https://github.com/mattpocock/skills/tree/main/skills/productivity/grilling`（同上） |
| `domain-modeling` | 传递依赖：glossary / ADR 维护 | [mattpocock/skills](https://github.com/mattpocock/skills) | `https://github.com/mattpocock/skills/tree/main/skills/engineering/domain-modeling`（同上） |
| `search-gates` | 直接依赖：结构搜索闸门 | 随本仓库开源（`vendor/skills/search-gates`） | 随仓库复制，`install-deps` 自动处理 |
| `verification-before-completion` | 直接依赖：完成声明门禁 | [obra/superpowers](https://github.com/obra/superpowers) | `npx skills@latest add obra/superpowers --skill verification-before-completion -y -g` |
| `ponytail` | 直接依赖：最简可行解纪律 | [DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail) | `npx skills@latest add DietrichGebert/ponytail --skill ponytail -y -g` |
| `systematic-debugging` | 直接依赖：根因调试闭环 | [obra/superpowers](https://github.com/obra/superpowers) | `npx skills@latest add obra/superpowers --skill systematic-debugging -y -g` |
| `test-driven-development` | 直接依赖：红灯先行 | [obra/superpowers](https://github.com/obra/superpowers) | `npx skills@latest add obra/superpowers --skill test-driven-development -y -g` |
| `alibaba-java-development-guide` | 直接依赖（条件）：Java 项目 | [Sxuan-Coder/alibaba-java-development-guide](https://github.com/Sxuan-Coder/alibaba-java-development-guide)（SKILL.md 在仓库根目录） | `git clone https://github.com/Sxuan-Coder/alibaba-java-development-guide.git ~/.codex/skills/alibaba-java-development-guide`（目录名即 skill 名），或 `npx skills@latest add Sxuan-Coder/alibaba-java-development-guide` |

### 验证安装

```bash
# 列出已安装目录（应看到 coding-review-pipeline 与全部依赖）
ls ~/.codex/skills

# 校验 frontmatter：目录名必须等于 SKILL.md 中的 name
head -3 ~/.codex/skills/coding-review-pipeline/SKILL.md

# 运行仓库自带校验（复用 openai/skills 的 quick_validate 逻辑）
python scripts/validate.py ~/.codex/skills
```

期望输出：每个 skill 打印 `Skill is valid!`；`name` 与目录名一致。

## Reference：依赖清单
### 直接依赖（本 skill 显式调用）

- **grill-with-docs** — 复杂计划命中时的追问门禁：先问清全部细节，再输出完整计划；组合 `grilling` 与 `domain-modeling`，追问结论是后续计划、glossary 与 ADR 的唯一输入。
- **search-gates** — 结构、调用关系、数据流与影响面的搜索闸门；按「图谱 → 记忆 → rg → 子代理展开」四层，信息足够立即止步。
- **verification-before-completion** — 完成声明前的证据门禁；没有最新命令输出、退出码与失败数，不允许使用完成措辞。
- **ponytail** — 编码与 review 阶段的最简可行解纪律；拒绝过度抽象与多余的依赖。
- **systematic-debugging** — 缺陷修复前强制复现、根因、假设与验证闭环；禁止猜测式修改。
- **test-driven-development** — 可测实现的红灯先行纪律；先写失败测试，再实现，再重构。
- **alibaba-java-development-guide** — 仅 Java 项目按任务主题加载；统一编码规约、异常日志、数据库与工程结构标准。

### 传递依赖（经 grill-with-docs 间接使用）

- **grilling** — 追问的底层原语：把决策映射为设计树，按 frontier 分轮提问，每个问题给出推荐答案；事实由主会话查明，决策交给用户。
- **domain-modeling** — 领域模型的主动维护：术语定案即更新 `CONTEXT.md` glossary；只有难逆转、缺背景会意外、存在真实权衡时才新增 ADR。

### 内置引用（非 skill，随本仓库分发）

`references/task-contracts.md`（coder/reviewer 任务契约）、`references/verification-routing.md`（验证命令路由）、`references/recovery-and-failures.md`（工作树、线程与失败恢复）。它们随 `skills/coding-review-pipeline/` 一起复制，无需单独安装。

## 缺失依赖时的行为

流水线的不可变原则之一：**不静默降级**。

- 加载不到 `grill-with-docs` 时，复杂计划不进入追问门禁，主会话报告缺失并停止，而不是改用「简化版追问」。
- 加载不到 `verification-before-completion` 时，不声称验证通过。
- 加载不到 `search-gates` 时，不退回无闸门搜索。

这也是为什么依赖必须显式安装：流水线的纪律密度依赖这些 skill 真实存在。

## For AI agents

安装本 skill 的 agent 请注意：

1. 安装写全局目录 `~/.codex/skills`；在受限沙箱中会被拦截，需提升权限或由用户手动复制。
2. 安装完成后 skill 在**下一轮对话**生效；本会话内不要假设它已可加载。
3. 校验三件事：目录存在、frontmatter `name` 等于目录名、`references/` 三个文件完整。
4. 复杂计划必须走 `grill-with-docs` 追问门禁；优先使用 `request_user_input` 表达互斥选项，问题数/选项数受 live schema 限制，超出按依赖与优先级拆轮。
5. 依赖缺失时报告缺失项与安装命令，不静默降级、不把部分验证说成全部通过。

## 仓库结构

```text
.
├── README.md
├── LICENSE
├── skills/
│   └── coding-review-pipeline/
│       ├── SKILL.md
│       └── references/
│           ├── task-contracts.md
│           ├── verification-routing.md
│           └── recovery-and-failures.md
├── vendor/skills/                  ← 随本仓库开源的依赖副本
│   └── search-gates/
├── scripts/
│   ├── install-deps.sh             ← 一键安装全部依赖（含 vendor）
│   ├── install-deps.ps1            ← Windows 等价脚本
│   └── validate.py                 ← 校验 ~/.codex/skills 下全部相关 skill
└── .github/workflows/validate.yml  ← CI：对 skills/ 与 vendor/ 跑 validate.py
```

`vendor/skills/` 中每个目录的 SKILL.md 保留原 frontmatter `name`；复制到 `$CODEX_HOME/skills/` 时以该 `name` 为目录名。
