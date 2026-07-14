# AgentRecord

本地 Agent 日记与个人思维分析系统。每天的信息继续保存为独立的 Markdown 文件；AI 可以即时回答问题，也可以自动生成日记总结、分析日报和分析周报。

- **原始日记兼容**：每天一个 Markdown 文件，现有文件及内容格式保持不变
- **信息隔离**：即时问答 AI 只读日记；总结由程序写入 `<summary>`，分析报告保存到独立目录
- **自动分析**：跨过零点后总结前一天并生成日报，每周生成上一完整自然周的周报
- **多模型**：支持 OpenAI 兼容接口和 Gemini 接口，`/m` 一键切换
- **联网搜索**：模型自带搜索优先，否则可配置第三方搜索（博查 AI），支持多轮检索

### 配置

```yaml
# config.yaml
models:                   # 模型列表（支持 openai / gemini 类型）
  - name: deepseek-v4-pro
    type: openai
    search: false         # 模型是否自带搜索
    ...

third_search:             # 第三方搜索（仅当模型无原生搜索时生效）
  enabled: false
  api_url: "https://api.bocha.cn/v1/web-search"
  api_key: ""
  count: 25
  max_rounds: 3           # 最大搜索轮数

diary_dir: "./Records"    # 日记存储目录
analysis_dir: "./AnalysisReports"

automation:
  enabled: true
  model: deepseek-v4-pro
  daily_summary: true
  daily_report: true
  weekly_report: true
```

### 命令

```
╭───────────────────────────── 命令手册 ──────────────────────────────╮
│   /h        → 显示此帮助                                            │
│   /m        → 切换到下一个模型                                       │
│   /v [日期] → 查看历史日记（空=今天, /v help 查看所有用法）              │
│   /s [日期] → 生成指定日记顶部总结（空=今天）                            │
│   /a [类型] [日期] → 生成分析报告（daily/weekly，默认 daily）            │
│   /r        → 重试今日最后一个未回答的 @AI 提问                         │
│   /c        → 清空当前窗口                                           │
│   /d        → 删除今日最后一条记录                                    │
│   @[内容]   → 呼叫 AI 解答或执行任务（完整记录回复）                    │
╰─────────────────────────────────────────────────────────────────────╯

[deepseek-v4-pro] >>
```

### 用法示例

```
# 记录日常
[deepseek-v4-pro] >> 今天下午3点开了项目周会

# 咨询 AI（保持现有即时问答方式）
[deepseek-v4-pro] >> @分析这个想法可能存在的盲点

# 手动生成日记顶部总结
[deepseek-v4-pro] >> /s             # 今天
[deepseek-v4-pro] >> /s -1          # 昨天

# 手动生成独立分析报告
[deepseek-v4-pro] >> /a daily -1    # 昨日分析日报
[deepseek-v4-pro] >> /a weekly      # 当前日期所在周的分析周报

# 查看历史
[deepseek-v4-pro] >> /v -1         # 昨天
[deepseek-v4-pro] >> /v 6-25       # 6月25日

# 重试最后一个未回答的提问
[deepseek-v4-pro] >> /r
```

分析报告默认保存为：

```text
AnalysisReports/
  Daily/YYYY-MM-DD.md
  Weekly/YYYY-MM-DD_to_YYYY-MM-DD.md
```

程序启动后会在后台检查自动任务。首次启用只处理昨天，之后若程序中断数日，会从上次成功日期继续补做；自动任务失败不会影响原始日记写入。
