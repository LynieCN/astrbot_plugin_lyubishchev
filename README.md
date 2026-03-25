# AstrBot 柳比歇夫时间管理插件

这个插件用于在 AstrBot 中记录、统计和追查个人时间使用情况。

它会把时间记录保存为结构化账本，并在此基础上提供总结、定时推送、长期检索和日常对话调用能力。

## 功能概览

1. 记录时间，支持自然语言、时间段、时长、标签、分类、项目。
2. 支持单条录入和多行批量录入。
3. 查看今天、最近记录、单条详情，支持修改和删除。
4. 生成日报、周报、月报和自定义区间总结。
5. 支持定时总结并主动推送。
6. 支持 embedding + rerank 的长期记录追查。
7. 支持在普通聊天中按需调用时间记录工具。
8. 录入后可生成结合近期聊天和记录的反馈。

## 安装后建议先做的事

先确认插件已经正常加载：

```text
/lyu help
```

然后录入一条最简单的记录：

```text
/lyu note 09:00-10:30 阅读论文 #科研
```

再查看今天的记录：

```text
/lyu today
```

如果这几步都正常，插件就已经可用了。

## 基本使用

### 录入一条记录

```text
/lyu note 09:00-10:30 阅读论文 #科研
```

```text
/lyu note 45分钟 处理报销 #行政
```

```text
/lyu note 昨天 1小时 整理实验数据 #科研
```

### 批量录入

`/lyu note` 支持按行拆分，一行一条：

```text
/lyu note
09:00-10:30 阅读论文 #科研
10:40-11:10 回邮件 #行政
14:00-15:30 开组会 #工作
```

### 自动记录

默认支持以下前缀：

- `记录：`
- `记录 `
- `记时：`
- `记时 `
- `lyu：`
- `lyu:`

例如：

```text
记录：昨天 45分钟 处理报销 #行政
```

如果 `auto_record_require_wake = true`，则仍需满足 AstrBot 的唤醒条件。

## 记录写法

### 常见时间表达

- 时间段：`09:00-10:30`、`09:00~10:30`
- 时长：`45分钟`、`30min`、`2h`、`1.5小时`
- 相对日期：`今天`、`昨天`、`前天`、`明天`
- 绝对日期：`2026-03-24`

### 结构化信息

- 标签：`#科研`、`#学习`
- 分类：`category:学习`、`分类:学习`
- 项目：`project:论文`、`项目:论文`
- 类型：`kind:plan`

### 示例

```text
/lyu note 08:00-09:30 背单词和精读文章 #英语 category:学习
```

```text
/lyu note 14:00-16:00 处理实验数据 #科研 category:研究 project:NiFe
```

```text
/lyu note 20:00-21:30 刷视频 #娱乐
```

## 查看、修改和删除

查看今天的记录：

```text
/lyu today
```

查看最近几条：

```text
/lyu recent
/lyu recent 10
```

查看某条详情：

```text
/lyu show <记录ID前缀>
```

修改某条记录：

```text
/lyu amend <记录ID前缀> <新的内容>
```

删除某条记录：

```text
/lyu delete <记录ID前缀>
```

## 总结与追查

日总结：

```text
/lyu summary day
```

周总结：

```text
/lyu summary week
```

月总结：

```text
/lyu summary month
```

最近 7 天：

```text
/lyu summary custom:7
```

指定日期范围：

```text
/lyu summary range:2026-03-01,2026-03-07
```

长期追查：

```text
/lyu query 我最近时间主要花在哪了
```

```text
/lyu query 帮我找一下上个月和报销有关的记录
```

## 定时总结

先查看已有规则：

```text
/lyu rule list
```

添加一条规则：

```text
/lyu rule add 每日晚报 | 30 22 * * * | day | Asia/Shanghai
```

这条规则表示每天 22:30 推送日总结。

例如每周日晚 21:00 推送周总结：

```text
/lyu rule add 每周周报 | 0 21 * * 0 | week | Asia/Shanghai
```

手动运行某条规则：

```text
/lyu rule run <规则ID前缀>
```

删除某条规则：

```text
/lyu rule delete <规则ID前缀>
```

## 日常对话调用

插件已经向 AstrBot 提供了可调用工具，因此在普通聊天中也可以直接提问，例如：

- 我今天都做了什么？
- 我这周时间主要花在哪了？
- 我最近是不是又在摸鱼？
- 帮我看看最近几条时间记录。

在工具调用可用的情况下，AstrBot 会按需读取插件记录再作答。

## 录入反馈

每次成功录入后，AstrBot 可以结合：

- 当前录入内容
- 最近聊天上下文
- 最近时间记录

生成一段反馈。是否开启由 `record_feedback_enabled` 控制。

## 配置说明

### `default_timezone`

默认时区。影响日期解析、定时规则和总结区间。

中国大陆通常使用：

```text
Asia/Shanghai
```

### `analysis_provider_id`

用于总结、问答和部分分析能力。建议优先配置。

### `embedding_provider_id`

用于长期记忆向量化和语义召回。不配置也能记账，但长期检索能力会弱一些。

### `rerank_provider_id`

用于 `/lyu query` 的结果重排。记录较多时建议配置。

### `record_feedback_provider_id`

录入反馈的兜底模型来源。插件默认会优先尝试使用当前会话里的 AstrBot 主机器人。

### `auto_record_prefixes`

自动记录前缀，一行一个。

### `auto_record_require_wake`

是否要求先唤醒 AstrBot 再进行自动记录。担心误识别时建议保持开启。

### `record_feedback_enabled`

是否在录入成功后生成反馈。

### `record_feedback_max_recent_records`

录入反馈时最多参考多少条最近记录。

### `record_feedback_max_recent_chats`

录入反馈时最多参考多少条最近聊天。

### `record_feedback_prompt_appendix`

给录入反馈额外追加提示要求。

### `summary_with_advice`

总结中是否附带建议。

### `query_answer_with_llm`

`/lyu query` 是否由模型组织最终答案。关闭后会更接近原始检索结果。

### `max_query_candidates`

检索时最多返回多少条候选记录。

### `vector_similarity_threshold`

向量召回阈值。越高越严格，通常保持默认即可。

### `summary_prompt_appendix`

给总结额外追加提示要求。

## 常用命令

### 记录与查看

- `/lyu help`
- `/lyu note <内容>`
- `/lyu today`
- `/lyu recent [数量]`
- `/lyu show <记录ID前缀>`
- `/lyu amend <记录ID前缀> <新内容>`
- `/lyu delete <记录ID前缀>`

### 总结与追查

- `/lyu summary day`
- `/lyu summary week`
- `/lyu summary month`
- `/lyu summary custom:7`
- `/lyu summary range:2026-03-01,2026-03-07`
- `/lyu query <问题或关键词>`

### 定时规则

- `/lyu rule list`
- `/lyu rule add <名称 | cron | day|week|month|custom:7 | timezone可选>`
- `/lyu rule delete <规则ID前缀>`
- `/lyu rule run <规则ID前缀>`

### 其他

- `/lyu status`

## 说明

- 结构化记录是事实来源，统计、检索和总结都以时间记录为准。
- 日常对话反馈和总结会参考聊天上下文，但不会替代原始记录本身。
- 如果准备长期使用，建议尽量稳定使用标签、分类和项目字段，这样后续统计会更清楚。
