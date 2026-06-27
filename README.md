# AgentRecord

简易的 Agent 日记系统。每天的信息保存为独立的 Markdown 文件，AI 可调用本地工具实现查阅、搜索、总结，支持联网搜索。

- **信息隔离**：AI 只能修改 `<summary>` 区域，原始记录流由程序管理
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
```

### 命令

```
╭───────────────────────────── 命令手册 ──────────────────────────────╮
│   /h        → 显示此帮助                                            │
│   /m        → 切换到下一个模型                                       │
│   /v [日期] → 查看历史日记（空=今天, /v help 查看所有用法）              │
│   /r        → 重试今日最后一个未回答的提问（保持原类型）                 │
│   /c        → 清空当前窗口                                           │
│   /d        → 删除今日最后一条记录                                    │
│   /q [问题] → 查阅提问（只读，仅生成一句话轻记录）                      │
│   @[内容]   → 呼叫 AI 解答或执行任务（完整记录回复）                    │
╰─────────────────────────────────────────────────────────────────────╯

[deepseek-v4-pro] >>
```

### 用法示例

```
# 记录日常
[deepseek-v4-pro] >> 今天下午3点开了项目周会

# AI 总结
[deepseek-v4-pro] >> @总结今日内容

# 查阅（轻记录，不写 summary）
[deepseek-v4-pro] >> /q Python 3.13 有什么新特性

# 查看历史
[deepseek-v4-pro] >> /v -1         # 昨天
[deepseek-v4-pro] >> /v 6-25       # 6月25日

# 重试最后一个未回答的提问
[deepseek-v4-pro] >> /r
```
