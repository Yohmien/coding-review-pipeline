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
Error Prone 的扫描算法，只调用其 CLI 并归一化输出；无 CLI 时输出 SKIPPED 并继续。OCR 只是
optional rule enrichment：ocr 不可用时输出 SKIPPED 并继续，绝不 STOP review。

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
