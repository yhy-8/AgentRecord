# SQLite 数据模型

分析数据库默认位于 `AnalysisReports/.analysis.sqlite3`。当前 schema 版本为 **3**。它保存派生运行状态和跨周期人物画像，不替代 Markdown 日记，也不允许 Agent 直接读写。

## 1. 设计目标

v3 删除旧版通用知识图谱，把数据库职责缩小为：

- 审计每次周报、月报运行；
- 保存 Agent 完成或失败产物；
- 将报告中的 `R-*` 映射到日记位置；
- 保存观点、理念、理想、行为模式和关注领域；
- 保存用户对画像的认可、否决和修正。

外部领域研究是当期报告内容，不进入长期人物画像。人物画像也不是用户原话本身；它始终是带来源、可否决、可重建的派生判断。

## 2. 连接与事务

每个存储操作创建独立连接，并启用：

- `PRAGMA foreign_keys = ON`
- `PRAGMA busy_timeout = 10000`
- `PRAGMA journal_mode = WAL`
- 写事务使用 `BEGIN IMMEDIATE`

事务只覆盖 SQL，不跨越模型或网络请求。普通日记记录不访问 SQLite，因此数据库锁定或损坏不能阻止用户写日记。

## 3. analysis_runs

一行表示一次周报或月报运行。

| 字段 | 含义 |
|---|---|
| `id` | 32 位 UUID 十六进制运行 ID |
| `kind` | `weekly` 或 `monthly` |
| `period_start/end` | 报告闭区间日期 |
| `origin` | `manual` 或 `auto` |
| `trigger` | `manual`、`scheduled` 或 `retry` |
| `model_name` | 本次模型显示名 |
| `status` | `running`、`completed`、`failed` |
| `input_hash` | 完整输入快照 SHA-256 |
| `report_path` | 成功交付文件路径 |
| `error` | 失败原因 |
| `created_at/completed_at` | 本地时间戳 |

手动触发只允许 `origin=manual, trigger=manual`。系统计划任务首次执行使用 `origin=auto, trigger=scheduled`；整点自动重试和 `/retry` 都使用 `origin=auto, trigger=retry`。

每次重跑都插入新行。Markdown 固定路径可以被覆盖，运行 ID 仍保留每次尝试的审计身份。

## 4. agent_artifacts

该表保存一次运行内各 Agent 的 JSON 产物。

| 字段 | 含义 |
|---|---|
| `id` | 产物 ID |
| `run_id` | 所属运行 |
| `agent` | Agent 或审查阶段名 |
| `revision` | 同运行、同 Agent 从 1 递增的调用次数 |
| `status` | 通常为 `completed` 或 `failed` |
| `payload_json` | 结构化载荷 |
| `error` | 解析、校验或调用错误 |
| `created_at` | 创建时间 |

唯一约束为 `(run_id, agent, revision)`。模型回答格式错误、结构校验失败或 Reviewer 输出不完整时，也保存失败产物以便定位具体阶段。

## 5. source_catalog 与 run_sources

`source_catalog` 以稳定 `R-YYYYMMDD-NNN` 为主键，保存：

- `relative_path`
- `source_date`、`source_time`
- `record_index`
- `speaker`、`tag`
- `content_hash`
- 最多 500 字符 `excerpt`
- `last_seen_at`

同一位置的日记内容发生变化时，目录项更新为最近一次看到的哈希和摘录。每次运行自身的完整输入状态由 `analysis_runs.input_hash` 审计。

`run_sources` 以 `(run_id, source_id)` 为联合主键，表示一次运行使用过哪些记录。外键保证运行不能指向不存在的来源目录项。

## 6. profile_entries

画像类别：

| `category` | 语义 |
|---|---|
| `viewpoint` | 对具体问题的观点或判断 |
| `principle` | 理念、判断原则或方法原则 |
| `ideal` | 长期希望趋近的状态或价值目标 |
| `behavior_pattern` | 多次记录支持的行为习惯或模式 |
| `interest` | 持续关注或反复探索的领域 |

主要字段：

| 字段 | 含义 |
|---|---|
| `id` | 持久画像 ID |
| `run_id` | 产生该版本的报告运行 |
| `title/statement` | 标题与陈述 |
| `status` | `accepted`、`rejected`、`superseded` |
| `confidence` | 0 到 1 |
| `source_refs_json` | 支撑该版本的 `R-*` 列表 |
| `first_observed/last_observed` | 记录中首次与最近观察日期 |
| `created_by` | `retrospective` 或 `user` |
| `supersedes_id` | 被当前版本替代的旧条目 |
| `created_at/updated_at` | 本地时间戳 |

Retrospective 的候选只有通过 Reviewer 才能为 `accepted`，但只有整份报告成功交付并把运行标为 `completed` 后才进入历史上下文。完成运行与旧版本 `superseded` 在同一事务中生效；研究板块或交付失败时，新候选不激活，旧版本仍有效。

失败运行的候选不会在下一轮自动清除，而是和失败 Agent 产物一起保留审计。重试创建新的运行；任何读取有效画像的查询都会排除这些失败运行，因此保留它们不会污染后续报告。

历史上下文只读取 `status=accepted` 且 `last_observed <= 报告周期结束日` 的条目。这个日期截断是防止未来信息进入过去报告的硬边界。

## 7. profile_feedback

该表记录用户通过 `/f` 做出的：

- `accept`
- `reject`
- `correct`

字段包括原条目、动作、可选替代条目和时间。否决将原条目标为 `rejected`；认可或修正创建 `created_by=user`、置信度为 1 的 accepted 新版本，并把原条目标为 `superseded`。所有动作保留审计，不改写历史报告。

## 8. 状态与版本链

画像状态变化只允许通过新候选或用户反馈表达：

```text
候选审查：新行 → accepted | rejected
版本更新：accepted 旧行 → superseded，新行 → accepted
用户否决：accepted 旧行 → rejected
```

没有通用关系表、节点边表或自由类型。画像之间的复杂联系应在当期报告正文中解释；没有稳定需求前不恢复知识图谱。

## 9. 开发阶段版本策略

项目尚处于快速开发阶段，不维护旧 SQLite schema 的兼容、迁移或自动备份代码：

- 版本为 0 时创建当前 v3 结构；
- 版本为 3 时直接使用；
- 其他版本直接报错，并保持主库、`-wal`、`-shm` 原样不动。

需要切换结构时，由开发者先判断人物画像反馈等数据是否值得保留；必要时人工导出或备份，然后手动删除三个数据库文件并重新启动。程序不得为了方便开发而静默删除不兼容数据库，更不得触碰 `Records/`。

## 10. 备份与恢复

- 原始日记与 v3 SQLite 应分别备份；只恢复日记会丢失画像反馈，但不会丢失用户原文。
- 运行中的 v3 数据库应使用 SQLite backup API 或同时一致处理主库、WAL、SHM，不能只复制主文件。
- 数据库错误不得触发对 `Records/` 的清理。
- 报告是可重建交付物，画像反馈是不可从原文完全推导的用户决定，恢复时应优先保护后者。
