# AgentRecord

想写什么就写什么——日常、计划、经历、观点、判断、问题或一闪而过的念头。AgentRecord 不要求用户先分类，也不把记录过程变成与 AI 对话。它与写在纸上的核心区别是：每条内容都以统一格式带时间保存，可以通过同样稳定的格式引用过去的日记和报告；当一天、一周、一个月结束后，Agent 在后台整理已经积累的材料，寻找关联、变化、矛盾、盲点和新的探索方向，并交付可以独立阅读的总结或报告。

用户负责自由记录与最终判断，Agent 负责在记录之后收集上下文、展开分析和汇报。分析由 Extractor、Cluster、Explorer、World、Reviewer 和 Report 六类职责明确的 Agent 完成；中控统一提供最小上下文和工具权限，Agent 不能直接修改日记、数据库或报告文件。这个“低摩擦记录 → 可追溯引用 → 可迭代分析 → 周期报告”的循环，就是 AgentRecord 的核心。

项目不提供 `@AI` 即时聊天。AI 的价值集中在记录之后的分析，而不是回答随手可在其他 AI 产品中提出的常识问题。设计与实现文档见 [Docs](./Docs/README.md)。

## 使用示例

### 启动应用

Linux 或其他类 Unix 环境直接运行 Python：

```bash
python main.py
```

也可以通过包入口启动：

```bash
python -m AgentRecord
```

Windows 用户运行打包后的程序：

```powershell
AgentRecord.exe
```

Windows 版的 `AgentRecord.exe` 和 `config.yaml` 应放在同一目录。程序默认进入记录模式，输入提示符固定为 `>>`。

### 记录模式

普通文字按回车后立即写入当天的 Markdown 日记，不等待模型：

```text
>> 今天重新整理了报告模式的交互。
>> 这里可以记录计划、判断、问题或任何临时想法。
```

记录模式命令：

```text
/h                         显示记录模式帮助
/mode                      切换到报告模式
/v [日期]                  查看日记；/v help 查看日期格式
/ref [类型] [筛选词]       引用日记或报告；diary/daily/weekly/monthly
/d                         删除今日最后一条记录
/c                         清空终端显示
```

除命令外的输入都会作为普通记录保存，包括以 `@` 开头的内容。日期支持 `-1`、`today`、`昨天`、`MM-DD` 和 `YYYY-MM-DD` 等格式。

### 报告模式

执行 `/mode` 后进入报告模式，此时 `/h` 只显示报告相关命令：

```text
/h                         显示报告模式帮助
/mode                      返回记录模式
/s [日期]                  手动生成日记顶部总结；空为今天
/a daily [日期]            手动生成分析日报
/a weekly [日期]           手动生成日期所在自然周的周报
/a monthly [日期]          手动生成日期所在自然月的月报
/m                         永久切换总结和报告统一使用的模型
```

报告模式不接收普通文字，避免把报告操作或临时输入误写入日记。月报可以直接指定月份，例如 `/a monthly 2026-06`。同一周期再次生成手动报告时，程序会先确认是否覆盖原手动报告。手动报告启动后会转入后台并自动返回记录模式；当前窗口可继续记录，完成后会显示通知。任务完成前不要关闭该窗口。

### 引用已有材料

例如引用一份周报并记录由它继续展开的新想法：

```text
>> /ref weekly
选择编号 [空=取消] >> 1
关联记录 [可留空] >> 这个判断可能还能联系到最近的产品决策。
```

日记中保存为带相对路径的标准记录：

```markdown
**14:32 [引用]:** [自动分析周报 | 2026-07-06 至 2026-07-12](<../AnalysisReports/Weekly/2026-07-06_to_2026-07-12_auto.md>)

这个判断可能还能联系到最近的产品决策。
```

`/ref` 默认展示最近 20 个来源，也可以用日期筛选，例如 `/ref monthly 2026-06`。来源文件不会被修改。

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
  weekly_report: true
  monthly_report: true
```

- `current_model` 是手动总结、手动报告和自动任务统一使用的模型；报告模式执行 `/m` 会永久更新该配置。
- `diary_dir`、`analysis_dir` 和 `log_dir` 的相对路径以 `config.yaml` 所在目录为基准。
- 可以分别关闭三种自动任务，或用 `automation.enabled: false` 关闭整个自动流程。
- 第三方搜索仅在所选模型没有原生搜索能力时使用。
- 不要把真实 API 密钥提交到版本库或写入测试输出。

### 安装系统后台任务

后台任务只需安装一次。安装后，无论 AgentRecord 终端是否打开，操作系统都会在登录或重启后以及每小时自动检查到期任务。

正常进入交互界面时，程序会显示系统后台任务是否已经完整安装。该提示只检查状态；未安装时仍需执行下面的安装命令。安装完成后可以关闭 AgentRecord 窗口，自动任务不依赖交互进程存活。

Linux 或其他类 Unix 环境：

```bash
python main.py --install-automation
```

Windows 打包版本：

```powershell
AgentRecord.exe --install-automation
```

重新执行安装命令会更新已有系统任务。卸载命令分别为：

```bash
python main.py --uninstall-automation
```

```powershell
AgentRecord.exe --uninstall-automation
```

Windows 后台任务调用 `AgentRecord.exe --run-automation`。该入口会隐藏控制台窗口，正常运行 `AgentRecord.exe` 时仍会显示记录终端。

### 构建 Windows EXE

在 Windows 构建环境安装 PyInstaller 后执行：

```powershell
pyinstaller --onefile --name AgentRecord main.py
Copy-Item config.yaml dist\config.yaml
```

最终分发 `dist` 目录中的 `AgentRecord.exe` 和 `config.yaml`。后台任务会记录 EXE 的绝对路径，因此移动程序目录后应重新执行 `AgentRecord.exe --install-automation`。

## 文件布局

```text
main.py                         极薄的脚本与 PyInstaller 入口
AgentRecord/
  settings.py                  配置、目录和模型选择
  logging_config.py            标准日志与按大小轮转
  journal.py                   原始日记唯一读写边界
  ai_client.py                 OpenAI 兼容请求和授权工具循环
  cli/
    entry.py                   进程参数和 Windows 后台入口
    terminal.py                跨平台终端输入和 Rich 展示
    commands.py                记录/报告命令
    report_jobs.py             手动报告后台线程
    app.py                     交互主循环
  analysis/
    context.py                 周期输入、引用和历史上下文
    orchestrator.py            总结与多 Agent 报告中控
    store.py                   SQLite 版本化节点和关系
    automation.py              自动任务、锁和系统任务安装
  agents/                      六个独立 Agent 及共享协议

Docs/                          设计基线与实现文档

Records/
  YYYY-MM-DD.md
AnalysisReports/
  .analysis.sqlite3
  Daily/YYYY-MM-DD_manual.md
  Weekly/YYYY-MM-DD_to_YYYY-MM-DD_manual.md
  Weekly/YYYY-MM-DD_to_YYYY-MM-DD_auto.md
  Monthly/YYYY-MM_manual.md
  Monthly/YYYY-MM_auto.md
  .automation-state.json
Log/
  AgentRecord.log              当前运行日志；归档后缀为 .1 和 .2
```

## 测试

```bash
python -m unittest discover -s tests -v
```
