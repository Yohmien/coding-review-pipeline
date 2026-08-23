# Model Selection 细则（阶段 3 按需加载）

本文件是 SKILL.md 第 3 节的可读细则；只在进入阶段 3、处理模型选择 UI、或恢复 model_selection 时加载。规则以本文件为准，主文件不重复展开。

## 展示与确认口径

1. 依据已确认计划的复杂度，先紧凑推荐 coding model、review model、coding effort、review effort 四项；模型使用 live schema 完整 ID，effort 使用精确枚举值。
2. 请求选择前检查展示覆盖率：当前推荐与候选未覆盖全部模型条目或全部 effort 条目时，紧凑展示所有模型完整 ID、effort 并集及每个模型支持的 effort；已覆盖时不重复展开。
3. 用户确认四项：自由文本可一次填写；结构化输入以当前 schema 为准，用最少轮次收齐，每题使用允许的最大显式选项数，推荐项置顶。
4. 结构化输入无法容纳全部条目时，先在正文完整展示清单；UI 只放允许数量的优先候选，其余通过自由输入完整 ID 或精确 effort 值选择；不得因 UI 限制隐藏或排除任何 live 选项。
5. 复用条件：用户已在本会话确认且 schema 仍支持时复用；用户改选、上下文缺失或 schema 变化时重新确认。上下文压缩恢复时从 ledger 读回最近确认，展示上次四项并请求一行确认，不得重新展开全部选项。

## Schema 快照与探针合并

- 进入阶段 3 立即读取 spawn_agent live schema 并写入 ledger（model_schema_snapshot 事件）；用户询问可用模型或给出选择时先对照快照即时判定，不得在用户选择后才首次读取 schema。
- 首次使用未在本 run 成功派发过的模型组合时，把探针语义合并到该 run 首个正式任务 spawn prompt（首行要求回复 OK 后再执行任务）；返回无 error 且含正常任务产出即视为通过。首个正式派发连续 2 次同一线路级错误失败（协议错误、额度拒绝、运行时不可达）即判定不可用，按 Provider 连续失败降级处理；不得为探针单独消耗一轮派发。
## 程序化选择请求（model_prompt.py）

主会话不自行措辞请求用户确认。先把 live schema 快照与推荐四项写成 JSON，运行：
`python scripts/model_prompt.py --schema <snapshot.json> --recommend <recommend.json>`
脚本输出固定格式的确认块（四项编号推荐 + 完整枚举范围 + 回复示例），原样展示给用户。
schema 条目格式 `[{"id": "...", "efforts": ["..."]}]`；recommend 键为四个字段名。
