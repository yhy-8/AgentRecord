# AgentRecord

AgentRecord 是一个本地优先的个人记录、整理回顾与领域研究系统。用户每天自由记录事实、行动、观点、理念、理想、问题和兴趣，程序不要求预先分类，也不把记录过程变成与 AI 对话。

系统自动生成每日信息简报、自然周报和自然月报。周报、月报包含两个独立板块：

1. **整理与回顾**：根据可追溯记录整理经历、观点、理念、理想和行为模式。
2. **领域探索与研究**：从记录或信息线索中选择少量公开问题，联网查证、分析和推演。

原始 Markdown 日记是唯一事实源。SQLite 只保存运行审计、来源索引、Agent 遥测、阶段缓存和跨周期人物画像。更完整的产品与实现约束见 [Docs](./Docs/README.md)。

## 启动

Linux 或其他类 Unix 环境：

```bash
python main.py
```

也可以通过包入口启动：

```bash
python -m AgentRecord
```

Windows 打包版：

```powershell
AgentRecord.exe
```

Windows 版 `AgentRecord.exe` 和 `config.yaml` 应放在同一目录。

## 记录模式

启动后默认进入记录模式。普通文字按回车后立即写入当天日记，不等待模型。

```text
/h                         显示记录模式帮助
/mode                      切换到报告模式
/v [日期]                  查看日记
/ref [日期]                选择并引用日记
/d                         删除今日最后一条记录
/c                         清空终端显示
```

日期支持 `-1`、`today`、`昨天`、`MM-DD` 和 `YYYY-MM-DD` 等写法。新记录只能引用日记，不能引用报告。

## 报告模式

执行 `/mode` 后进入报告模式：

```text
/h                         显示报告模式帮助
/mode                      返回记录模式
/status                    查看自动任务产物、进度与失败状态
/s [日期]                  手动生成日记总结
/a weekly [日期]           手动生成自然周报
/a monthly [日期]          手动生成自然月报
/retry                     后台重试全部失败自动任务
/f                         认可、否决或修正人物画像
/m                         切换总结和报告使用的模型
```

`/a` 默认等同于 `/a weekly`。手动报告在后台工作线程中生成；当前窗口可继续记录，但任务完成前不要关闭窗口。`/f` 只影响以后的分析，不改写已有报告。

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

- `current_model` 用于手动总结、手动报告和自动任务。
- 第三方搜索只在所选模型没有原生搜索能力时使用。
- 相对目录以 `config.yaml` 所在目录为基准。

## 自动任务

安装（可重复执行）：

```bash
python main.py --install-automation
```

```powershell
AgentRecord.exe --install-automation
```

查看状态：启动程序，用 `/mode` 进入报告模式，然后执行：

```text
/status
```

卸载：

```bash
python main.py --uninstall-automation
```

```powershell
AgentRecord.exe --uninstall-automation
```

移动程序目录或更换 Python 环境后，重新执行安装命令。

## 数据与文件

```text
Records/YYYY-MM-DD.md
AnalysisReports/
  .analysis.sqlite3
  .automation-state.json
  Information/YYYY-MM-DD.md
  Weekly/YYYY-MM-DD_to_YYYY-MM-DD_manual.md
  Weekly/YYYY-MM-DD_to_YYYY-MM-DD_auto.md
  Monthly/YYYY-MM_manual.md
  Monthly/YYYY-MM_auto.md
Log/AgentRecord.log
```

`.analysis.sqlite3` 不记录 schema 版本，也不提供数据库迁移或旧结构兼容。数据库不存在时会按当前结构自动创建；结构不符合时程序拒绝使用，不会自动改写或删除。

## 构建 Windows EXE

GitHub Actions 在每次 `push` 时自动构建 Windows 产物，也支持手动触发。

```powershell
pyinstaller --onefile --name AgentRecord main.py
Copy-Item config.yaml dist\config.yaml
```

最终分发 `dist` 中的 `AgentRecord.exe` 和 `config.yaml`。
