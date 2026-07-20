# SQLite 数据模型

分析数据库默认位于 `AnalysisReports/.analysis.sqlite3`。它保存派生运行状态、Agent 遥测/阶段缓存和跨周期人物画像，不替代 Markdown 日记，也不允许 Agent 直接读写。数据库不记录 schema 版本号。

## 1. 设计目标

当前结构使用精简画像模型，并把 Agent 调用遥测和已过审阶段缓存纳入审计产物：

- 审计每次每日画像、周报和月报运行；
- 保存 Agent 完成或失败产物；
- 保存请求耗时、token/缓存 token 和搜索证据；
- 仅复用同输入下已完全过审的 Agent 阶段；
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

一行表示一次每日画像、周报或月报运行。

| 字段 | 含义 |
|---|---|
| `id` | 32 位 UUID 十六进制运行 ID |
| `kind` | `daily_profile`、`weekly` 或 `monthly` |
| `period_start/end` | 分析闭区间日期；每日画像两者相同 |
| `origin` | `manual` 或 `auto` |
| `trigger` | `manual`、`scheduled` 或 `retry` |
| `model_name` | 本次模型显示名 |
| `status` | `running`、`completed`、`failed` |
| `input_hash` | 完整输入快照 SHA-256 |
| `report_path` | 成功交付文件路径；每日画像为 `NULL` |
| `error` | 失败原因 |
| `created_at/completed_at` | 本地时间戳 |

手动触发只允许 `origin=manual, trigger=manual`。每日画像只允许 `origin=auto`。系统计划任务首次执行使用 `trigger=scheduled`；整点自动重试和 `/retry` 使用 `trigger=retry`。

每次重跑都插入新行。Markdown 固定路径可以被覆盖，运行 ID 仍保留每次尝试的审计身份。

## 4. agent_artifacts

该表保存一次运行内各 Agent 的 JSON 产物。

| 字段 | 含义 |
|---|---|
| `id` | 产物 ID |
| `run_id` | 所属运行 |
| `agent` | Agent 或审查阶段名 |
| `revision` | 同运行、同 Agent 的审计产物从 1 递增的版本号 |
| `status` | 通常为 `completed` 或 `failed` |
| `payload_json` | 结构化载荷 |
| `error` | 解析、校验或调用错误 |
| `created_at` | 创建时间 |

唯一约束为 `(run_id, agent, revision)`。模型回答格式错误、结构校验失败、Reviewer 输出不完整或审查未通过时，也保存失败产物；同阶段修订产生后续版本。`payload_json._telemetry` 保存请求与搜索遥测，`payload_json._cache` 标记已过审阶段复用。

`research_search` 是中控阶段而非模型 Agent：它保存固定选题、实际执行的查询、`W-*` 证据 ID、URL、标题/摘要和搜索遥测。等价失败运行重试时只有选题完全一致且每个保留主题都有安全有效证据，才能复用该产物。

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
| `run_id` | 产生该版本的每日画像或报告运行 |
| `title/statement` | 标题与陈述 |
| `status` | `accepted`、`rejected`、`superseded` |
| `confidence` | 0 到 1 |
| `source_refs_json` | 支撑该版本的 `R-*` 列表 |
| `first_observed/last_observed` | 记录中首次与最近观察日期 |
| `created_by` | `retrospective` 或 `user` |
| `supersedes_id` | 被当前版本替代的旧条目 |
| `created_at/updated_at` | 本地时间戳 |

Retrospective 的候选只有通过 Reviewer 才能为 `accepted`，但只有所属运行标为 `completed` 后才进入历史上下文。每日画像完成审查即可完成运行，不生成 Markdown 日报；周报/月报仍须整份报告成功交付。完成运行与旧版本 `superseded` 在同一事务中生效；每日审查、研究板块或交付失败时，新候选不激活，旧版本仍有效。

失败运行的候选不会在下一轮自动清除，而是和失败 Agent 产物一起保留审计。重试创建新的运行；任何读取有效画像的查询都会排除这些失败运行，因此保留它们不会污染后续报告。

历史上下文是截至报告周期结束日的版本快照：条目 `last_observed` 和产生它的运行 `period_end` 都不得晚于截止日。对当前已为 `superseded` 或 `rejected` 的条目，查询会按替代运行周期或 `profile_feedback.created_at` 回放它在当时是否仍有效；用户后来的修正、认可或否决不会进入过去报告。

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

## 9. 结构校验

程序不在 SQLite `user_version` 或业务表中保存 schema 版本。启动时直接校验当前表集合和字段集合：

- 空数据库按当前最终结构创建；
- 结构完全一致时直接使用；
- 结构不一致时直接报错，不迁移、补表、覆盖、备份或删除。

需要重建时，由用户确认数据无需保留后，在没有报告任务运行时手动删除主库、`-wal`、`-shm`。程序永远不会因数据库问题改写或删除 `Records/`。

## 10. 备份与恢复

- 原始日记与 SQLite 应分别备份；只恢复日记会丢失画像反馈，但不会丢失用户原文。
- 运行中的数据库应使用 SQLite backup API 或同时一致处理主库、WAL、SHM，不能只复制主文件。
- 数据库错误不得触发对 `Records/` 的清理。
- 报告是可重建交付物，画像反馈是不可从原文完全推导的用户决定，恢复时应优先保护后者。
- 已生成的 Markdown 报告包含完整正文、外部链接和 `R-*` 来源索引，阅读不依赖 SQLite；删除数据库不会截断报告文件，但会永久丢失运行审计、缓存、画像版本链和用户反馈。
