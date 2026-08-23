# Review Routing（确定性 review 前置）

本文件是 Phase 11 Review Core 的可读契约，描述 `scripts/review_preflight.py` 的职责与边界。
程序结果以 `review_preflight.py` 的 JSON 输出为准；本文件不增加程序之外的新触发条件。

## 定位：WRAP / ADAPTER

`review_preflight.py` 不是 Agent，也不是任何扫描算法的重实现。它只做：

```text
detect analyzers（检测，reuse-before-install）
run allowed analyzers（只调用已有 CLI）
normalize findings（归一化为统一 schema）
diff-filter + attribute（diff 归因与过滤）
deduplicate（跨源去重）
build negative coverage（机器覆盖与 FOCUS ON）
pack review context（P0-P3 与预算）
```

它绝不重写 ast-grep / Semgrep / Gitleaks / OSV-Scanner / reviewdog / SpotBugs / PMD / ArchUnit /
Error Prone 的扫描算法，只调用其 CLI 并归一化输出；无 CLI 时输出 SKIPPED 并继续。

## Reuse Before Install（五级优先级）

```text
1. project existing analyzer（项目已有配置）
2. project existing CI/report
3. locally available standalone analyzer（PATH 上可执行）
4. optional lightweight CRP tooling
5. new installation
```

`review_preflight.py` 实际只接线第 1 级与第 3 级：检测项目已有配置（`project_markers`）并调用
PATH 上可执行的 analyzer CLI；绝不执行第 5 级安装，也绝不为了 review 修改 `pom.xml` /
Gradle / 添加依赖或 CI 配置。第 2 级「project existing CI/report」复用是策略位、尚未接线
（Maven/Gradle 集成 analyzer 的 reason 原文为「build-integrated analyzer (Maven/Gradle);
reuse via CI/report not implemented」）；第 4 级 lightweight CRP tooling 不在本 adapter 内
实现。

## Reviewer Spawn Prompt 构造（主会话派发 reviewer 时加载本节）

reviewer 的 spawn prompt 必须直接以 task-contracts.md 的 `ROLE_LOCK` 开头，不得添加「按 coding-review-pipeline」「将契约交给 fresh reviewer」「请再派发 reviewer」等主会话叙事。`coding-review-pipeline` 只供主会话编排；不得要求 reviewer 加载或使用本 skill。spawn 返回 agent id 即表示 reviewer 已完成派发，子代理只消费 review package，不再转交、调度或创建任何代理。

prompt 正文只包含三要素：`ROLE_LOCK`、preflight 命令行、verdict 返回格式；审查事实包通过文件传递（reviewer 自行读取），不内联在 prompt 正文中。消费 preflight 输出：attributable 机器阻断（new secret、known vulnerable dependency、verification exit_code != 0、configured analyzer hard failure）直接采信；MACHINE COVERAGE 的 clean/skipped/failed/unsupported 决定 FOCUS ON 与预算；review_context 按 P0-P3 逐级消费，不在一启动就搜全仓。

## Analyzer 检测

有 adapter 归一化、可实际运行：

```text
gitleaks        secret / 新密钥
osv-scanner     dependency 漏洞（仅 dependency/lockfile 变更时）
semgrep         structural/static
ast-grep        structural rule
```

detect-only（保留检测条目与解析器，run 阶段一律 SKIPPED）：

```text
pmd             Java（需要项目专属 ruleset 参数，adapter 不具备）
checkstyle      Java style（需要项目专属 config 参数，adapter 不具备）
```

检测到但无 adapter（借用其思想、不作为 runtime 依赖）：

```text
reviewdog / sonar-scanner / sonarqube-scanner
```

第 81 节 Java analyzer 检测清单：PMD / SpotBugs / Error Prone / ArchUnit / Checkstyle / P3C /
Sonar 全部有检测条目。PMD / Checkstyle 的 CLI 需要项目专属 ruleset/config 参数，adapter
不具备，因此 run 阶段 detect-only：无论 CLI 是否存在一律输出 SKIPPED、reason
`detect_only / missing_required_args`，绝不自动 run、绝不成为机器阻断；检测条目与解析器保留，
但 CI/report 复用未接线，既有 CI 报告不会自动喂给解析器。仅 Maven/Gradle 集成、无独立 CLI
的 SpotBugs / ArchUnit / Error Prone / P3C / Sonar 输出 unsupported，reason 注明
build-integrated、reuse via CI/report not implemented，使 negative coverage 如实呈现。均只
复用已有配置，不自动安装。

## 语义同构与同族矩阵扫描（reviewer 必查）

新入口或新接口的校验与状态门槛必须与既有权威语义源逐条同构。权威语义源指既有 policy / service
中已定案的状态机、状态门槛或校验实现（写明具体类名与方法），不是新代码自带的更严副本。reviewer
发现新入口的校验或状态门槛与权威语义源不一致、或比权威语义更严时，禁止复制更严状态表：

1. 与既有权威语义源逐条对照，给出同构或差异结论。
2. 发现一处此类缺口，必须对同类「事件类型 × 状态组合」矩阵扫描并一次列全，不得只报单点。
3. 同族缺口 >=2 处时，verdict 必须是 rethink：该族语义需要主会话重新定案，不得以单点
   fix-first 补丁延续。

## 统一 Finding Schema

```json
{
  "id": "F...",
  "source": "gitleaks",
  "rule_id": "...",
  "path": "...",
  "start_line": 10,
  "end_line": 10,
  "severity_raw": "...",
  "severity": "HIGH",
  "category": "security",
  "message": "...",
  "confidence": "high",
  "in_diff": true,
  "attributable": true,
  "fingerprint": "..."
}
```

去重后合并为一个 finding 时增加 `sources=[...]`。

## CRP Severity

统一枚举 `BLOCKER / HIGH / MEDIUM / LOW / INFO`，每个 analyzer 有独立 mapping。工具自身的
`warning` / `info` 不得被简单提升为 CRP HIGH；例如 Semgrep `WARNING` → `MEDIUM`，PMD
`priority=1` → `HIGH`。

## 归因（Attribution）

只有同时满足「in changed file」且「与 current diff 有归属关系」才作为当前 task finding：

- in changed line → `in_diff=true` 且 `attributable=true`；
- in changed file 但 line 在 diff range 之外 → 不归属，不阻塞；
- 未变更文件 / 仓库历史问题 → 不归属，不阻塞（除非本次修改使其可达或恶化，交强模型判断）。

## 去重（Dedup）

按 `path + range + category + rule-family + message` 指纹归并；多 source 合并为 `sources=[...]`。
合并时 severity 取各 source 的最高 rank（如 Semgrep MEDIUM + PMD HIGH 同指纹 → HIGH），其余字段
保留首个。例如 Semgrep / PMD / Reviewer 指向同一问题时形成一个 finding。

## 机器阻断（Machine Blocking）

确定性工具已证明的下述情况直接 `BLOCKER / HIGH`，无需 LLM 再判断「是否真的失败」：

```text
new secret（本次新增密钥，Gitleaks 且 attributable）
known vulnerable dependency（OSV-Scanner，manifest/lockfile 变更）
build/test failure（verification 记录 exit_code != 0）
project-configured analyzer hard failure（项目配置的 analyzer 运行失败）
```

severity 尊重项目配置与工具语义；secret-like / 漏洞匹配 / 启发式匹配默认是 candidate finding，
置信度低、severity 不自动为 HIGH，不机器阻断；secret-like candidate 的 message 脱敏，只保留
rule/位置/截断掩码，不内嵌疑似密钥原文。

## Negative Coverage

review context 必须告诉 reviewer `MACHINE COVERAGE`：clean / skipped / failed / unsupported 清单，
以及：

```text
DO NOT SPEND PRIMARY REVIEW BUDGET ON:
- formatting
- unused imports
- analyzer-covered mechanical patterns

FOCUS ON:
- behavior correctness
- state transitions
- data consistency
- transaction semantics
- concurrency
- compatibility
- cross-file contract
- failure path
- test adequacy
```

## Review Context Packer（P0-P3）

reviewer 不在一启动就搜索整个仓库，固定四级：

```text
P0  task contract / current diff / change facts / verification / machine findings
P1  changed functions/classes context
P2  direct callers/callees/interfaces（仅 risk 要求且有 symbol index）
P3  broader repository search（仅 reviewer 请求）
```

每级输出 `tier / files / estimated_chars / omitted / reason`，尽量可观测 context 大小。
P0 的 `files` 纳入 `--facts`（change facts 路径）、`--task-facts` 与 `--verification` 传入的文件
路径，使 reason 文案与实际内容一致。
`--verification` 证据文件只接受两种形状：JSON list，或 `{"records": [...]}`；其余形状（如
`{"runs": [...]}`）以 invalid_input 退出。主会话打包验证证据时必须用其一，否则 preflight 无法
消费、reviewer 只能手工核对。

## 退出码

```text
0  ok
2  invalid_input（参数、facts/verification JSON 非法）
3  policy_blocked（如非 git 仓库）
1  internal_error
```

analyzer 缺失、失败、不支持均为非致命：输出 SKIPPED/failed/unsupported 后继续，进程退出码仍为 0。

## 与其他脚本的关系

- `change_facts.py`：产出 change facts（changed/untracked files、diff_ranges、security_candidate
  等），`review_preflight.py` 消费它做归因、secret-like 候选与 dependency/Java 门控。
- `route_context.py`：G1-G5 门控，负责路由 references/skills/tools，与 review 的 analyzer
  归一化正交。
- `run_ledger.py`：持久 run ledger 与 verification router；`--verification` 记录交给
  `review_preflight.py` 判 build/test failure 机器阻断。
## Finding 分级与 verdict 优先序（复审时加载）

finding 按四级归类，verdict 由最高级别决定，不按数量累计：

```text
S1 语义正确性    业务规则/状态迁移/数据一致性与已定案契约不符 → fix-first 或 rethink
S2 流程链路      调用顺序/事务边界/重试恢复路径存在真实断裂 → fix-first
S3 健壮性        真实可达输入的边界缺陷（有复现路径才计）→ fix-first 或 P2 备注
S4 风格与防御    命名/注释/日志措辞/不可达分支的理论防御 → 不阻断 ship，记 P3 备注
```

判定纪律：复审优先验证语义正确性与流程链路正确无错误；S4 类发现不得作为 fix-first 的唯
一理由，也不得因 S4 清单长而拉低 verdict。理论风险必须给出真实可达路径才能计入 S3；给
不出路径的归入 S4。同一位置重复出现且已有 ledger 记录的 S4 类意见不再重复提出。
## 不收敛升级与歧义处理（主会话收敛判定时加载）

**不收敛递进协议（轮次升级与刷新）**：任一任务复审进入第 3 轮且仍存在任何待修发现（不论是否与此前同题），必须主对话 rethink——重新探索、更严格收窄派发给编码子代理的内容（检查 packet 决策权是否过高导致编码方向偏移、DECIDED 清单是否不足），重新定案契约后派新 packet；此类 rethink 重派上限 **2 轮**。两轮 rethink 重派后复审仍报问题，立即 grill-me 向用户追问计划内容本身的问题（不依据代码自行推测），取得定案后重新派发并把该任务复审轮次刷新为 0 重新循环。每次 rethink 重派、grill-me 升级与轮次刷新都写入 canonical ledger（convergence_escalation 事件），供恢复与审计。

**同题复发立即升级**：复审发现与上一轮 fix 相同或同类的问题时，不等待第 3 轮——立即触发主对话 rethink（重探索 + 更严格收窄 packet）；存在歧义则同时 grill-me。同类复发说明 coder 决策难度过高而非执行不力：rethink 时必须把复发场景补入 DECIDED 清单或直接给出权威代码锚点，从源头消除 coder 需自行判断的部分。此升级同样计入 escalation_count 与上限。

**歧义 grill-me 硬规则**：coder 或 reviewer 遇到无法从仓库事实、packet DECIDED 清单或既有权威契约确认的歧义时，主会话必须调用 grill-me（grill-with-docs，传递 grilling / domain-modeling）向用户追问定案；严禁依据代码阅读自行推测业务语义并猜测实现。推测式修复即使测试全绿也视为无效，发现即回滚重做。避免无意义的编码细节追究考核以及编码方向偏移。

## Finding 双轴标注（severity × confidence）

每条 finding 除 S1-S4 severity 外必须标注 confidence：verified（有复现证据）、probable（有线索未复现）、speculative（推测）。speculative 默认不进入报告，仅当同类推测累计 >=3 条时合并为一条提示交主会话裁量。confidence 与 severity 独立：S1 语义问题若仅 speculative，需先取证升级为 probable 以上才计入 fix-first 依据。
**计数与防绕过**：escalation_count 由主会话在每次 rethink 重派时递增并写入 ledger，禁止通过
重置 review_round、重建 ledger、改名 run 或跳过 convergence_escalation 事件来绕过计数；reviewer
返回 verdict 时主会话必须先核对 ledger 中当前 review_round 与 escalation_count 再决定走 fix-first、
rethink 还是 grill-me，不得凭印象判断。escalation_total 达到 9 次仍不收敛 → 停止自动循环，
向用户报告完整证据链（每轮 verdict、修改摘要、验证结果）等待人工决策；这是唯一出口，不存在
继续重试的第三条路径。
