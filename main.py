from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re

from apscheduler.triggers.cron import CronTrigger

from astrbot.api import AstrBotConfig, llm_tool, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.star.filter.command import GreedyStr

from .scheduler import LyubishchevScheduler
from .service import PLUGIN_NAME, LyubishchevService
from .storage import LyubishchevStorage


@register(PLUGIN_NAME, "Lynie", "柳比歇夫时间管理", "1.2.1")
class LyubishchevPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.plugin_name = PLUGIN_NAME
        self.data_dir = Path(StarTools.get_data_dir(self.plugin_name))
        self.storage = LyubishchevStorage(self.data_dir / "lyubishchev.sqlite3")
        self.service = LyubishchevService(
            context=self.context,
            config=self.config,
            storage=self.storage,
        )
        self.scheduler = LyubishchevScheduler(
            storage=self.storage,
            service=self.service,
            sender=self._send_plain_message,
        )

    async def initialize(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        await self.storage.initialize()
        await self.scheduler.start()
        logger.info("[%s] initialized, data_dir=%s", self.plugin_name, self.data_dir)

    async def terminate(self) -> None:
        await self.scheduler.shutdown()

    async def _send_plain_message(self, session_id: str, text: str) -> bool:
        chain = MessageChain().message(text)
        sent = await self.context.send_message(session_id, chain)
        return bool(sent)

    def _current_timestamp(self) -> str:
        return self.service._now().isoformat()

    def _format_user_error(self, exc: Exception, fallback: str) -> str:
        if isinstance(exc, ValueError):
            message = str(exc).strip()
            if message:
                return message
        return fallback

    def _extract_command_payload(
        self,
        event: AstrMessageEvent,
        command_path: str | list[str] | tuple[str, ...],
        fallback: str = "",
        *,
        preserve_newlines: bool = False,
    ) -> str:
        fallback_text = str(fallback or "").strip()
        if isinstance(command_path, str):
            command_tokens = [command_path]
        else:
            command_tokens = [token for token in command_path if token]
        raw_message = (event.message_str or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if preserve_newlines:
            message = raw_message
        else:
            message = re.sub(r"\s+", " ", raw_message)
        if not command_tokens:
            return fallback_text
        match = re.match(
            r"^/?"
            + r"\s+".join(re.escape(token) for token in ["lyu", *command_tokens])
            + r"(?:\s+(?P<payload>[\s\S]*))?$",
            message,
            flags=re.IGNORECASE,
        )
        if not match:
            return fallback_text
        payload = str(match.group("payload") or "").strip()
        if payload and (not fallback_text or len(payload) >= len(fallback_text)):
            return payload
        return fallback_text

    def _extract_amend_payload(
        self,
        event: AstrMessageEvent,
        record_id_prefix: str,
        fallback: str = "",
    ) -> str:
        payload = self._extract_command_payload(event, ["amend"], "")
        if payload:
            parts = payload.split(" ", 1)
            if len(parts) == 2 and parts[0] == record_id_prefix:
                return parts[1].strip()
        return str(fallback or "").strip()

    async def _build_record_created_reply(
        self,
        *,
        session_id: str,
        record: dict,
        title: str,
    ) -> str:
        lines = [
            title,
            self.service.format_record_line(record),
            f"解析置信度: {record.get('parser_confidence', 0):.2f}",
        ]
        if record.get("parser_notes"):
            lines.append(f"解析提示: {record['parser_notes']}")
        feedback = await self.service.generate_record_feedback(
            session_id=session_id,
            record=record,
        )
        if feedback:
            lines.extend(["", feedback])
        return "\n".join(lines)

    def _split_note_entries(self, note_text: str) -> list[str]:
        text = note_text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            return []
        if "\n" not in text:
            return [text]
        entries: list[str] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            line = re.sub(r"^(?:[-*•]\s+|\d+[.)、]\s+)", "", line).strip()
            if line:
                entries.append(line)
        return entries

    def _build_batch_record_reply(
        self,
        *,
        records: list[dict],
        failures: list[tuple[str, str]],
    ) -> str:
        if not records:
            lines = ["这次没有成功入库。"]
            if failures:
                lines.append("请检查下面这些行：")
                lines.extend(f"- {text} ({reason})" for text, reason in failures[:10])
            return "\n".join(lines)

        lines = [f"已批量记录 {len(records)} 条："]
        lines.extend(f"- {self.service.format_record_line(record)}" for record in records[:20])
        if len(records) > 20:
            lines.append(f"- 其余 {len(records) - 20} 条也已经入库。")
        if failures:
            lines.extend(
                [
                    "",
                    f"另外有 {len(failures)} 行没有录进去：",
                ]
            )
            lines.extend(f"- {text} ({reason})" for text, reason in failures[:10])
            if len(failures) > 10:
                lines.append(f"- 其余 {len(failures) - 10} 行请回头再检查一下。")
        lines.append("")
        lines.append("批量模式默认是一行一条。")
        return "\n".join(lines)

    async def _build_batch_record_reply_with_feedback(
        self,
        *,
        session_id: str,
        records: list[dict],
        failures: list[tuple[str, str]],
    ) -> str:
        lines = [self._build_batch_record_reply(records=records, failures=failures)]
        feedback = await self.service.generate_records_feedback(
            session_id=session_id,
            records=records,
            failures=failures,
        )
        if feedback:
            lines.extend(["", feedback])
        return "\n".join(lines)

    async def _get_record_for_session(
        self,
        event: AstrMessageEvent,
        record_id_prefix: str,
    ) -> dict | None:
        record_id_prefix = record_id_prefix.strip()
        if len(record_id_prefix) < 4:
            return None
        record_id = await self.storage.resolve_record_id(
            event.unified_msg_origin,
            record_id_prefix,
        )
        if not record_id:
            return None
        return await self.storage.get_record(record_id)

    async def _get_rule_for_session(
        self,
        event: AstrMessageEvent,
        rule_id_prefix: str,
    ) -> dict | None:
        rule_id_prefix = rule_id_prefix.strip()
        if len(rule_id_prefix) < 4:
            return None
        rule_id = await self.storage.resolve_rule_id(
            event.unified_msg_origin,
            rule_id_prefix,
        )
        if not rule_id:
            return None
        return await self.storage.get_summary_rule(rule_id)

    def _format_query_tool_result(self, result: dict) -> str:
        lines = [result["answer"]]
        candidates = result.get("candidates", [])
        if candidates:
            lines.extend(["", "相关记录证据："])
            for candidate in candidates[:5]:
                content = str(candidate.get("content", "") or "").replace("\n", " | ").strip()
                if len(content) > 220:
                    content = content[:217] + "..."
                lines.append(
                    f"- [{candidate['source_type']}:{candidate['source_id'][:8]}] {content}"
                )
        return "\n".join(lines)

    def _format_summary_snapshot_for_tool(self, snapshot: dict) -> str:
        stats = snapshot["stats"]
        start_date = snapshot["start_date"].isoformat()
        end_date = snapshot["end_date"].isoformat()
        lines = [
            f"统计区间: {start_date} -> {end_date}",
            f"记录数: {stats['record_count']}",
            f"可统计总时长: {stats['total_minutes']} 分钟",
        ]
        category_minutes = list(stats.get("category_minutes", {}).items())[:5]
        if category_minutes:
            lines.append("分类分布:")
            lines.extend(f"- {name}: {minutes} 分钟" for name, minutes in category_minutes)
        project_minutes = list(stats.get("project_minutes", {}).items())[:5]
        if project_minutes:
            lines.append("项目分布:")
            lines.extend(f"- {name}: {minutes} 分钟" for name, minutes in project_minutes)
        recent_records = snapshot["records"][:8]
        if recent_records:
            lines.append("周期内记录样本:")
            lines.extend(f"- {self.service.format_record_line(record)}" for record in recent_records)
        return "\n".join(lines)

    @llm_tool(name="lyu_query_history")
    async def lyu_query_history_tool(self, event: AstrMessageEvent, question: str) -> str:
        """检索并回答用户关于时间记录的问题。

        当用户在日常聊天里询问“今天/昨天/这周做了什么”“最近时间花在哪”“某个项目投入如何”
        “有没有摸鱼/熬夜/学习记录”“帮我翻一下之前的时间账本”这类问题时调用。

        Args:
            question(string): 用户关于时间记录的原始问题，尽量保留时间范围、项目、标签和关键词。
        """
        result = await self.service.query_memory(
            session_id=event.unified_msg_origin,
            question=question,
            with_llm=False,
        )
        return self._format_query_tool_result(result)

    @llm_tool(name="lyu_get_period_summary")
    async def lyu_get_period_summary_tool(self, event: AstrMessageEvent, period_spec: str = "day") -> str:
        """获取某个周期内的时间记录总结。

        当用户在日常聊天里询问“今天我都做了什么”“这周时间怎么分配的”“给我看最近7天总结”
        这类按周期查看时间账本的问题时调用。

        Args:
            period_spec(string): 周期参数，支持 day、week、month、custom:7、range:2026-03-01,2026-03-07。
        """
        try:
            summary_type, start_date, end_date = self._parse_summary_spec(period_spec or "day")
        except ValueError as exc:
            return f"周期参数格式不对：{exc}"
        snapshot = await self.service.build_summary_snapshot(
            session_id=event.unified_msg_origin,
            summary_type=summary_type,
            start_date=start_date,
            end_date=end_date,
        )
        if snapshot is None:
            return "这个周期里还没有可用的时间记录。"
        return self._format_summary_snapshot_for_tool(snapshot)

    @llm_tool(name="lyu_list_recent_records")
    async def lyu_list_recent_records_tool(self, event: AstrMessageEvent, limit: int = 8) -> str:
        """列出最近的时间记录原文。

        当用户在日常聊天里想看“最近几条时间记录”“先把原始记录列出来”“把今天记过的账给我看看”
        这类偏原始清单的问题时调用。

        Args:
            limit(number): 想查看的记录条数，建议 3 到 15 之间。
        """
        records = await self.storage.list_records(
            event.unified_msg_origin,
            limit=max(1, min(int(limit), 20)),
        )
        if not records:
            return "还没有任何时间记录。"
        total_minutes = sum(record.get("duration_minutes") or 0 for record in records)
        lines = [f"最近 {len(records)} 条记录，共计 {total_minutes} 分钟："]
        lines.extend(f"- {self.service.format_record_line(record)}" for record in records)
        return "\n".join(lines)

    @filter.command_group("lyu")
    def lyu(self) -> None:
        """柳比歇夫时间管理主指令组。"""

    @lyu.command("help")
    async def lyu_help(self, event: AstrMessageEvent):
        """查看插件帮助。"""
        lines = [
            "柳比歇夫时间管理使用入口：",
            "/lyu note <记录文本>  手动记录，支持自然语言、时间段、标签、分类、项目",
            "/lyu note 后面换行写多条也可以，默认一行一条分别入库",
            "/lyu today",
            "/lyu recent [数量]",
            "/lyu show <记录ID前缀>",
            "/lyu amend <记录ID前缀> <新文本>",
            "/lyu delete <记录ID前缀>",
            "/lyu summary <day|week|month|custom:7|range:2026-03-01,2026-03-07>  生成总结",
            "/lyu query <问题或关键词>  追查长期记录",
            "/lyu rule list  查看定时总结规则",
            "/lyu rule add <名称 | cron | day|week|month|custom:7 | timezone可选>",
            "/lyu rule delete <规则ID前缀>",
            "/lyu rule run <规则ID前缀>",
            "/lyu status",
            "",
            "记录文本示例：",
            "/lyu note 09:00-10:30 阅读论文 #科研 category:学习 project:论文",
            "/lyu note 昨天 45分钟 处理报销 #行政",
            "/lyu note 2026-03-24 21:00-22:15 写日报 #复盘 category:总结",
            "",
            "自动记录前缀默认是：记录： / 记时： / lyu：",
            "每次成功录入后，AstrBot 会结合最近聊天和最近时间记录给你一段录入反馈。",
            "现在平时聊天里直接问“我最近都在干嘛”“这周时间花哪了”这类问题，也可以自动调用时间账本工具。",
            "详细说明见插件目录里的 README.md。",
        ]
        yield event.plain_result("\n".join(lines))

    @lyu.command("note")
    async def lyu_note(self, event: AstrMessageEvent, text: GreedyStr):
        """新增一条时间记录。"""
        note_text = self._extract_command_payload(
            event,
            ["note"],
            str(text),
            preserve_newlines=True,
        )
        if not note_text:
            yield event.plain_result("请在 /lyu note 后输入要记录的时间文本。")
            return

        entries = self._split_note_entries(note_text)
        if len(entries) <= 1:
            try:
                record = await self.service.create_record(
                    session_id=event.unified_msg_origin,
                    platform_id=event.get_platform_id(),
                    sender_id=event.get_sender_id(),
                    sender_name=event.get_sender_name(),
                    text=entries[0] if entries else note_text.strip(),
                    source="command",
                )
            except (ValueError, RuntimeError) as exc:
                logger.warning(
                    "/lyu note failed for session %s: %s",
                    event.unified_msg_origin,
                    exc,
                    exc_info=True,
                )
                yield event.plain_result(
                    self._format_user_error(exc, "记录失败，请检查时间写法后重试。")
                )
                return
            yield event.plain_result(
                await self._build_record_created_reply(
                    session_id=event.unified_msg_origin,
                    record=record,
                    title="已记录：",
                )
            )
            return

        records: list[dict] = []
        failures: list[tuple[str, str]] = []
        for entry in entries:
            try:
                record = await self.service.create_record(
                    session_id=event.unified_msg_origin,
                    platform_id=event.get_platform_id(),
                    sender_id=event.get_sender_id(),
                    sender_name=event.get_sender_name(),
                    text=entry,
                    source="command",
                )
                records.append(record)
            except (ValueError, RuntimeError) as exc:
                logger.warning(
                    "Batch /lyu note failed for line %r: %s",
                    entry,
                    exc,
                    exc_info=True,
                )
                failures.append(
                    (
                        entry,
                        self._format_user_error(exc, "格式有误，请检查后重试。"),
                    )
                )
        yield event.plain_result(
            await self._build_batch_record_reply_with_feedback(
                session_id=event.unified_msg_origin,
                records=records,
                failures=failures,
            )
        )

    @lyu.command("today")
    async def lyu_today(self, event: AstrMessageEvent):
        """查看今天的记录。"""
        today = self.service._now().date().isoformat()
        records = await self.storage.list_records(
            event.unified_msg_origin,
            start_date=today,
            end_date=today,
            limit=100,
        )
        if not records:
            yield event.plain_result("今天还没有记录。")
            return
        total_minutes = sum(record.get("duration_minutes") or 0 for record in records)
        lines = [f"今天的记录（{len(records)} 条，总计 {total_minutes} 分钟）："]
        lines.extend(f"- {self.service.format_record_line(record)}" for record in records)
        yield event.plain_result("\n".join(lines))

    @lyu.command("recent")
    async def lyu_recent(self, event: AstrMessageEvent, limit: int = 10):
        """查看最近的记录。"""
        records = await self.storage.list_records(
            event.unified_msg_origin,
            limit=max(1, min(limit, 50)),
        )
        if not records:
            yield event.plain_result("还没有任何时间记录。")
            return
        lines = [f"最近 {len(records)} 条记录："]
        lines.extend(f"- {self.service.format_record_line(record)}" for record in records)
        yield event.plain_result("\n".join(lines))

    @lyu.command("show")
    async def lyu_show(self, event: AstrMessageEvent, record_id_prefix: str):
        """查看某条记录详情。"""
        record = await self._get_record_for_session(event, record_id_prefix)
        if not record:
            yield event.plain_result("没有找到匹配的记录 ID，请提供更完整的前缀。")
            return
        revisions = await self.storage.list_revisions(record["record_id"])
        yield event.plain_result(self.service.format_record_detail(record, revisions))

    @lyu.command("amend")
    async def lyu_amend(
        self,
        event: AstrMessageEvent,
        record_id_prefix: str,
        text: GreedyStr,
    ):
        """修订某条记录。"""
        record = await self._get_record_for_session(event, record_id_prefix)
        if not record:
            yield event.plain_result("没有找到匹配的记录 ID，请提供更完整的前缀。")
            return
        amend_text = self._extract_amend_payload(event, record_id_prefix, str(text))
        if not amend_text:
            yield event.plain_result("请在记录 ID 后提供新的记录文本。")
            return
        updated = await self.service.amend_record(record, amend_text)
        if not updated:
            yield event.plain_result("修订失败，记录可能已不存在。")
            return
        yield event.plain_result("已修订：\n" + self.service.format_record_line(updated))

    @lyu.command("delete")
    async def lyu_delete(self, event: AstrMessageEvent, record_id_prefix: str):
        """软删除某条记录。"""
        record = await self._get_record_for_session(event, record_id_prefix)
        if not record:
            yield event.plain_result("没有找到匹配的记录 ID，请提供更完整的前缀。")
            return
        await self.service.delete_record(record)
        yield event.plain_result(f"已删除记录 {record['record_id'][:8]}。")

    @lyu.command("summary")
    async def lyu_summary(self, event: AstrMessageEvent, spec: GreedyStr = "day"):
        """按周期生成总结。"""
        period_spec = self._extract_command_payload(event, ["summary"], str(spec)) or "day"
        try:
            summary_type, start_date, end_date = self._parse_summary_spec(period_spec)
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return
        summary = await self.service.generate_summary(
            session_id=event.unified_msg_origin,
            summary_type=summary_type,
            start_date=start_date,
            end_date=end_date,
        )
        if summary is None:
            yield event.plain_result("该周期暂无可用记录。")
            return
        yield event.plain_result(summary["content"])

    def _parse_summary_spec(self, spec: str):
        now = self.service._now()
        if spec in {"day", "week", "month"}:
            start_date, end_date = self.service.get_period_bounds(spec, now=now)
            return spec, start_date, end_date
        if spec.startswith("custom:"):
            try:
                days = int(spec.split(":", 1)[1])
            except ValueError as exc:
                raise ValueError("custom 格式错误，应为 custom:7") from exc
            if days <= 0:
                raise ValueError("custom 天数必须大于 0。")
            start_date, end_date = self.service.get_period_bounds(
                "custom",
                custom_days=days,
                now=now,
            )
            return "custom", start_date, end_date
        if spec.startswith("range:"):
            body = spec.split(":", 1)[1]
            try:
                left, right = [part.strip() for part in body.split(",", 1)]
                start_date = datetime.fromisoformat(left).date()
                end_date = datetime.fromisoformat(right).date()
                if end_date < start_date:
                    raise ValueError("结束日期不能早于开始日期")
                return (
                    "range",
                    start_date,
                    end_date,
                )
            except Exception as exc:  # noqa: BLE001
                raise ValueError(
                    "range 格式错误，应为 range:2026-03-01,2026-03-07"
                ) from exc
        raise ValueError("summary 参数只支持 day/week/month/custom:7/range:开始,结束")

    @lyu.command("query")
    async def lyu_query(self, event: AstrMessageEvent, question: GreedyStr):
        """进行长期记忆检索与追查。"""
        question_text = self._extract_command_payload(event, ["query"], str(question))
        if not question_text:
            yield event.plain_result("请在 /lyu query 后输入问题或关键词。")
            return
        result = await self.service.query_memory(
            session_id=event.unified_msg_origin,
            question=question_text,
        )
        lines = [result["answer"]]
        if result["candidates"]:
            lines.append("")
            lines.append("证据：")
            for candidate in result["candidates"][:5]:
                lines.append(
                    f"- [{candidate['source_type']}:{candidate['source_id'][:8]}] "
                    f"score={candidate.get('score', 0):.3f}"
                )
        yield event.plain_result("\n".join(lines))

    @lyu.group("rule")
    def lyu_rule(self) -> None:
        """定时总结规则管理。"""

    @lyu_rule.command("list")
    async def lyu_rule_list(self, event: AstrMessageEvent):
        """列出当前会话的定时总结规则。"""
        rules = await self.storage.list_summary_rules(event.unified_msg_origin)
        if not rules:
            yield event.plain_result("当前会话还没有定时总结规则。")
            return
        lines = ["当前会话的定时规则："]
        for rule in rules:
            next_run = self.scheduler.get_next_run_time(str(rule["rule_id"])) or "未调度"
            lines.append(
                f"- [{rule['rule_id'][:8]}] {rule['rule_name']} | {rule['cron_expression']} | "
                f"{rule['period_type']} | {rule['timezone']} | "
                f"{'启用' if rule.get('enabled') else '停用'} | next: {next_run}"
            )
        yield event.plain_result("\n".join(lines))

    @lyu_rule.command("add")
    async def lyu_rule_add(self, event: AstrMessageEvent, spec: GreedyStr):
        """添加定时总结规则。"""
        raw = self._extract_command_payload(event, ["rule", "add"], str(spec))
        if not raw:
            yield event.plain_result(
                "请使用 /lyu rule add <名称 | cron | day|week|month|custom:7 | timezone可选>"
            )
            return
        try:
            name, cron_expr, period_type, timezone_name = self._parse_rule_spec(raw)
            CronTrigger.from_crontab(cron_expr, timezone=timezone_name)
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"规则格式错误：{exc}")
            return

        now = self._current_timestamp()
        lookback_days = None
        if period_type.startswith("custom:"):
            lookback_days = int(period_type.split(":", 1)[1])
        rule = await self.storage.upsert_summary_rule(
            {
                "session_id": event.unified_msg_origin,
                "platform_id": event.get_platform_id(),
                "rule_name": name,
                "cron_expression": cron_expr,
                "timezone": timezone_name,
                "period_type": period_type,
                "lookback_days": lookback_days,
                "enabled": 1,
                "send_empty": 0,
                "created_at": now,
                "updated_at": now,
            }
        )
        await self.scheduler.reload_rules()
        next_run = self.scheduler.get_next_run_time(str(rule["rule_id"])) or "未调度"
        yield event.plain_result(
            f"已添加规则 [{rule['rule_id'][:8]}] {rule['rule_name']}。\n下次触发时间: {next_run}"
        )

    def _parse_rule_spec(self, spec: str) -> tuple[str, str, str, str]:
        parts = [part.strip() for part in spec.split("|")]
        if len(parts) not in {3, 4}:
            raise ValueError("应为 名称 | cron | day|week|month|custom:7 | timezone可选")
        name, cron_expr, period_type = parts[:3]
        if not name:
            raise ValueError("规则名称不能为空")
        if not cron_expr:
            raise ValueError("cron 表达式不能为空")
        timezone_name = (
            parts[3] if len(parts) == 4 and parts[3] else self.service.get_default_timezone()
        )
        if period_type not in {"day", "week", "month"} and not period_type.startswith("custom:"):
            raise ValueError("周期只支持 day / week / month / custom:天数")
        if period_type.startswith("custom:") and int(period_type.split(":", 1)[1]) <= 0:
            raise ValueError("custom:天数 中的天数必须大于 0")
        return name, cron_expr, period_type, timezone_name

    @lyu_rule.command("delete")
    async def lyu_rule_delete(self, event: AstrMessageEvent, rule_id_prefix: str):
        """删除定时总结规则。"""
        rule = await self._get_rule_for_session(event, rule_id_prefix)
        if not rule:
            yield event.plain_result("没有找到匹配的规则 ID，请提供更完整的前缀。")
            return
        await self.storage.delete_summary_rule(rule["rule_id"])
        await self.scheduler.reload_rules()
        yield event.plain_result(f"已删除规则 {rule['rule_id'][:8]}。")

    @lyu_rule.command("run")
    async def lyu_rule_run(self, event: AstrMessageEvent, rule_id_prefix: str):
        """立即触发一次定时规则。"""
        rule = await self._get_rule_for_session(event, rule_id_prefix)
        if not rule:
            yield event.plain_result("没有找到匹配的规则 ID，请提供更完整的前缀。")
            return
        ok = await self.scheduler.run_rule_now(rule["rule_id"])
        if not ok:
            yield event.plain_result("触发失败，规则可能已不存在。")
            return
        yield event.plain_result("已手动触发规则，本次总结会主动发送到当前会话。")

    @lyu.command("status")
    async def lyu_status(self, event: AstrMessageEvent):
        """查看插件状态。"""
        record_count = await self.storage.count_records(event.unified_msg_origin)
        rules = await self.storage.list_summary_rules(event.unified_msg_origin)
        lines = [
            f"数据目录: {self.data_dir}",
            f"默认时区: {self.service.get_default_timezone()}",
            f"记录数: {record_count}",
            f"定时规则数: {len(rules)}",
            f"分析 Provider: {self.config.get('analysis_provider_id', '') or '当前会话 Provider'}",
            f"Embedding Provider: {self.config.get('embedding_provider_id', '') or '未配置'}",
            f"Rerank Provider: {self.config.get('rerank_provider_id', '') or '未配置'}",
            f"录入反馈: {'开启' if self.service.record_feedback_enabled() else '关闭'}",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def auto_record_listener(self, event: AstrMessageEvent):
        """监听带前缀的自然语言时间记录。"""
        message = (event.message_str or "").strip()
        if not message or message.startswith("/lyu"):
            return
        if self.service.auto_record_require_wake() and not event.is_at_or_wake_command:
            return
        prefix_hit = None
        for prefix in self.service.get_auto_record_prefixes():
            if message.startswith(prefix):
                prefix_hit = prefix
                break
        if not prefix_hit:
            return
        text = message[len(prefix_hit) :].strip()
        if not text:
            yield event.plain_result("检测到记录前缀，但没有实际内容。")
            event.stop_event()
            return
        try:
            record = await self.service.create_record(
                session_id=event.unified_msg_origin,
                platform_id=event.get_platform_id(),
                sender_id=event.get_sender_id(),
                sender_name=event.get_sender_name(),
                text=text,
                source="auto",
            )
        except (ValueError, RuntimeError) as exc:
            logger.warning(
                "Auto record failed for session %s: %s",
                event.unified_msg_origin,
                exc,
                exc_info=True,
            )
            yield event.plain_result(
                self._format_user_error(exc, "自动记录失败，请检查时间写法后重试。")
            )
            event.stop_event()
            return
        yield event.plain_result(
            await self._build_record_created_reply(
                session_id=event.unified_msg_origin,
                record=record,
                title="已自动记录：",
            )
        )
        event.stop_event()
