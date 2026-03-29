# AstrBot 柳比歇夫时间管理插件

这个插件用来记录、查看、总结和追查个人时间使用情况。

现在的录入入口只保留一个：

```text
/ta <内容>
```

`/t` 只负责查看、修改、总结、查询和计时。  
`/tr` 只负责定时总结规则。

## 指令总览

```text
/ta <内容>

/t hp
/t ls [数量|日期|周期|记录ID前缀]
/t ed <记录ID前缀> <新内容>
/t dl <记录ID前缀>
/t ud
/t on <事项>
/t of
/t sm <周期>
/t qy <问题或关键词>
/t st

/tr ls
/tr ad <名称> <自然语言时间>
/tr ad <名称 | cron | 周期 | timezone>
/tr dl <规则ID前缀>
/tr rn <规则ID前缀>
```

## 新增记录

只用 `/ta`。

```text
/ta 09:00-10:30 阅读论文 #科研
/ta 45分钟 处理报销 #行政
/ta 昨天 1.5小时 写周报 category:总结
/ta 2026-03-24 20:00-21:30 背单词 #英语 category:学习
```

批量录入也走 `/ta`：

```text
/ta
09:00-10:30 阅读论文 #科研
10:40-11:10 回邮件 #行政
14:00-15:30 开组会 #工作
```

也支持分号分隔：

```text
/ta 09:00-09:30 站会 #工作；09:30-11:00 写代码 #开发；11:00-11:20 回消息 #行政
```

## 查看与修改

看今天：

```text
/t ls
```

看最近 10 条：

```text
/t ls 10
```

看某一天或某个周期：

```text
/t ls 2026-03-24
/t ls 昨天
/t ls 本周
/t ls 最近7天
/t ls 3.1-3.7
/t ls range:2026-03-01,2026-03-07
```

看单条详情：

```text
/t ls 8f3a
```

修改记录：

```text
/t ed 8f3a 09:00-10:40 阅读论文 #科研 category:学习
/t ed 8f3a 昨天 1小时 处理报销 #行政
```

删除记录：

```text
/t dl 8f3a
```

撤销最近一条有效记录：

```text
/t ud
```

## 计时

开始计时：

```text
/t on 阅读论文
/t on 写代码 #开发 project:NiFe
```

结束计时并自动记账：

```text
/t of
```

典型流程：

```text
/t on 整理实验数据 #科研
/t of
```

## 总结与追查

周期总结：

```text
/t sm day
/t sm 今天
/t sm 本周
/t sm 上周
/t sm 本月
/t sm 上月
/t sm 最近7天
/t sm 14天
/t sm 3.1-3.7
/t sm range:2026-03-01,2026-03-07
```

说明：

- 总结默认只统计实际记录的总时长。
- 如果区间里有 `kind:plan`，会单独提示计划条数和计划时长。

长期记忆追查：

```text
/t qy 我最近时间主要花在哪些事上？
/t qy 帮我找一下上个月和报销有关的记录
/t qy 这周我在 NiFe 项目上花了多少时间？
/t qy 最近有没有连续几天都在写代码？
```

## 定时规则

查看已有规则：

```text
/tr ls
```

自然语言添加规则：

```text
/tr ad 日报 每天22点
/tr ad 日报 每天 22点
/tr ad 周报 每周日21点
/tr ad 月报 每月1号9点
```

高级写法：

```text
/tr ad 每日晚报 | 30 22 * * * | day | Asia/Shanghai
/tr ad 每周周报 | 0 21 * * 0 | week | Asia/Shanghai
/tr ad 双周回顾 | 0 22 * * 0 | custom:14 | Asia/Shanghai
```

手动运行和删除：

```text
/tr rn a1b2
/tr dl a1b2
```

## 记录写法

时间段：

```text
09:00-10:30
09:00~10:30
23:30-00:30
```

时长：

```text
45分钟
30min
2h
1.5小时
```

日期：

```text
今天
昨天
前天
明天
2026-03-24
2026/03/24
```

结构化字段：

```text
#科研
category:学习
project:论文
kind:plan
```

综合示例：

```text
/ta 08:00-09:30 背单词和精读文章 #英语 category:学习
/ta 14:00-16:00 处理实验数据 #科研 category:研究 project:NiFe
/ta 明天 2小时 准备开题汇报 kind:plan category:规划 project:论文
```

## 常用配置

`default_timezone`

- 默认时区，用于解析相对日期和定时规则。
- 例子：`Asia/Shanghai`

`analysis_provider_id`

- 用于 `/t sm` 和 `/t qy`。

`embedding_provider_id`

- 用于长期记忆写入和向量召回。

`rerank_provider_id`

- 可选，用于 `/t qy` 重排结果。

`record_feedback_provider_id`

- 可选，用于录入后的反馈。

`record_feedback_enabled`

- 控制录入后是否生成反馈。

`summary_with_advice`

- 控制总结里是否附带建议。

`query_answer_with_llm`

- 关闭后，`/t qy` 会直接返回证据，不再让 LLM 组织答案。

`max_query_candidates`

- 长期记忆检索的候选上限。

`vector_similarity_threshold`

- 向量召回阈值，越高越严格。
