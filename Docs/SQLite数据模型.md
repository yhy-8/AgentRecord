# SQLite 数据模型

分析数据库默认位于 `AnalysisReports/.analysis.sqlite3`。它是可追溯、可迭代的派生知识层，不替代 Markdown 原始记录，也不能成为 Agent 绕过 `journal.py` 修改用户数据的通道。

## 1. 连接与事务

每个存储操作创建独立连接，并启用：

- `PRAGMA foreign_keys = ON`；
- `PRAGMA busy_timeout = 10000`；
- `PRAGMA journal_mode = WAL`；
- 写操作使用 `BEGIN IMMEDIATE`。

事务只包围实际 SQL 读写，不跨越模型或网络调用。这样前台记录完全不依赖数据库，多个分析任务的写入也只发生短暂串行竞争。

数据库版本记录在 `PRAGMA user_version`，当前模式版本为 2。程序遇到高于自身支持的版本时必须停止，不能猜测性降级或重建。v1 升级前使用 SQLite backup API 生成 `.analysis.sqlite3.v1.bak` 一致备份，已有备份不覆盖。

## 2. analysis_runs

一行代表一次日报、周报或月报运行。

| 字段 | 含义 |
|---|---|
| `id` | UUID 十六进制运行 ID |
| `kind` | `daily`、`weekly` 或 `monthly` |
| `period_start/end` | 闭区间日期 |
| `origin` | `manual` 或 `auto` |
| `model_name` | 本次统一模型名称 |
| `status` | `running`、`completed`、`failed` |
| `parent_run_id` | 同类型、周期和来源方式的上一成功运行 |
| `input_hash` | 输入快照 SHA-256 |
| `report_path` | 成功交付文件路径 |
| `error` | 失败原因 |
| `created_at/completed_at` | 本地时间戳 |

运行先以 `running` 插入。报告文件原子替换成功后才能转为 `completed`；任何未处理异常转为 `failed`。

## 3. analysis_sources

该表把模型使用的 `R-*` 映射回 Markdown 位置。联合主键是 `(run_id, source_id)`。

保存内容包括相对路径、日期、时间、记录序号、说话者、标签、正文哈希和最多 500 字符摘录。哈希用于判断重跑时来源是否未变化；摘录用于诊断和有限上下文，不代表复制接管完整日记。

## 4. agent_artifacts

该表保存每个 Agent 每次调用或校验的结构化产物。`(run_id, agent, revision)` 唯一，重复调用自动增加 revision。

状态通常是 `completed` 或 `failed`。模型调用、JSON 解析、权限校验或结构校验失败时仍写入失败产物，使排障可以看到失败发生在哪一层。产物可能包含用户派生内容，因此数据库和报告目录都属于本地数据，不进入版本库。

## 5. knowledge_nodes

节点类型包括：

- `evidence`：可直接追溯到原始记录的结构化证据；
- `theme`：多个证据形成的主题和时间轨迹；
- `hypothesis`：尚待判断的解释；
- `research`：外部核查结果；
- `insight`：通过关联、演化、矛盾或盲点得到的候选洞见。

节点状态机：

```text
candidate → accepted
candidate → rejected
candidate → candidate
accepted  → superseded   # 仅在替代版本被接受时
```

节点保存标题、正文、置信度、创建 Agent、原始来源 JSON、类型专属元数据、版本号和 `supersedes_id`。修改通过插入新节点表达，禁止原地改写旧判断。

## 6. knowledge_edges

关系连接两个持久节点，保存关系类型、状态、权重、置信度、理由、创建 Agent 和版本信息。当前关系语义包括归属、支持、挑战、矛盾、演化、分叉和合并。

Agent 输出可以使用临时节点 ID；中控先插入节点获得持久 ID，再映射关系端点。数据库外键保证不存在悬空关系。候选审查结束后，两个端点都有效的关系才接受，其余拒绝。

## 7. 来源继承

Extractor 直接写 `R-*`。Cluster、Explorer 和 World 主要看到知识节点 ID，因此中控在保存前执行来源继承：

1. 保留已允许的原始来源 ID；
2. 将可见知识节点 ID 展开为该节点的 `source_refs`；
3. 从关系端点、`evidence_for`、`evidence_against` 和 `target_id` 补充来源；
4. 保持顺序去重；
5. 未知引用留给校验器拒绝。

最终进入 `knowledge_nodes.source_refs_json` 的只能是本次中控允许的原始来源。

## 8. 版本与历史读取

带 `supersedes_id` 的新节点版本号是旧节点版本加一。只有 Reviewer 接受新节点时，旧的 accepted 节点才标为 superseded。

历史读取区分本次运行、父运行和其他近期成功运行。报告只接收当前已接受节点，以及来源哈希仍未变化的可复用父运行节点。拒绝节点和失败产物保留用于审计，但不会进入报告上下文。

## 9. node_feedback

该表记录用户对已接受 Theme、Hypothesis 和 Insight 的 `accept`、`reject` 或 `correct` 决定，保留原节点、替代节点和时间。认可与修正插入 `created_by=user` 的 accepted 新节点并 supersede 旧节点；否决将旧节点标记为 rejected。因此用户决定会改变未来历史上下文，但不改写任何已有 Markdown 报告。

## 10. 备份与迁移规则

- 原始 Markdown 和 SQLite 必须分别备份；仅从 Markdown 重建会丢失用户未来的接受、否决和修订判断。
- 迁移前保留数据库、`-wal` 和 `-shm` 的一致副本，不能只复制主文件而忽略正在提交的 WAL。
- 模式升级必须按 `user_version` 逐级迁移，并验证旧数据、重复运行和失败恢复。
- 数据库损坏时不得自动删除并创建空库；应停止分析、保留现场并通过日志报告。
- 任何迁移失败都不能影响 `Records/` 中的原始记录。
