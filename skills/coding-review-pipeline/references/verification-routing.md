# 验证路由（Verification Router）

本参考将“需要跑哪些验证”尽量程序化：`run_ledger.py verification_tier` 依据 change facts + task facts 输出唯一 tier 建议与 reasons；无法程序化证明时返回 `null`，由 Main 决定。程序只决定 CAN，最终命令仍以仓库实际构建/测试设施为准。

## Verification Tier

输出四档之一：`TARGETED`、`MODULE`、`INTEGRATION`、`FULL`，按优先级取最高适用档：

| 优先级 | 触发条件 | tier |
|---|---|---|
| 1 | task facts `risk == HIGH`，或 transaction/security/concurrency/external side effect 候选为 `candidate/confirmed` | `FULL` |
| 2 | `migration_changed`，或 `dependency_manifest_changed`/`lockfile_changed`，或 contract/interface 类文件，或 `public_api_candidate` 命中 | `INTEGRATION` |
| 3 | 变更跨多个 module | `INTEGRATION` |
| 4 | 变更落在单个 module | `MODULE` |
| 5 | `tests_changed`，或仅单个文件变更 | `TARGETED` |
| — | 以上都无法证明（如无 module、无测试、多文件且无触发） | `null`（Main 决定） |

结果形如 `{"tier": "MODULE", "reasons": ["single module changed"]}`；无法证明时为 `null`。source↔test 映射可程序化时给出 `TARGETED` 建议，否则由 Main 决定。

## 验证记录

每条验证记录固定为：

```json
{
  "command": "...",
  "exit_code": 0,
  "failure_count": 0,
  "diff_fingerprint": "...",
  "timestamp": "..."
}
```

硬约束：

- 缺 `exit_code`（或非整数）的验证记录一律视为无效，不得作为通过证据。
- 验证记录必须关联 `diff_fingerprint`；当前 diff 指纹与记录不一致即 STALE，必须重跑，不得沿用旧结论。
- 部分验证不得表述为全部通过；未运行项必须逐项列出原因、影响与残余风险。

## 证据要求（verification-before-completion）

宣称“完成/修复/通过”前，必须有最新命令、完整关键输出、退出码、失败数与实际文件清单。父会话独立重跑所有用于结论的命令，不以 coder 或 reviewer 的自述代替。

## 测试三档

对每项改动归入一档并记录依据：**可测**（新增/更新测试并保留红绿证据）、**客观不可测**（说明阻断事实与替代检查）、**用户批准豁免**（记录批准范围，不写成验证通过）。

红绿证据以目标行为缺失导致的失败为准，不得用语法/依赖/环境错误充当红态；实现前无法保留自然红态时使用五步红变体并确认 diff 不含临时变体。
