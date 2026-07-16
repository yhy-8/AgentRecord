# AgentRecord

AgentRecord 是一个本地优先的个人记录、整理回顾与领域研究系统。用户每天自由记录事实、行动、观点、理念、理想、问题和兴趣，程序不要求预先分类，也不把记录过程变成与 AI 对话。系统每天收集“综合新闻雷达 + 记录驱动信息”，每周、每月生成一份由两个独立板块组成的报告：

1. **整理与回顾**：回顾做过什么，并根据可追溯记录整理观点、理念、理想与行为模式的形成、延续和变化。
2. **领域探索与研究**：从记录或综合新闻雷达选择少量公开领域问题，联网查证、分析和推演，以拓宽视野。

两个板块由不同 Agent 生成，再分别通过审查。系统不生成分析日报，也不提供行为教练：AI 负责整理事实、呈现依据、研究领域和标明推断，人负责价值判断与下一步行动。

原始日记是唯一事实源；SQLite 只保存运行审计、来源索引和跨周期人物画像。报告可以重新生成、覆盖且不是稳定唯一对象，因此新记录只能引用日记，不能引用报告。既有 Markdown 的标准记录格式和记录流保持不变。

更完整的产品与实现约束见 [Docs](./Docs/README.md)。

## 使用

### 启动

Linux 或其他类 Unix 环境：

```bash
python main.py
```

也可以通过包入口启动：

```bash
python -m AgentRecord
```

Windows 打包版本：

```powershell
AgentRecord.exe
```

Windows 版 `AgentRecord.exe` 和 `config.yaml` 应放在同一目录。程序默认进入记录模式，提示符为 `>>`。

### 记录模式

普通文字按回车后立即写入当天的 Markdown 日记，不等待模型：

```text
>> 今天重新梳理了产品定位。
>> 我认为事实判断与行动决定应该分开。
```

记录模式命令：

```text
/h                         显示记录模式帮助
/mode                      切换到报告模式
/status                    查看自动任务安装、进度、延后与失败状态
/v [日期]                  查看日记；/v help 查看日期格式
/ref [日期]                按日期选择并引用日记
/d                         删除今日最后一条记录
/c                         清空终端显示
```

除命令外的输入都会作为普通记录保存，包括以 `@` 开头的内容。日期支持 `-1`、`today`、`昨天`、`MM-DD` 和 `YYYY-MM-DD` 等格式。

`/ref` 不再选择来源类型。空参数显示最近日记；可以给出 `YYYY-MM` 查看某月，或使用与 `/v` 相同的单日日期写法。

### 报告模式

执行 `/mode` 后进入报告模式：

```text
/h                         显示报告模式帮助
/mode                      返回记录模式
/status                    查看自动任务安装、进度、延后与失败状态
/s [日期]                  手动生成日记顶部总结；空为今天
/a weekly [日期]           手动生成日期所在自然周的周报
/a monthly [日期]          手动生成日期所在自然月的月报
/retry                     独立后台重试全部失败自动任务
/f                         认可、否决或修正最近的人物画像条目
/m                         永久切换总结和报告使用的模型
```

`/a` 默认等同于 `/a weekly`。周报和月报未给日期时，程序按日记文件列出最近自然周期，并显示闭合状态、记录天数和手动/自动报告状态。手动报告在当前交互进程的工作线程中运行；窗口可继续记录，但任务完成前不要关闭窗口。

`/retry` 不要求逐项选择。它一次取得当前全部失败自动任务，在一个与当前终端分离的独立进程中依次重试，然后立即返回记录模式。重试生成的周报或月报仍使用自动报告路径，SQLite 中 `origin=auto`、`trigger=retry`，不会被伪装成手动报告。

`/f` 只影响后续报告使用的人物画像，不改写已有 Markdown 报告。

### 引用日记

```text
>> /ref 2026-07
选择编号 [空=取消] >> 1
关联记录 [可留空] >> 这条旧判断在本周发生了变化。
```

日记中保存为标准记录：

```markdown
**14:32 [引用]:** [日记 | 2026-07-06](<2026-07-06.md>)

这条旧判断在本周发生了变化。
```

引用流程、相对路径和可选关联记录保持不变；来源选择只包含日记。历史中已经存在的报告引用不会被改写，但新分析不会把报告引用内容作为来源载入。

## 配置

编辑应用目录中的 `config.yaml`：

```yaml
models:
  - name: deepseek-v4-pro
    model_id: deepseek-v4-pro
    api_url: https://api.deepseek.com/chat/completions
    api_key: ""
    search: false

current_model: deepseek-v4-pro

third_search:
  enabled: false
  api_url: https://api.bocha.cn/v1/web-search
  api_key: ""
  count: 25
  timeout: 30
  max_rounds: 3

diary_dir: ./Records
analysis_dir: ./AnalysisReports
log_dir: ./Log

automation:
  enabled: true
  daily_summary: true
  daily_information: true
  daily_information_time: "08:05"
  weekly_report: true
  monthly_report: true
```

- `daily_summary` 是日记顶部摘要，不是分析日报。
- `daily_information` 生成“高价值综合新闻 + 本周记录定向信息”简报，保存在 `AnalysisReports/Information/YYYY-MM-DD.md`，作为周报、月报研究选题的线索。选题会对照本自然周此前的简报和定向查询去重；只有出现实质新进展时才继续追踪同一主题，没有新角度时定向查询可以为零。没有可用联网能力时任务失败，不生成伪联网简报。
- `current_model` 用于手动总结、手动报告和自动任务；报告模式 `/m` 会永久更新该配置。
- 相对目录以 `config.yaml` 所在目录为基准。
- 第三方搜索只在所选模型没有原生搜索能力时使用。
- 不要把真实 API 密钥提交到版本库或写入测试输出。

## 自动任务与故障恢复

系统后台任务只需安装一次：

```bash
python main.py --install-automation
```

Windows 打包版本：

```powershell
AgentRecord.exe --install-automation
```

安装后，操作系统会在登录/重启时以及每分钟启动一次短进程检查。检查不是每分钟都调用模型：只有任务到期、没有成功进度且当前没有失败记录时才执行实际工作。任务真正失败后不会被分钟检查无限重跑，需在报告模式执行 `/retry`；锁屏延后不算失败，解锁后仍会自动补做。

检查同时核对实际产物，而不只相信状态文件：昨日总结仍为空或为默认占位时会补做；最近已闭合自然周、自然月有记录但对应自动报告文件缺失时也会重新生成。`/status` 会显示当前正在执行的信息收集、周报或月报阶段，首次同时补做多项任务时可能持续数分钟。

自动化专门处理以下意外情况：

- **锁屏**：任务开始前和 Agent 调用之间都会检测会话状态；锁屏时不发起模型或联网请求，记录为延后。解锁后在下一分钟检查中继续，通常不超过一分钟。
- **休眠**：休眠期间没有任务运行；唤醒后由下一分钟检查补做。
- **重启或进程崩溃**：自动任务使用操作系统内核锁。进程退出后锁由内核释放，磁盘上的 `.automation.lock` 只是锁载体，不会再因遗留 PID 或超时判断永久卡住。
- **短暂 DNS、连接或超时错误**：单次请求按 1 秒、2 秒间隔最多重试三次；任务最终失败时保留失败状态，不推进成功位置。
- **手动补跑**：在报告模式执行 `/retry`，全量重试当前失败的日总结、信息简报、自动周报和自动月报。

当前实现通过每分钟检查实现“解锁后尽快运行”，不是常驻桌面事件监听，因此语义是解锁后 **下一分钟内**，而非同一毫秒。

查看状态：

```text
/status
```

卸载任务：

```bash
python main.py --uninstall-automation
```

```powershell
AgentRecord.exe --uninstall-automation
```

移动程序目录后应重新安装后台任务，因为系统任务保存的是入口绝对路径。

## 数据与文件布局

```text
main.py
AgentRecord/
  journal.py                   Markdown 日记唯一写边界
  ai_client.py                 模型请求、联网工具与短暂网络重试
  cli/                         双模式终端和手动报告工作线程
  analysis/
    context.py                 周期记录、日记引用与信息简报上下文
    information.py             每日综合新闻和记录驱动信息收集
    orchestrator.py            双板块报告中控
    store.py                   SQLite v3 运行、来源与人物画像
    automation.py              到期任务、内核锁、重试和系统任务
    session_state.py           Windows/Linux 会话锁定检测
  agents/
    retrospective.py           整理回顾与画像候选
    research_planner.py        研究选题与查询去隐私
    researcher.py              联网领域研究
    reviewer.py                两个板块的独立质量审查

Records/YYYY-MM-DD.md
AnalysisReports/
  .analysis.sqlite3
  .automation-state.json
  .automation.lock
  Information/YYYY-MM-DD.md
  Weekly/YYYY-MM-DD_to_YYYY-MM-DD_manual.md
  Weekly/YYYY-MM-DD_to_YYYY-MM-DD_auto.md
  Monthly/YYYY-MM_manual.md
  Monthly/YYYY-MM_auto.md
Log/AgentRecord.log
```

旧版 SQLite 会先备份为 `.analysis.sqlite3.v<旧版本>.legacy.bak`，再建立 v3 数据库。原始 Markdown 日记及其标准格式不会迁移或删除。

## 测试

```bash
python -m unittest discover -s tests -v
```

## 构建 Windows EXE

```powershell
pyinstaller --onefile --name AgentRecord main.py
Copy-Item config.yaml dist\config.yaml
```

最终分发 `dist` 中的 `AgentRecord.exe` 和 `config.yaml`。后台 `--run-automation` 与 `--retry-automation` 入口会隐藏控制台，普通交互入口仍显示终端。
