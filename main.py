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


@register(PLUGIN_NAME, "Lynie", "柳比歇夫时间管理", "2.3.1")
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

    # ---- internal helpers ----

    async def _send_plain_message(self, session_id: str, text: str) -> bool:
        chain = MessageChain().message(text)
        sent = await self.context.send_message(session_id, chain)
        return bool(sent)

    def _scoped_session(self, event: AstrMessageEvent) -> str:
        """Get sender-scoped session id to isolate records per user."""
        return self.service.get_scoped_session_id(
            event.unified_msg_origin, event.get_sender_id()
        )

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
            + r"\s+".join(re.escape(token) for token in ["t", *command_tokens])
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
        payload = self._extract_command_payload(event, ["ed"], "")
        if payload:
            parts = payload.split(" ", 1)
            if len(parts) == 2 and parts[0] == record_id_prefix:
                return parts[1].strip()
        return str(fallback or "").strip()

    def _extract_root_command_payload(
        self,
        event: AstrMessageEvent,
        command_name: str,
        fallback: str = "",
        *,
        preserve_newlines: bool = False,
    ) -> str:
        fallback_text = str(fallback or "").strip()
        raw_message = (event.message_str or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        message = raw_message if preserve_newlines else re.sub(r"\s+", " ", raw_message)
        match = re.match(
            r"^/?"
            + re.escape(command_name)
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
        has_multiple = "\n" in text or ";" in text or "；" in text
        if not has_multiple:
            return [text]
        entries: list[str] = []
        normalized = text.replace("；", ";").replace(";", "\n")
        for raw_line in normalized.splitlines():
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
        lines.append("批量模式支持换行或分号分隔。")
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
    ) -> tuple[dict | None, str | None]:
        """Returns (record, error_message)."""
        record_id_prefix = record_id_prefix.strip()
        if len(record_id_prefix) < 4:
            return None, "ID 前缀至少需要 4 个字符。"
        record_id = await self.storage.resolve_record_id(
            self._scoped_session(event),
            record_id_prefix,
        )
        if not record_id:
            count = await self.storage.count_records_by_prefix(
                self._scoped_session(event),
                record_id_prefix,
            )
            if count > 1:
                return None, f"匹配到 {count} 条记录，请提供更长的 ID 前缀。"
            return None, "没有找到匹配的记录。"
        record = await self.storage.get_record(record_id)
        if not record:
            return None, "记录已不存在。"
        return record, None

    async def _get_rule_for_session(
        self,
        event: AstrMessageEvent,
        rule_id_prefix: str,
    ) -> dict | None:
        rule_id_prefix = rule_id_prefix.strip()
        if len(rule_id_prefix) < 4:
            return None
        rule_id = await self.storage.resolve_rule_id(
            self._scoped_session(event),
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
        if stats.get("plan_record_count"):
            lines.append(
                f"计划记录: {stats['plan_record_count']} 条，共 {stats.get('plan_minutes', 0)} 分钟"
                "（未计入上面的实际总时长）"
            )
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

    async def _handle_create_record(
        self,
        event: AstrMessageEvent,
        text: str,
        *,
        source: str = "command",
        title: str = "已记录：",
    ):
        """Shared logic for creating time records."""
        entries = self._split_note_entries(text)
        if len(entries) <= 1:
            try:
                record = await self.service.create_record(
                    session_id=self._scoped_session(event),
                    platform_id=event.get_platform_id(),
                    sender_id=event.get_sender_id(),
                    sender_name=event.get_sender_name(),
                    text=entries[0] if entries else text.strip(),
                    source=source,
                )
            except (ValueError, RuntimeError) as exc:
                logger.warning(
                    "Record creation failed for session %s: %s",
                    self._scoped_session(event),
                    exc,
                    exc_info=True,
                )
                yield event.plain_result(
                    self._format_user_error(exc, "记录失败，请检查时间写法后重试。")
                )
                return
            yield event.plain_result(
                await self._build_record_created_reply(
                    session_id=self._scoped_session(event),
                    record=record,
                    title=title,
                )
            )
            return

        records: list[dict] = []
        failures: list[tuple[str, str]] = []
        for entry in entries:
            try:
                record = await self.service.create_record(
                    session_id=self._scoped_session(event),
                    platform_id=event.get_platform_id(),
                    sender_id=event.get_sender_id(),
                    sender_name=event.get_sender_name(),
                    text=entry,
                    source=source,
                )
                records.append(record)
            except (ValueError, RuntimeError) as exc:
                logger.warning(
                    "Batch record failed for line %r: %s",
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
                session_id=self._scoped_session(event),
                records=records,
                failures=failures,
            )
        )

    # ---- LLM tools ----

    @llm_tool(name="time_query_history")
    async def time_query_history_tool(self, event: AstrMessageEvent, question: str) -> str:
        """检索并回答用户关于时间记录的问题。

        当用户在日常聊天里询问"今天/昨天/这周做了什么""最近时间花在哪""某个项目投入如何"
        "有没有摸鱼/熬夜/学习记录""帮我翻一下之前的时间账本"这类问题时调用。

        Args:
            question(string): 用户关于时间记录的原始问题，尽量保留时间范围、项目、标签和关键词。
        """
        result = await self.service.query_memory(
            session_id=self._scoped_session(event),
            question=question,
            with_llm=False,
        )
        return self._format_query_tool_result(result)

    @llm_tool(name="time_get_period_summary")
    async def time_get_period_summary_tool(self, event: AstrMessageEvent, period_spec: str = "day") -> str:
        """获取某个周期内的时间记录总结。

        当用户在日常聊天里询问"今天我都做了什么""这周时间怎么分配的""给我看最近7天总结"
        这类按周期查看时间账本的问题时调用。

        Args:
            period_spec(string): 周期参数，支持 day/today/week/本周/上周/month/本月/上月/最近7天/range:2026-03-01,2026-03-07。
        """
        try:
            summary_type, start_date, end_date = self.service.parse_natural_period(period_spec or "day")
        except ValueError as exc:
            return f"周期参数格式不对：{exc}"
        snapshot = await self.service.build_summary_snapshot(
            session_id=self._scoped_session(event),
            summary_type=summary_type,
            start_date=start_date,
            end_date=end_date,
        )
        if snapshot is None:
            return "这个周期里还没有可用的时间记录。"
        return self._format_summary_snapshot_for_tool(snapshot)

    @llm_tool(name="time_list_recent_records")
    async def time_list_recent_records_tool(self, event: AstrMessageEvent, limit: int = 8) -> str:
        """列出最近的时间记录原文。

        当用户在日常聊天里想看"最近几条时间记录""先把原始记录列出来""把今天记过的账给我看看"
        这类偏原始清单的问题时调用。

        Args:
            limit(number): 想查看的记录条数，建议 3 到 15 之间。
        """
        records = await self.storage.list_records(
            self._scoped_session(event),
            limit=max(1, min(int(limit), 20)),
        )
        if not records:
            return "还没有任何时间记录。"
        total_minutes = sum(record.get("duration_minutes") or 0 for record in records)
        lines = [f"最近 {len(records)} 条记录，共计 {total_minutes} 分钟："]
        lines.extend(f"- {self.service.format_record_line(record)}" for record in records)
        return "\n".join(lines)

    # ---- command groups ----

    @filter.command("ta")
    async def time_add(self, event: AstrMessageEvent, text: GreedyStr = ""):
        """直接新增时间记录。推荐作为默认录入入口。"""
        note_text = self._extract_root_command_payload(
            event,
            "ta",
            str(text),
            preserve_newlines=True,
        )
        if not note_text:
            yield event.plain_result(
                "请在 /ta 后输入内容，例如：/ta 09:00-10:30 阅读论文 #科研"
            )
            return
        async for result in self._handle_create_record(
            event,
            note_text,
            source="ta",
            title="已记录：",
        ):
            yield result

    @filter.command_group("t")
    def time_group(self) -> None:
        """时间管理主指令组。"""

    @filter.command_group("tr")
    def time_rule(self) -> None:
        """定时总结规则管理。"""

    @time_group.command("hp", alias={"help"})
    async def time_help(self, event: AstrMessageEvent):
        """查看插件帮助。"""
        lines = [
            "柳比歇夫时间管理",
            "",
            "1. 新增记录只用 /ta",
            "  /ta 09:00-10:30 阅读论文 #科研",
            "  /ta 45分钟 处理报销 #行政",
            "  /ta 支持换行或分号批量录入",
            "",
            "2. 查看记录",
            "  /t ls                  今天的记录",
            "  /t ls 10               最近 10 条",
            "  /t ls 昨天 / 本周 / 3.1-3.7",
            "  /t ls 8f3a             查看某条详情",
            "",
            "3. 修改记录",
            "  /t ed 8f3a 09:00-10:40 阅读论文 #科研",
            "  /t dl 8f3a",
            "  /t ud                  撤销最近一条有效记录",
            "",
            "4. 计时",
            "  /t on 阅读论文",
            "  /t of                  结束当前计时并记录",
            "",
            "5. 总结与追查",
            "  /t sm 今天 / 本周 / 上周 / 本月 / 最近7天",
            "  /t qy 我最近在 NiFe 上花了多少时间？",
            "",
            "6. 定时总结规则",
            "  /tr ls",
            "  /tr ad 日报 每天22点",
            "  /tr ad 日报 每天 22点",
            "  /tr ad 每日晚报 | 30 22 * * * | day | Asia/Shanghai",
            "  /tr dl <规则ID前缀>",
            "  /tr rn <规则ID前缀>",
            "",
            "7. 其他",
            "  /t hp                  查看这份帮助",
            "  /t st                  查看插件状态",
        ]
        yield event.plain_result("\n".join(lines))

    @time_group.command("ls", alias={"list"})
    async def time_ls(self, event: AstrMessageEvent, spec: GreedyStr = ""):
        """查看时间记录。支持：无参数(今天)/数字(最近N条)/日期/周期关键词/ID前缀。"""
        spec_str = self._extract_command_payload(event, ["ls"], str(spec)).strip()

        # Default: today's records in chronological order
        if not spec_str:
            today = self.service._now().date().isoformat()
            records = await self.storage.list_records(
                self._scoped_session(event),
                start_date=today,
                end_date=today,
                limit=100,
            )
            if not records:
                timer = await self.storage.get_active_timer(self._scoped_session(event))
                if timer:
                    yield event.plain_result(
                        "今天还没有完成的记录。\n"
                        f"正在计时: {self.service.format_record_line(timer)}"
                    )
                else:
                    yield event.plain_result("今天还没有记录。")
                return
            records.reverse()
            total = sum(r.get("duration_minutes") or 0 for r in records)
            lines = [f"今天的记录（{len(records)} 条，共 {total} 分钟）："]
            lines.extend(f"- {self.service.format_record_line(r)}" for r in records)
            timer = await self.storage.get_active_timer(self._scoped_session(event))
            if timer:
                lines.extend(["", f"正在计时: {self.service.format_record_line(timer)}"])
            yield event.plain_result("\n".join(lines))
            return

        # Try as pure number → recent N records
        if spec_str.isdigit():
            n = max(1, min(int(spec_str), 200))
            records = await self.storage.list_records(
                self._scoped_session(event),
                limit=n,
            )
            if not records:
                yield event.plain_result("还没有任何时间记录。")
                return
            lines = [f"最近 {len(records)} 条记录："]
            lines.extend(f"- {self.service.format_record_line(r)}" for r in records)
            yield event.plain_result("\n".join(lines))
            return

        # Try as period keyword or date range
        try:
            _, start_date, end_date = self.service.parse_natural_period(spec_str)
            records = await self.storage.list_records(
                self._scoped_session(event),
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
                limit=200,
            )
            if not records:
                yield event.plain_result(f"{start_date} ~ {end_date} 没有记录。")
                return
            records.reverse()
            total = sum(r.get("duration_minutes") or 0 for r in records)
            lines = [f"{start_date} ~ {end_date}（{len(records)} 条，共 {total} 分钟）："]
            lines.extend(f"- {self.service.format_record_line(r)}" for r in records)
            yield event.plain_result("\n".join(lines))
            return
        except ValueError:
            pass

        # Try as record ID prefix (hex, >= 4 chars)
        if re.match(r"^[0-9a-f]{4,}$", spec_str, re.IGNORECASE):
            record, error = await self._get_record_for_session(event, spec_str)
            if record:
                revisions = await self.storage.list_revisions(record["record_id"])
                yield event.plain_result(self.service.format_record_detail(record, revisions))
                return
            if error:
                yield event.plain_result(error)
                return

        yield event.plain_result(
            f"无法识别的参数：{spec_str}\n"
            "支持：数字(最近N条)、日期、本周/上周/本月/上月/最近N天、记录ID前缀"
        )

    @time_group.command("ed", alias={"edit", "amend"})
    async def time_amend(
        self,
        event: AstrMessageEvent,
        record_id_prefix: str,
        text: GreedyStr,
    ):
        """修订某条记录。"""
        record, error = await self._get_record_for_session(event, record_id_prefix)
        if not record:
            yield event.plain_result(error or "没有找到匹配的记录。")
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

    @time_group.command("dl", alias={"del", "delete"})
    async def time_delete(self, event: AstrMessageEvent, record_id_prefix: str):
        """软删除某条记录。"""
        record, error = await self._get_record_for_session(event, record_id_prefix)
        if not record:
            yield event.plain_result(error or "没有找到匹配的记录。")
            return
        await self.service.delete_record(record)
        yield event.plain_result(f"已删除记录 {record['record_id'][:8]}。")

    @time_group.command("ud", alias={"undo"})
    async def time_undo(self, event: AstrMessageEvent):
        """撤销最近一条记录。"""
        record = await self.storage.get_latest_record(self._scoped_session(event))
        if not record:
            yield event.plain_result("没有可以撤销的记录。")
            return
        await self.service.delete_record(record)
        yield event.plain_result(
            f"已撤销：\n{self.service.format_record_line(record)}"
        )

    @time_group.command("on", alias={"start", "now"})
    async def time_now(self, event: AstrMessageEvent, text: GreedyStr):
        """开始计时。"""
        note_text = self._extract_command_payload(event, ["on"], str(text))
        if not note_text:
            yield event.plain_result("请在 /t on 后输入事项，例如：/t on 阅读论文")
            return
        try:
            record = await self.service.start_timer(
                session_id=self._scoped_session(event),
                platform_id=event.get_platform_id(),
                sender_id=event.get_sender_id(),
                sender_name=event.get_sender_name(),
                text=note_text,
            )
        except (ValueError, RuntimeError) as exc:
            yield event.plain_result(self._format_user_error(exc, "开始计时失败。"))
            return
        started = datetime.fromisoformat(record["started_at"])
        yield event.plain_result(
            f"开始计时：{record.get('normalized_text') or record.get('raw_text')}\n"
            f"开始时间：{started.strftime('%H:%M')}\n"
            "完成后用 /t of 停止计时。"
        )

    @time_group.command("of", alias={"off", "end", "stop"})
    async def time_stop(self, event: AstrMessageEvent):
        """停止计时并记录。"""
        record = await self.service.stop_timer(self._scoped_session(event))
        if not record:
            yield event.plain_result("当前没有正在计时的任务。")
            return
        yield event.plain_result(
            await self._build_record_created_reply(
                session_id=self._scoped_session(event),
                record=record,
                title="计时完成：",
            )
        )

    @time_group.command("sm", alias={"sum", "summary"})
    async def time_summary(self, event: AstrMessageEvent, spec: GreedyStr = "day"):
        """按周期生成总结。"""
        period_spec = self._extract_command_payload(event, ["sm"], str(spec)) or "day"
        try:
            summary_type, start_date, end_date = self.service.parse_natural_period(period_spec)
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return
        summary = await self.service.generate_summary(
            session_id=self._scoped_session(event),
            summary_type=summary_type,
            start_date=start_date,
            end_date=end_date,
        )
        if summary is None:
            yield event.plain_result("该周期暂无可用记录。")
            return
        yield event.plain_result(summary["content"])

    @time_group.command("qy", alias={"query"})
    async def time_query(self, event: AstrMessageEvent, question: GreedyStr):
        """进行长期记忆检索与追查。"""
        question_text = self._extract_command_payload(event, ["qy"], str(question))
        if not question_text:
            yield event.plain_result("请在 /t qy 后输入问题或关键词。")
            return
        result = await self.service.query_memory(
            session_id=self._scoped_session(event),
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

    @time_rule.command("ls", alias={"list"})
    async def time_rule_list(self, event: AstrMessageEvent):
        """列出当前会话的定时总结规则。"""
        rules = await self.storage.list_summary_rules(self._scoped_session(event))
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

    @time_rule.command("ad", alias={"add"})
    async def time_rule_add(self, event: AstrMessageEvent, spec: GreedyStr):
        """添加定时总结规则。"""
        raw = self._extract_root_command_payload(
            event,
            "tr",
            str(spec),
            preserve_newlines=True,
        )
        stripped = raw.lstrip()
        lowered = stripped.lower()
        if lowered.startswith("ad "):
            raw = stripped[3:].strip()
        elif lowered == "ad":
            raw = ""
        elif lowered.startswith("add "):
            raw = stripped[4:].strip()
        elif lowered == "add":
            raw = ""
        if not raw:
            yield event.plain_result(
                "用法：\n"
                "/tr ad 日报 每天22点\n"
                "/tr ad 周报 每周日21点\n"
                "/tr ad 月报 每月1号9点\n"
                "高级：/tr ad <名称 | cron | day|week|month | timezone>"
            )
            return

        # Pipe syntax (advanced)
        if "|" in raw:
            try:
                name, cron_expr, period_type, timezone_name = self._parse_rule_spec_pipe(raw)
                CronTrigger.from_crontab(cron_expr, timezone=timezone_name)
            except Exception as exc:  # noqa: BLE001
                yield event.plain_result(f"规则格式错误：{exc}")
                return
        else:
            # Natural language: "<name> <schedule> [period_type]"
            try:
                name, cron_expr, period_type, timezone_name = self._parse_rule_spec_natural(raw)
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
                "session_id": self._scoped_session(event),
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
            f"已添加规则 [{rule['rule_id'][:8]}] {rule['rule_name']}\n"
            f"Cron: {cron_expr}\n"
            f"周期: {period_type}\n"
            f"下次触发: {next_run}"
        )

    def _parse_rule_spec_pipe(self, spec: str) -> tuple[str, str, str, str]:
        """Parse pipe-separated rule spec: 名称 | cron | period | timezone"""
        parts = [part.strip() for part in spec.split("|")]
        if len(parts) not in {3, 4}:
            raise ValueError("应为 名称 | cron | day|week|month|custom:7 | timezone(可选)")
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

    def _parse_rule_spec_natural(self, spec: str) -> tuple[str, str, str, str]:
        """Parse natural language rule spec: 日报 每天22点 [period_type]"""
        raw = spec.strip()
        if not raw:
            raise ValueError("请提供名称和时间，例如：日报 每天22点")
        parts = raw.split(None, 1)
        if len(parts) < 2:
            raise ValueError("请提供名称和时间，例如：日报 每天22点")
        name, remainder = parts[0], parts[1].strip()
        explicit_period = None
        period_match = re.search(
            r"\s+(day|week|month|custom:\d+)\s*$",
            remainder,
            flags=re.IGNORECASE,
        )
        if period_match:
            explicit_period = period_match.group(1).lower()
            schedule_text = remainder[:period_match.start()].strip()
        else:
            schedule_text = remainder
        if not schedule_text:
            raise ValueError("请提供有效的时间表达式，例如：每天22点或每天 22点")
        cron_expr, inferred_period = self.service.parse_natural_schedule(
            re.sub(r"\s+", "", schedule_text)
        )
        period_type = explicit_period or inferred_period
        timezone_name = self.service.get_default_timezone()
        return name, cron_expr, period_type, timezone_name

    @time_rule.command("dl", alias={"del", "delete"})
    async def time_rule_delete(self, event: AstrMessageEvent, rule_id_prefix: str):
        """删除定时总结规则。"""
        rule = await self._get_rule_for_session(event, rule_id_prefix)
        if not rule:
            yield event.plain_result("没有找到匹配的规则 ID，请提供更完整的前缀。")
            return
        await self.storage.delete_summary_rule(rule["rule_id"])
        await self.scheduler.reload_rules()
        yield event.plain_result(f"已删除规则 {rule['rule_id'][:8]}。")

    @time_rule.command("rn", alias={"run"})
    async def time_rule_run(self, event: AstrMessageEvent, rule_id_prefix: str):
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

    @time_group.command("st", alias={"status"})
    async def time_status(self, event: AstrMessageEvent):
        """查看插件状态。"""
        record_count = await self.storage.count_records(self._scoped_session(event))
        rules = await self.storage.list_summary_rules(self._scoped_session(event))
        timer = await self.storage.get_active_timer(self._scoped_session(event))
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
        if timer:
            lines.append(f"当前计时: {self.service.format_record_line(timer)}")
        yield event.plain_result("\n".join(lines))

    # ---- auto-record listener ----

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def auto_record_listener(self, event: AstrMessageEvent):
        """新增记录入口已统一为 /ta，这里不再拦截普通消息。"""
        return
