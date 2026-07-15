# AgentRecord

想写什么就写什么——日常、计划、经历、观点、判断、问题或一闪而过的念头。AgentRecord 不要求用户先分类，也不把记录过程变成与 AI 对话。它与写在纸上的核心区别是：每条内容都以统一格式带时间保存，可以通过同样稳定的格式引用过去的日记和报告；当一天、一周、一个月结束后，Agent 在后台整理已经积累的材料，寻找关联、变化、矛盾、盲点和新的探索方向，并交付可以独立阅读的总结或报告。

用户负责自由记录与最终判断，Agent 负责在记录之后收集上下文、展开分析和汇报。这个“低摩擦记录 → 可追溯引用 → 周期性探索分析”的循环，就是 AgentRecord 的核心。

项目不提供 `@AI` 即时聊天。AI 的价值集中在记录之后的分析，而不是回答随手可在其他 AI 产品中提出的常识问题。完整的数据边界和架构基线见 [DESIGN.md](./DESIGN.md)。

## 使用示例

### 启动应用

Linux 或其他类 Unix 环境直接运行 Python：

```bash
python main.py
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

报告模式不接收普通文字，避免把报告操作或临时输入误写入日记。月报可以直接指定月份，例如 `/a monthly 2026-06`。同一周期再次生成手动报告时，程序会先确认是否覆盖原手动报告。

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

automation:
  enabled: true
  daily_summary: true
  weekly_report: true
  monthly_report: true
```

- `current_model` 是手动总结、手动报告和自动任务统一使用的模型；报告模式执行 `/m` 会永久更新该配置。
- `diary_dir` 和 `analysis_dir` 的相对路径以 `config.yaml` 所在目录为基准。
- 可以分别关闭三种自动任务，或用 `automation.enabled: false` 关闭整个自动流程。
- 第三方搜索仅在所选模型没有原生搜索能力时使用。
- 不要把真实 API 密钥提交到版本库或写入测试输出。

### 安装系统后台任务

后台任务只需安装一次。安装后，无论 AgentRecord 终端是否打开，操作系统都会在登录或重启后以及每小时自动检查到期任务。

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

## 工作方式

```text
自由输入
  └─ 立即成为带日期和时间的 Markdown 原始记录
       ├─ /ref 显式引用过去的日记或报告
       ├─ 自然日闭合：生成前一日总结
       ├─ 自然周闭合：生成上一完整周的分析周报
       └─ 自然月闭合：生成上一完整月的分析月报
```

- 普通输入立即落盘；模型、网络或自动任务失败不会影响记录。
- 记录日期和时间以最后按下回车、提交完整内容的时刻为准。一次提交只捕获一个时间，跨午夜输入不会出现文件日期、文件头和记录时间不一致。
- 总结只更新日记顶部 `<summary>`，不改写原始记录流。
- 分析报告独立保存在 `AnalysisReports/`，用户可以直接在文件夹中阅读。
- 引用带来源类型、周期、相对链接和引用时刻，来源文件保持不变。
- 日报只作为手动下钻能力；自动流程默认生成日总结、周报和月报。
- 手动与自动报告分开保存，同一周期各自只保留一份。

## 后台任务细节

所有周期使用运行机器的本地日期和时间。

| 任务 | 周期定义 | 最早生成时间 | 产物 |
|---|---|---|---|
| 日总结 | 本地时间 00:00–23:59 的自然日 | 次日 00:00 以后 | 原日记顶部 `<summary>` |
| 周报 | 周一至周日的完整自然周 | 下周一 00:00 以后 | `Weekly/周一_to_周日_auto.md` |
| 月报 | 每月 1 日至最后一日的完整自然月 | 下月 1 日 00:00 以后 | `Monthly/YYYY-MM_auto.md` |

安装后台任务后：

- Windows 使用当前用户的任务计划程序：用户登录时立即检查，此后每小时第 5 分钟检查。
- 类 Unix 使用当前用户的 cron：系统重启时立即检查，此后每小时第 5 分钟检查。
- 每次检查都会启动独立进程，完成后退出，不需要长期维持 Python 常驻进程。
- 跨进程锁会阻止两个慢任务同时写状态或报告。
- 失败原因保存在 `AnalysisReports/.automation-state.json`，未完成任务会在后续检查中重试。

任务按“日总结 → 周报 → 月报”顺序检查，但失败彼此隔离。首次启用只处理每类任务最近一个已闭合周期，避免突然扫描全部历史并产生大量模型调用；形成状态后，再连续补做程序关闭或机器关机期间遗漏的周期。没有日记的周期只推进检查位置，不生成空报告。

自动任务使用 `_auto.md` 固定路径，手动任务使用 `_manual.md` 固定路径，因此两者互不覆盖。月报会综合当月原始记录、显式引用、月前 30 天的日记总结，以及与当月相交的已有周报。

未来增加每日信息获取、分析和整合 Agent 时，仍应接入同一个一次性后台调度入口：系统按时唤醒，完成到期工作并保存进度，然后退出。这样后台分析的模型或网络失败不会影响用户打开应用记录内容。

## 文件布局

```text
main.py        唯一入口、终端输入和命令映射
settings.py    配置、目录和模型选择
journal.py     原始日记读写、日期解析和引用
ai_client.py   OpenAI 兼容请求、只读工具循环和联网搜索
analysis.py    总结、报告和自动任务；项目的分析编排核心

Records/
  YYYY-MM-DD.md
AnalysisReports/
  Daily/YYYY-MM-DD_manual.md
  Weekly/YYYY-MM-DD_to_YYYY-MM-DD_manual.md
  Weekly/YYYY-MM-DD_to_YYYY-MM-DD_auto.md
  Monthly/YYYY-MM_manual.md
  Monthly/YYYY-MM_auto.md
  .automation-state.json
```

## 测试

```bash
python -m unittest discover -s tests -v
```
