# AstrBot 柳比歇夫时间管理插件

这是一个给 AstrBot 使用的柳比歇夫时间管理插件。

它把日常时间记录保存成结构化账本，并在此基础上提供周期总结、长期追查、定时推送和聊天式反馈。你既可以手动记账，也可以在日常对话里直接问机器人“我这周都在做什么”“最近时间花在哪了”，让它按需调用时间记录工具来回答。

## 核心功能

1. 自然语言记录时间，支持时间段、时长、标签、分类、项目等结构化信息。
2. 用 SQLite 保存原始记录，方便长期积累、修改、删除和追查。
3. 支持按天、周、月和自定义区间生成总结。
4. 支持多条定时总结规则，按设定周期主动推送。
5. 支持 embedding + rerank 的长期检索，方便跨周期回看记录。
6. 每次录入后可生成聊天式反馈，支持鼓励、提醒和轻度吐槽。

## Provider 配置

如果想把插件能力用全，建议在插件配置里准备下面几类 Provider：

- `analysis_provider_id`
  用于总结、问答和部分分析逻辑。不填时默认使用当前会话中的聊天模型。
- `embedding_provider_id`
  用于长期记忆向量化和语义召回。
- `rerank_provider_id`
  用于 `/lyu query` 检索结果重排。
- `record_feedback_provider_id`
  用于录入反馈的兜底模型来源。留空时会优先走当前会话中的 AstrBot。

## 快速开始

先看帮助：

```text
/lyu help
```

记下一条时间记录：

```text
/lyu note 09:00-10:30 阅读论文 #科研 category:学习 project:论文
```

查看今天的记录：

```text
/lyu today
```

## 自动记录

除了 `/lyu note`，你也可以直接用配置好的前缀触发自动记录。

默认前缀有这些：

- `记录：`
- `记录 `
- `记时：`
- `记时 `
- `lyu：`
- `lyu:`

示例：

```text
记录：昨天 45分钟 处理报销 #行政
```

如果开启了 `auto_record_require_wake = true`，则这类消息需要先满足 AstrBot 的唤醒条件。

## 记录写法

### 时间表达方式

- 具体时间段：`09:00-10:30`、`09:00~10:30`
- 直接写时长：`45分钟`、`1.5小时`、`2h`、`30min`
- 相对日期：`今天`、`昨天`、`前天`、`明天`
- 绝对日期：`2026-03-24`

### 结构化标记

- 标签：`#科研` `#复盘`
- 分类：`category:学习` 或 `分类:学习`
- 项目：`project:论文` 或 `项目:论文`
- 类型：`kind:plan`，或者直接以 `计划 ` 开头

### 多行批量录入

`/lyu note` 支持换行批量录入，默认一行一条：

```text
/lyu note
09:00-10:30 阅读论文 #科研
10:40-11:10 回邮件 #行政
14:00-15:30 开组会 #工作
```

## 日常对话可调用

这版插件已经给 AstrBot 提供了可调用工具。

所以除了命令，你还可以直接在日常对话里问这类问题：

- 我今天都做了什么？
- 我这周时间主要花在哪了？
- 我最近是不是又摸鱼了？
- 把最近几条时间记录列给我看

在默认工具配置下，AstrBot 会按需调用插件工具来读取时间账本。

## 指令一览

### 记录与查看

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

## 配置项说明

- `default_timezone`
  用于解析相对日期和定时规则时区。
- `auto_record_require_wake`
  控制自动记录是否要求先唤醒机器人。
- `auto_record_prefixes`
  自动记录前缀，一行一个。
- `record_feedback_enabled`
  控制录入后是否生成反馈。
- `record_feedback_max_recent_records`
  反馈时参考的最近记录条数。
- `record_feedback_max_recent_chats`
  反馈时参考的最近聊天条数。
- `record_feedback_prompt_appendix`
  给录入反馈追加自定义要求。
- `summary_with_advice`
  控制周期总结中是否附带建议。
- `query_answer_with_llm`
  控制 `/lyu query` 是否让模型组织最终答案。
- `max_query_candidates`
  控制长期检索候选条数上限。
- `vector_similarity_threshold`
  控制向量召回阈值。
- `summary_prompt_appendix`
  给周期总结追加自定义要求。

## 注意

- 结构化记录是事实层，统计、检索和总结都以时间记录为准。
- 对话反馈和周期总结会参考当前会话上下文，但不会替代原始记录本身。
