from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Context
from astrbot.core.provider.provider import EmbeddingProvider, Provider, RerankProvider

from .storage import LyubishchevStorage


PLUGIN_NAME = "astrbot_plugin_lyubishchev"
POSITIVE_FEEDBACK_KEYWORDS = (
    "学习",
    "阅读",
    "看书",
    "论文",
    "复习",
    "刷题",
    "实验",
    "工作",
    "开会",
    "写代码",
    "编码",
    "开发",
    "写日报",
    "写周报",
    "写总结",
    "整理",
    "复盘",
    "锻炼",
    "运动",
    "跑步",
    "健身",
)
WASTE_FEEDBACK_KEYWORDS = (
    "摸鱼",
    "刷手机",
    "刷视频",
    "短视频",
    "发呆",
    "摆烂",
    "躺平",
    "打游戏",
    "游戏",
    "追剧",
    "闲逛",
    "水群",
    "浪费",
)
COMFORT_FEEDBACK_KEYWORDS = (
    "累",
    "疲惫",
    "难过",
    "不开心",
    "烦",
    "焦虑",
    "崩溃",
    "委屈",
    "失落",
    "低落",
    "难受",
    "沮丧",
    "心烦",
    "烦躁",
    "emo",
    "压抑",
)
RELATIVE_DATE_OFFSETS = {
    "今天": 0,
    "昨日": -1,
    "昨天": -1,
    "前天": -2,
    "明天": 1,
}
ABSOLUTE_DATE_RE = re.compile(r"(?P<year>\d{4})[-/](?P<month>\d{1,2})[-/](?P<day>\d{1,2})")
RELATIVE_DATE_RE = re.compile("|".join(re.escape(key) for key in RELATIVE_DATE_OFFSETS))
TIME_RANGE_RE = re.compile(r"(?P<start>\d{1,2}:\d{2})\s*[-~至到—–]\s*(?P<end>\d{1,2}:\d{2})")
DURATION_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>小时|小時|分钟|分鐘|h|hr|hrs|min|m)(?:\b|$)",
    flags=re.IGNORECASE,
)


@dataclass
class ParsedRecordInput:
    raw_text: str
    normalized_text: str
    record_kind: str
    record_date: str
    started_at: str | None
    ended_at: str | None
    duration_minutes: int | None
    category: str | None
    project: str | None
    tags: list[str]
    parser_confidence: float
    parser_notes: str


class LyubishchevService:
    def __init__(
        self,
        *,
        context: Context,
        config: AstrBotConfig,
        storage: LyubishchevStorage,
    ) -> None:
        self.context = context
        self.config = config
        self.storage = storage

    def get_default_timezone(self) -> str:
        tz = str(self.config.get("default_timezone", "Asia/Shanghai") or "Asia/Shanghai")
        try:
            ZoneInfo(tz)
        except Exception:
            logger.warning("Invalid timezone %s, fallback to Asia/Shanghai", tz)
            tz = "Asia/Shanghai"
        return tz

    def get_auto_record_prefixes(self) -> list[str]:
        raw = str(self.config.get("auto_record_prefixes", "") or "")
        return [line.strip() for line in raw.splitlines() if line.strip()]

    def auto_record_require_wake(self) -> bool:
        return bool(self.config.get("auto_record_require_wake", True))

    def query_answer_with_llm(self) -> bool:
        return bool(self.config.get("query_answer_with_llm", True))

    def summary_with_advice(self) -> bool:
        return bool(self.config.get("summary_with_advice", True))

    def max_query_candidates(self) -> int:
        return max(int(self.config.get("max_query_candidates", 8) or 8), 1)

    def similarity_threshold(self) -> float:
        try:
            return float(self.config.get("vector_similarity_threshold", 0.2) or 0.2)
        except (TypeError, ValueError):
            return 0.2

    def summary_prompt_appendix(self) -> str:
        return str(self.config.get("summary_prompt_appendix", "") or "").strip()

    def record_feedback_enabled(self) -> bool:
        return bool(self.config.get("record_feedback_enabled", True))

    def record_feedback_max_recent_records(self) -> int:
        try:
            return max(1, min(int(self.config.get("record_feedback_max_recent_records", 6) or 6), 20))
        except (TypeError, ValueError):
            return 6

    def record_feedback_max_recent_chats(self) -> int:
        try:
            return max(0, min(int(self.config.get("record_feedback_max_recent_chats", 6) or 6), 20))
        except (TypeError, ValueError):
            return 6

    def record_feedback_prompt_appendix(self) -> str:
        return str(self.config.get("record_feedback_prompt_appendix", "") or "").strip()

    def _now(self, timezone_name: str | None = None) -> datetime:
        return datetime.now(ZoneInfo(timezone_name or self.get_default_timezone()))

    def parse_record_text(
        self,
        text: str,
        *,
        now: datetime | None = None,
    ) -> ParsedRecordInput:
        now = now or self._now()
        working = self._squash_spaces(text)
        raw_text = working
        tags = sorted(set(re.findall(r"#([\w\-\u4e00-\u9fff]+)", working)))
        working = self._squash_spaces(re.sub(r"#([\w\-\u4e00-\u9fff]+)", "", working))

        category = self._extract_inline_label(working, ["分类", "类别", "category"])
        if category:
            working = self._remove_inline_label(working, ["分类", "类别", "category"])

        project = self._extract_inline_label(working, ["项目", "project"])
        if project:
            working = self._remove_inline_label(working, ["项目", "project"])

        record_kind = "actual"
        kind = self._extract_inline_label(working, ["类型", "kind"])
        if kind:
            working = self._remove_inline_label(working, ["类型", "kind"])
            if kind.lower() in {"plan", "计划"}:
                record_kind = "plan"
        elif working.startswith("计划 "):
            record_kind = "plan"
            working = working[3:].strip()

        record_date = self._parse_record_date(raw_text, now)
        working = self._remove_record_date_tokens(working)
        started_at = None
        ended_at = None
        duration_minutes = None
        parser_confidence = 0.6
        parser_notes: list[str] = []

        range_match = TIME_RANGE_RE.search(working)
        if range_match:
            try:
                start_time = datetime.strptime(range_match.group("start"), "%H:%M").time()
                end_time = datetime.strptime(range_match.group("end"), "%H:%M").time()
            except ValueError as exc:
                raise ValueError("时间段格式无效，请使用类似 09:00-10:30 的写法。") from exc
            base_date = datetime.fromisoformat(f"{record_date}T00:00:00")
            start_dt = datetime.combine(base_date.date(), start_time, now.tzinfo)
            end_dt = datetime.combine(base_date.date(), end_time, now.tzinfo)
            if end_dt <= start_dt:
                end_dt += timedelta(days=1)
                parser_notes.append("检测到跨天时间段。")
            started_at = start_dt.isoformat()
            ended_at = end_dt.isoformat()
            duration_minutes = int((end_dt - start_dt).total_seconds() // 60)
            working = self._squash_spaces(working[: range_match.start()] + working[range_match.end() :])
            parser_confidence = 0.95
        else:
            duration_match = DURATION_RE.search(working)
            if duration_match:
                value = float(duration_match.group("value"))
                unit = duration_match.group("unit").lower()
                if unit in {"小时", "小時", "h", "hr", "hrs"}:
                    duration_minutes = int(round(value * 60))
                else:
                    duration_minutes = int(round(value))
                working = self._squash_spaces(
                    working[: duration_match.start()] + working[duration_match.end() :]
                )
                parser_confidence = 0.82
            else:
                parser_notes.append("未识别到明确时间段或时长，建议后续补充。")

        normalized_text = self._clean_record_text(working)
        if not normalized_text:
            normalized_text = raw_text

        return ParsedRecordInput(
            raw_text=raw_text,
            normalized_text=normalized_text,
            record_kind=record_kind,
            record_date=record_date,
            started_at=started_at,
            ended_at=ended_at,
            duration_minutes=duration_minutes,
            category=category,
            project=project,
            tags=tags,
            parser_confidence=parser_confidence,
            parser_notes=" ".join(parser_notes).strip(),
        )

    def _extract_inline_label(self, text: str, keys: list[str]) -> str | None:
        for key in keys:
            match = re.search(rf"{re.escape(key)}\s*[:=：]\s*([^\s#]+)", text, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return None

    def _remove_inline_label(self, text: str, keys: list[str]) -> str:
        output = text
        for key in keys:
            output = re.sub(
                rf"{re.escape(key)}\s*[:=：]\s*[^\s#]+",
                "",
                output,
                flags=re.IGNORECASE,
            )
        return self._squash_spaces(output)

    def _squash_spaces(self, text: str) -> str:
        return re.sub(r"\s+", " ", text.strip())

    def _remove_record_date_tokens(self, text: str) -> str:
        output = ABSOLUTE_DATE_RE.sub("", text)
        output = RELATIVE_DATE_RE.sub("", output)
        return self._squash_spaces(output)

    def _clean_record_text(self, text: str) -> str:
        cleaned = self._remove_record_date_tokens(text)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" ，,;；。")
        return cleaned

    def _parse_record_date(self, text: str, now: datetime) -> str:
        absolute_match = ABSOLUTE_DATE_RE.search(text)
        if absolute_match:
            year = int(absolute_match.group("year"))
            month = int(absolute_match.group("month"))
            day = int(absolute_match.group("day"))
            try:
                return date(year, month, day).isoformat()
            except ValueError as exc:
                raise ValueError("日期格式无效，请使用类似 2026-03-24 的写法。") from exc
        matches = [match.group(0) for match in RELATIVE_DATE_RE.finditer(text)]
        keywords = list(dict.fromkeys(matches))
        if len(keywords) > 1:
            raise ValueError("检测到多个相对日期，请只保留一个，例如今天或昨天。")
        if keywords:
            return (now.date() + timedelta(days=RELATIVE_DATE_OFFSETS[keywords[0]])).isoformat()
        return now.date().isoformat()

    async def create_record(
        self,
        *,
        session_id: str,
        platform_id: str,
        sender_id: str,
        sender_name: str,
        text: str,
        source: str,
    ) -> dict[str, Any]:
        now = self._now()
        parsed = self.parse_record_text(text, now=now)
        payload = {
            "session_id": session_id,
            "platform_id": platform_id,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "record_kind": parsed.record_kind,
            "record_date": parsed.record_date,
            "raw_text": parsed.raw_text,
            "normalized_text": parsed.normalized_text,
            "started_at": parsed.started_at,
            "ended_at": parsed.ended_at,
            "duration_minutes": parsed.duration_minutes,
            "category": parsed.category,
            "project": parsed.project,
            "tags": parsed.tags,
            "source": source,
            "parser_confidence": parsed.parser_confidence,
            "parser_notes": parsed.parser_notes,
            "status": "active",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "deleted_at": None,
        }
        try:
            record = await self.storage.add_record(payload)
            await self.refresh_record_memory(record)
        except sqlite3.Error as exc:
            logger.error(
                "Failed to create record for session %s: %s",
                session_id,
                exc,
                exc_info=True,
            )
            raise RuntimeError("记录保存失败，请稍后重试。") from exc
        return record

    async def amend_record(self, record: dict[str, Any], new_text: str) -> dict[str, Any] | None:
        now = self._now()
        parsed = self.parse_record_text(new_text, now=now)
        updated = await self.storage.amend_record(
            record["record_id"],
            {
                "record_kind": parsed.record_kind,
                "record_date": parsed.record_date,
                "raw_text": parsed.raw_text,
                "normalized_text": parsed.normalized_text,
                "started_at": parsed.started_at,
                "ended_at": parsed.ended_at,
                "duration_minutes": parsed.duration_minutes,
                "category": parsed.category,
                "project": parsed.project,
                "tags": parsed.tags,
                "parser_confidence": parsed.parser_confidence,
                "parser_notes": parsed.parser_notes,
                "updated_at": now.isoformat(),
                "deleted_at": None,
                "status": "active",
            },
        )
        if updated:
            await self.refresh_record_memory(updated)
        return updated

    async def delete_record(self, record: dict[str, Any]) -> dict[str, Any] | None:
        deleted = await self.storage.soft_delete_record(
            record["record_id"],
            deleted_at=self._now().isoformat(),
        )
        await self.storage.delete_memory_chunk("record", record["record_id"])
        return deleted

    async def refresh_record_memory(self, record: dict[str, Any]) -> None:
        chunk_text = self.render_record_for_memory(record)
        embedding_provider = self.get_embedding_provider()
        embedding: list[float] | None = None
        provider_id: str | None = None
        if embedding_provider:
            provider_id = embedding_provider.meta().id
            try:
                embedding = await embedding_provider.get_embedding(chunk_text)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Embedding refresh failed for record %s: %s", record["record_id"], exc)
        now = self._now().isoformat()
        await self.storage.upsert_memory_chunk(
            {
                "session_id": record["session_id"],
                "source_type": "record",
                "source_id": record["record_id"],
                "content": chunk_text,
                "metadata": {
                    "record_date": record["record_date"],
                    "duration_minutes": record.get("duration_minutes"),
                    "tags": record.get("tags", []),
                    "record_kind": record.get("record_kind", "actual"),
                },
                "embedding_provider_id": provider_id,
                "embedding": embedding,
                "created_at": now,
                "updated_at": now,
            }
        )

    async def refresh_summary_memory(self, summary: dict[str, Any]) -> None:
        embedding_provider = self.get_embedding_provider()
        embedding: list[float] | None = None
        provider_id: str | None = None
        content = (
            f"{summary['title']}\n"
            f"周期: {summary['period_start']} -> {summary['period_end']}\n"
            f"{summary['content']}"
        )
        if embedding_provider:
            provider_id = embedding_provider.meta().id
            try:
                embedding = await embedding_provider.get_embedding(content)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Embedding refresh failed for summary %s: %s", summary["summary_id"], exc)
        await self.storage.upsert_memory_chunk(
            {
                "session_id": summary["session_id"],
                "source_type": "summary",
                "source_id": summary["summary_id"],
                "content": content,
                "metadata": {
                    "summary_type": summary["summary_type"],
                    "period_start": summary["period_start"],
                    "period_end": summary["period_end"],
                },
                "embedding_provider_id": provider_id,
                "embedding": embedding,
                "created_at": summary["created_at"],
                "updated_at": summary["created_at"],
            }
        )

    def render_record_for_memory(self, record: dict[str, Any]) -> str:
        parts = [
            f"记录ID: {record['record_id']}",
            f"日期: {record['record_date']}",
            f"类型: {'计划' if record.get('record_kind') == 'plan' else '实际'}",
        ]
        if record.get("started_at") and record.get("ended_at"):
            start_dt = datetime.fromisoformat(record["started_at"])
            end_dt = datetime.fromisoformat(record["ended_at"])
            parts.append(f"时段: {start_dt.strftime('%H:%M')} - {end_dt.strftime('%H:%M')}")
        if record.get("duration_minutes") is not None:
            parts.append(f"时长: {record['duration_minutes']} 分钟")
        if record.get("category"):
            parts.append(f"分类: {record['category']}")
        if record.get("project"):
            parts.append(f"项目: {record['project']}")
        if record.get("tags"):
            parts.append("标签: " + ", ".join(record["tags"]))
        parts.append(f"内容: {record['normalized_text']}")
        return "\n".join(parts)

    def format_record_line(self, record: dict[str, Any]) -> str:
        short_id = record["record_id"][:8]
        head = f"[{short_id}] {record['record_date']}"
        if record.get("started_at") and record.get("ended_at"):
            start_dt = datetime.fromisoformat(record["started_at"])
            end_dt = datetime.fromisoformat(record["ended_at"])
            head += f" {start_dt.strftime('%H:%M')}-{end_dt.strftime('%H:%M')}"
        if record.get("duration_minutes") is not None:
            head += f" ({record['duration_minutes']}m)"
        tail = record.get("normalized_text") or record.get("raw_text") or ""
        if record.get("tags"):
            tail += " " + " ".join(f"#{tag}" for tag in record["tags"])
        return f"{head} {tail}".strip()

    def format_record_detail(self, record: dict[str, Any], revisions: list[dict[str, Any]]) -> str:
        lines = [
            f"记录ID: {record['record_id']}",
            f"日期: {record['record_date']}",
            f"类型: {'计划' if record.get('record_kind') == 'plan' else '实际'}",
            f"状态: {record.get('status', 'active')}",
            f"原始文本: {record.get('raw_text', '')}",
            f"解析内容: {record.get('normalized_text', '')}",
        ]
        if record.get("started_at") and record.get("ended_at"):
            lines.append(f"时段: {record['started_at']} -> {record['ended_at']}")
        if record.get("duration_minutes") is not None:
            lines.append(f"时长: {record['duration_minutes']} 分钟")
        if record.get("category"):
            lines.append(f"分类: {record['category']}")
        if record.get("project"):
            lines.append(f"项目: {record['project']}")
        if record.get("tags"):
            lines.append("标签: " + ", ".join(record["tags"]))
        if record.get("parser_notes"):
            lines.append(f"解析提示: {record['parser_notes']}")
        lines.append(f"修订次数: {len(revisions)}")
        return "\n".join(lines)

    def get_chat_provider(self, session_id: str | None = None) -> Provider | None:
        provider_id = str(self.config.get("analysis_provider_id", "") or "").strip()
        provider = None
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
        elif session_id:
            provider = self.context.get_using_provider(umo=session_id)
        if provider and isinstance(provider, Provider):
            return provider
        return None

    def get_embedding_provider(self) -> EmbeddingProvider | None:
        provider_id = str(self.config.get("embedding_provider_id", "") or "").strip()
        if not provider_id:
            return None
        provider = self.context.get_provider_by_id(provider_id)
        if provider and isinstance(provider, EmbeddingProvider):
            return provider
        return None

    def get_rerank_provider(self) -> RerankProvider | None:
        provider_id = str(self.config.get("rerank_provider_id", "") or "").strip()
        if not provider_id:
            return None
        provider = self.context.get_provider_by_id(provider_id)
        if provider and isinstance(provider, RerankProvider):
            return provider
        return None

    def get_record_feedback_provider(self, session_id: str) -> Provider | None:
        provider_id = str(self.config.get("record_feedback_provider_id", "") or "").strip()
        provider = None
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
        else:
            provider = self.context.get_using_provider(umo=session_id)
            if provider is None:
                provider = self.get_chat_provider(session_id)
        if provider and isinstance(provider, Provider):
            return provider
        return None

    def get_period_bounds(
        self,
        period_type: str,
        *,
        custom_days: int | None = None,
        now: datetime | None = None,
    ) -> tuple[date, date]:
        now = now or self._now()
        today = now.date()
        if period_type == "day":
            return today, today
        if period_type == "week":
            start = today - timedelta(days=today.weekday())
            return start, today
        if period_type == "month":
            start = today.replace(day=1)
            return start, today
        if period_type == "custom":
            days = max(custom_days or 1, 1)
            return today - timedelta(days=days - 1), today
        raise ValueError(f"Unsupported period_type: {period_type}")

    async def build_summary_snapshot(
        self,
        *,
        session_id: str,
        summary_type: str,
        start_date: date,
        end_date: date,
        with_advice: bool | None = None,
    ) -> dict[str, Any] | None:
        records = await self.storage.list_records(
            session_id,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            limit=500,
        )
        if not records:
            return None
        advice_flag = self.summary_with_advice() if with_advice is None else with_advice
        stats = self._build_stats(records)
        factual_summary = self._render_summary_text_fallback(
            summary_type=summary_type,
            start_date=start_date,
            end_date=end_date,
            records=records,
            stats=stats,
            with_advice=advice_flag,
        )
        return {
            "summary_type": summary_type,
            "start_date": start_date,
            "end_date": end_date,
            "records": records,
            "stats": stats,
            "factual_summary": factual_summary,
            "with_advice": advice_flag,
        }

    async def generate_summary(
        self,
        *,
        session_id: str,
        summary_type: str,
        start_date: date,
        end_date: date,
        rule_id: str | None = None,
        with_advice: bool | None = None,
    ) -> dict[str, Any] | None:
        snapshot = await self.build_summary_snapshot(
            session_id=session_id,
            summary_type=summary_type,
            start_date=start_date,
            end_date=end_date,
            with_advice=with_advice,
        )
        if not snapshot:
            return None

        records = snapshot["records"]
        stats = snapshot["stats"]
        title = f"{summary_type.upper()} 总结 {start_date.isoformat()} -> {end_date.isoformat()}"
        content = await self._render_summary_text(
            session_id=session_id,
            summary_type=summary_type,
            start_date=start_date,
            end_date=end_date,
            records=records,
            stats=stats,
            with_advice=bool(snapshot["with_advice"]),
        )
        created_at = self._now().isoformat()
        summary = await self.storage.add_summary(
            {
                "session_id": session_id,
                "rule_id": rule_id,
                "summary_type": summary_type,
                "period_start": start_date.isoformat(),
                "period_end": end_date.isoformat(),
                "title": title,
                "content": content,
                "stats": stats,
                "created_at": created_at,
            }
        )
        await self.refresh_summary_memory(summary)
        return summary

    def _build_stats(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        total_minutes = 0
        missing_duration = 0
        category_counter: defaultdict[str, int] = defaultdict(int)
        tag_counter: defaultdict[str, int] = defaultdict(int)
        project_counter: defaultdict[str, int] = defaultdict(int)
        kind_counter: Counter[str] = Counter()

        for record in records:
            kind_counter.update([record.get("record_kind", "actual")])
            minutes = record.get("duration_minutes")
            if minutes is None:
                missing_duration += 1
            else:
                total_minutes += int(minutes)
                if record.get("category"):
                    category_counter[str(record["category"])] += int(minutes)
                if record.get("project"):
                    project_counter[str(record["project"])] += int(minutes)
                for tag in record.get("tags", []):
                    tag_counter[str(tag)] += int(minutes)

        return {
            "record_count": len(records),
            "total_minutes": total_minutes,
            "missing_duration_count": missing_duration,
            "kind_distribution": dict(kind_counter),
            "category_minutes": dict(sorted(category_counter.items(), key=lambda item: item[1], reverse=True)),
            "project_minutes": dict(sorted(project_counter.items(), key=lambda item: item[1], reverse=True)),
            "tag_minutes": dict(sorted(tag_counter.items(), key=lambda item: item[1], reverse=True)),
        }

    async def _render_summary_text(
        self,
        *,
        session_id: str,
        summary_type: str,
        start_date: date,
        end_date: date,
        records: list[dict[str, Any]],
        stats: dict[str, Any],
        with_advice: bool,
    ) -> str:
        factual_summary = self._render_summary_text_fallback(
            summary_type=summary_type,
            start_date=start_date,
            end_date=end_date,
            records=records,
            stats=stats,
            with_advice=with_advice,
        )
        try:
            bot_summary = await self._render_summary_text_with_astrbot(
                session_id=session_id,
                summary_type=summary_type,
                start_date=start_date,
                end_date=end_date,
                records=records,
                factual_summary=factual_summary,
            )
            if bot_summary:
                return bot_summary
        except Exception as exc:  # noqa: BLE001
            logger.warning("Summary AstrBot reply failed, fallback to deterministic summary: %s", exc)
        return factual_summary

    async def _render_summary_text_with_astrbot(
        self,
        *,
        session_id: str,
        summary_type: str,
        start_date: date,
        end_date: date,
        records: list[dict[str, Any]],
        factual_summary: str,
    ) -> str | None:
        record_lines = "\n".join(
            f"- {self.format_record_line(record)}" for record in records[:20]
        ) or "无"
        prompt = (
            f"你刚收到一份柳比歇夫时间记录统计，请基于事实给用户做总结反馈。\n"
            f"周期类型: {summary_type}\n"
            f"统计区间: {start_date.isoformat()} -> {end_date.isoformat()}\n\n"
            f"事实底稿:\n{factual_summary}\n\n"
            f"最近记录样本:\n{record_lines}\n"
        )
        system_prompt = (
            "你是当前会话中的 AstrBot，需要根据用户已经记录好的时间账本做一段总结反馈。\n"
            "请保持当前机器人的说话风格，但必须严格依据给定事实，不要编造不存在的时间安排。\n"
            "输出中文，适合直接发给用户。\n"
            "建议结构是：先说整体观察，再说时间分配特点，最后给1到3条可执行建议。\n"
            "可以有一点自然的反应感，但不要过度脑补用户心情，不要写成生硬公文。\n"
            "优先使用 3 到 6 段自然段表达，不要使用 Markdown 标题、列表或 emoji。"
        )
        if appendix := self.summary_prompt_appendix():
            system_prompt += appendix
        return await self._generate_current_bot_reply(
            session_id=session_id,
            prompt=prompt,
            system_prompt=system_prompt,
            preserve_newlines=True,
        )

    async def _render_summary_text_with_llm(
        self,
        *,
        provider: Provider,
        summary_type: str,
        start_date: date,
        end_date: date,
        records: list[dict[str, Any]],
        stats: dict[str, Any],
        with_advice: bool,
    ) -> str:
        record_lines = "\n".join(
            f"- {self.format_record_line(record)}" for record in records[:80]
        )
        prompt = (
            f"周期类型: {summary_type}\n"
            f"统计区间: {start_date.isoformat()} -> {end_date.isoformat()}\n"
            f"聚合统计: {json.dumps(stats, ensure_ascii=False)}\n"
            f"记录列表:\n{record_lines}\n"
        )
        system_prompt = (
            "你是柳比歇夫时间管理助手。"
            "请严格基于用户给出的真实时间记录做总结，不要编造未出现的时间事实。"
            "输出中文，结构清晰，适合直接发给用户。"
            "至少包含：总体概览、时间分配、记录质量观察。"
        )
        if with_advice:
            system_prompt += "最后追加 2-3 条可执行的时间管理建议。"
        if appendix := self.summary_prompt_appendix():
            system_prompt += appendix
        response = await provider.text_chat(prompt=prompt, system_prompt=system_prompt)
        return response.completion_text.strip()

    def _render_summary_text_fallback(
        self,
        *,
        summary_type: str,
        start_date: date,
        end_date: date,
        records: list[dict[str, Any]],
        stats: dict[str, Any],
        with_advice: bool,
    ) -> str:
        lines = [
            f"{summary_type.upper()} 总结",
            f"统计区间: {start_date.isoformat()} -> {end_date.isoformat()}",
            f"记录数: {stats['record_count']}",
            f"可统计总时长: {stats['total_minutes']} 分钟",
        ]
        if stats["category_minutes"]:
            lines.append("分类分布:")
            for key, minutes in list(stats["category_minutes"].items())[:5]:
                lines.append(f"- {key}: {minutes} 分钟")
        if stats["tag_minutes"]:
            lines.append("标签分布:")
            for key, minutes in list(stats["tag_minutes"].items())[:5]:
                lines.append(f"- #{key}: {minutes} 分钟")
        if stats["missing_duration_count"]:
            lines.append(
                f"有 {stats['missing_duration_count']} 条记录缺少明确时长，建议后续补齐。"
            )
        lines.append("最近记录:")
        for record in records[:5]:
            lines.append(f"- {self.format_record_line(record)}")
        if with_advice:
            lines.append("建议:")
            lines.extend(self._build_advice(stats))
        return "\n".join(lines)

    def _build_advice(self, stats: dict[str, Any]) -> list[str]:
        advice: list[str] = []
        if stats["missing_duration_count"]:
            advice.append("- 尽量在记录时写清时间段或时长，后续总结会更准确。")
        category_minutes = stats.get("category_minutes", {})
        if category_minutes:
            top_name, top_minutes = next(iter(category_minutes.items()))
            total = stats.get("total_minutes", 0) or 1
            if top_minutes / total >= 0.6:
                advice.append(f"- 当前 {top_name} 占用时间明显偏高，建议检查是否挤压了其他关键事项。")
        if stats.get("record_count", 0) < 3:
            advice.append("- 记录密度偏低，可以把碎片事项也纳入记录，后续复盘会更完整。")
        if not advice:
            advice.append("- 继续保持稳定记录，后续可以按项目或标签细化统计口径。")
        return advice[:3]

    def _feedback_signal_summary_reference(
        self,
        *,
        records: list[dict[str, Any]],
        recent_records: list[dict[str, Any]],
        recent_chat_lines: list[str],
    ) -> str:
        current_text = " ".join(self._collect_feedback_text_fragments(records))
        recent_record_text = " ".join(self._collect_feedback_text_fragments(recent_records[:6]))
        recent_chat_text = " ".join(recent_chat_lines)
        positive_hits = self._collect_feedback_keyword_hits(current_text, POSITIVE_FEEDBACK_KEYWORDS)
        waste_hits = self._collect_feedback_keyword_hits(current_text, WASTE_FEEDBACK_KEYWORDS)
        comfort_hits = self._collect_feedback_keyword_hits(
            f"{current_text} {recent_chat_text}",
            COMFORT_FEEDBACK_KEYWORDS,
        )
        lines = ["可能成立的判断信号如下，只有证据够时才使用，不要硬编："]
        if positive_hits:
            lines.append("- 正向推进信号: " + "、".join(positive_hits))
        if waste_hits:
            lines.append("- 容易被吐槽的拖延/消磨信号: " + "、".join(waste_hits))
        if comfort_hits:
            lines.append("- 最近可能有点累或情绪不太好的信号: " + "、".join(comfort_hits))
        if recent_record_text:
            lines.append("- 最近时间记录也能帮助判断节奏是否连续。")
        if len(lines) == 1:
            lines.append("- 暂时没有特别明显的情绪或倾向信号，就正常聊天回应。")
        return "\n".join(lines)

    async def _generate_record_feedback_reference(
        self,
        *,
        session_id: str,
        record: dict[str, Any],
    ) -> str | None:
        if not self.record_feedback_enabled():
            return None

        recent_records = await self.storage.list_records(
            session_id,
            limit=max(self.record_feedback_max_recent_records() + 1, 2),
        )
        recent_records = [item for item in recent_records if item["record_id"] != record["record_id"]]
        recent_records = recent_records[: self.record_feedback_max_recent_records()]
        recent_chat_lines = await self._get_recent_chat_lines(
            session_id=session_id,
            limit=self.record_feedback_max_recent_chats(),
        )

        prompt = (
            f"用户刚录入了一条新的时间记录，请你做一个短反馈。\n"
            f"新记录:\n{self.render_record_for_memory(record)}\n\n"
            f"最近聊天摘录:\n"
            + ("\n".join(f"- {line}" for line in recent_chat_lines) or "无")
            + "\n\n最近时间记录:\n"
            + (
                "\n".join(
                    f"- {self.format_record_line(item)}"
                    for item in recent_records[: self.record_feedback_max_recent_records()]
                )
                or "无"
            )
        )
        system_prompt = (
            "你是当前会话中的 AstrBot。用户刚记了一条时间账，请给出很短的反馈。\n"
            "要求：\n"
            "1. 输出中文，2到3句，总长度控制在30到90字。\n"
            "2. 先自然回应这条记录，再给1条简短建议。\n"
            "3. 可以参考最近聊天和最近记录，但不能编造事实。\n"
            "4. 不要过度脑补用户情绪、生活状态或关系氛围。\n"
            "5. 不要写标题、列表、emoji，也不要机械复述原文。"
        )
        if appendix := self.record_feedback_prompt_appendix():
            system_prompt += appendix
        bot_feedback = await self._generate_current_bot_reply(
            session_id=session_id,
            prompt=prompt,
            system_prompt=system_prompt,
        )
        if bot_feedback:
            return bot_feedback

        provider = self.get_record_feedback_provider(session_id)
        if provider:
            try:
                return await self._generate_record_feedback_with_llm(
                    provider=provider,
                    record=record,
                    recent_records=recent_records,
                    recent_chat_lines=recent_chat_lines,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Record feedback generation failed, fallback to template: %s", exc)
        return self._generate_record_feedback_fallback(record, recent_records)

    async def _generate_record_feedback_with_llm_reference(
        self,
        *,
        provider: Provider,
        record: dict[str, Any],
        recent_records: list[dict[str, Any]],
        recent_chat_lines: list[str],
    ) -> str | None:
        recent_records_block = "\n".join(
            f"- {self.format_record_line(item)}" for item in recent_records[: self.record_feedback_max_recent_records()]
        ) or "无"
        recent_chat_block = "\n".join(f"- {line}" for line in recent_chat_lines) or "无"
        prompt = (
            f"刚录入的时间记录：\n{self.render_record_for_memory(record)}\n\n"
            f"最近聊天摘录：\n{recent_chat_block}\n\n"
            f"最近时间记录：\n{recent_records_block}\n"
        )
        system_prompt = (
            "你是当前会话里的 AstrBot，现在只需要对用户刚录入的一条时间记录做一个短反馈。\n"
            "你的回复要像正在和熟悉的用户说话，有一点反应感、活人感，但不要过头。\n"
            "严格要求：\n"
            "1. 输出中文，2到3句，总长度控制在30到90字。\n"
            "2. 先对这条记录做自然反应，可以轻微吐槽、夸赞或共情。\n"
            "3. 最后给1条很短、可以立刻执行的建议。\n"
            "4. 可以参考最近聊天语气和最近时间记录，但绝不能编造不存在的事实。\n"
            "5. 不要过度脑补用户的情绪、生活状态或关系氛围。\n"
            "6. 不要使用标题、列表、引号、emoji，不要写成长总结。\n"
            "7. 不要机械复述整条记录。"
        )
        if appendix := self.record_feedback_prompt_appendix():
            system_prompt += appendix
        response = await provider.text_chat(prompt=prompt, system_prompt=system_prompt)
        completion = self._normalize_feedback_text(response.completion_text)
        return completion or None

    def _collect_feedback_text_fragments(self, records: list[dict[str, Any]]) -> list[str]:
        fragments: list[str] = []
        for record in records:
            for value in (
                record.get("normalized_text"),
                record.get("raw_text"),
                record.get("category"),
                record.get("project"),
            ):
                if isinstance(value, str) and value.strip():
                    fragments.append(value.strip())
            for tag in record.get("tags", []):
                if isinstance(tag, str) and tag.strip():
                    fragments.append(tag.strip())
        return fragments

    def _collect_feedback_keyword_hits(
        self,
        text: str,
        keywords: tuple[str, ...],
        *,
        limit: int = 4,
    ) -> list[str]:
        lowered = text.lower()
        hits: list[str] = []
        for keyword in keywords:
            if keyword.lower() in lowered and keyword not in hits:
                hits.append(keyword)
            if len(hits) >= limit:
                break
        return hits

    def _build_feedback_signal_summary(
        self,
        *,
        records: list[dict[str, Any]],
        recent_records: list[dict[str, Any]],
        recent_chat_lines: list[str],
    ) -> str:
        current_text = " ".join(self._collect_feedback_text_fragments(records))
        recent_record_text = " ".join(self._collect_feedback_text_fragments(recent_records[:6]))
        recent_chat_text = " ".join(recent_chat_lines)
        positive_hits = self._collect_feedback_keyword_hits(current_text, POSITIVE_FEEDBACK_KEYWORDS)
        waste_hits = self._collect_feedback_keyword_hits(current_text, WASTE_FEEDBACK_KEYWORDS)
        comfort_hits = self._collect_feedback_keyword_hits(
            f"{current_text} {recent_chat_text}",
            COMFORT_FEEDBACK_KEYWORDS,
        )
        lines = ["可能成立的判断信号如下，只有证据够时才使用，不要硬编："]
        if positive_hits:
            lines.append("- 正向推进信号: " + ", ".join(positive_hits))
        if waste_hits:
            lines.append("- 容易被吐槽的拖延/消磨信号: " + ", ".join(waste_hits))
        if comfort_hits:
            lines.append("- 最近可能有点累或情绪不太好的信号: " + ", ".join(comfort_hits))
        if recent_record_text:
            lines.append("- 最近时间记录也能帮助判断节奏是否连续。")
        if len(lines) == 1:
            lines.append("- 暂时没有特别明显的情绪或倾向信号，就正常聊天回应。")
        return "\n".join(lines)

    async def generate_record_feedback(
        self,
        *,
        session_id: str,
        record: dict[str, Any],
    ) -> str | None:
        return await self.generate_records_feedback(
            session_id=session_id,
            records=[record],
        )

    async def generate_records_feedback(
        self,
        *,
        session_id: str,
        records: list[dict[str, Any]],
        failures: list[tuple[str, str]] | None = None,
    ) -> str | None:
        if not self.record_feedback_enabled() or not records:
            return None

        failures = failures or []
        exclude_ids = {
            str(record.get("record_id"))
            for record in records
            if record.get("record_id")
        }
        recent_records = await self.storage.list_records(
            session_id,
            limit=max(self.record_feedback_max_recent_records() + len(exclude_ids) + 2, 4),
        )
        recent_records = [
            item for item in recent_records if item.get("record_id") not in exclude_ids
        ][: self.record_feedback_max_recent_records()]
        recent_chat_lines = await self._get_recent_chat_lines(
            session_id=session_id,
            limit=self.record_feedback_max_recent_chats(),
        )

        new_records_block = "\n\n".join(
            self.render_record_for_memory(record) for record in records[:10]
        )
        recent_records_block = "\n".join(
            f"- {self.format_record_line(item)}"
            for item in recent_records[: self.record_feedback_max_recent_records()]
        ) or "暂无"
        recent_chat_block = "\n".join(f"- {line}" for line in recent_chat_lines) or "暂无"
        signal_summary = self._build_feedback_signal_summary(
            records=records,
            recent_records=recent_records,
            recent_chat_lines=recent_chat_lines,
        )
        failure_block = ""
        if failures:
            failure_block = "\n".join(f"- {text} ({reason})" for text, reason in failures[:5])

        prompt = (
            f"用户刚录入了 {len(records)} 条新的时间记录，请你像当前会话里的 AstrBot 一样做一次正常反馈。\n"
            f"新记录如下:\n{new_records_block}\n\n"
            f"{signal_summary}\n\n"
            f"最近聊天摘录:\n{recent_chat_block}\n\n"
            f"最近时间记录:\n{recent_records_block}\n"
        )
        if failure_block:
            prompt += f"\n这次还有几行没有入库成功：\n{failure_block}\n"

        system_prompt = (
            "你是当前会话里的 AstrBot。用户刚记完时间账，现在请你直接像平时聊天那样回他。\n"
            "要求：\n"
            "1. 输出中文，不要写标题、列表、emoji，也不要只回“已记录”。\n"
            "2. 回复可以自然一点，控制在 1 到 2 段、2 到 6 句。\n"
            "3. 先回应这次记录本身，再给一点吐槽、鼓励或提醒。\n"
            "4. 如果这次记录明显是在学习、工作、运动、复盘或推进正事，可以直接夸，语气真诚一点。\n"
            "5. 如果这次记录明显是在摸鱼、拖延、刷手机、打游戏或消磨时间，可以轻轻吐槽，但不要尖刻。\n"
            "6. 如果最近聊天或记录里看得出用户有点累、烦、难过、低落，可以顺手安慰一句；证据不够时只能用谨慎表达，不要硬编。\n"
            "7. 如果是批量录入，优先综合评价整体节奏，再抓 1 到 2 个最明显的点来夸或吐槽。\n"
            "8. 不要机械复述所有时间段，不要假装知道没有记录出来的事情。"
        )
        if appendix := self.record_feedback_prompt_appendix():
            system_prompt += appendix
        bot_feedback = await self._generate_current_bot_reply(
            session_id=session_id,
            prompt=prompt,
            system_prompt=system_prompt,
            preserve_newlines=True,
        )
        if bot_feedback:
            return bot_feedback

        provider = self.get_record_feedback_provider(session_id)
        if provider:
            try:
                return await self._generate_record_feedback_with_llm(
                    provider=provider,
                    records=records,
                    recent_records=recent_records,
                    recent_chat_lines=recent_chat_lines,
                    failures=failures,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Record feedback generation failed, fallback to template: %s", exc)
        return self._generate_record_feedback_fallback(
            records,
            recent_records,
            recent_chat_lines,
        )

    async def _generate_record_feedback_with_llm(
        self,
        *,
        provider: Provider,
        records: list[dict[str, Any]],
        recent_records: list[dict[str, Any]],
        recent_chat_lines: list[str],
        failures: list[tuple[str, str]],
    ) -> str | None:
        new_records_block = "\n\n".join(
            self.render_record_for_memory(record) for record in records[:10]
        )
        recent_records_block = "\n".join(
            f"- {self.format_record_line(item)}"
            for item in recent_records[: self.record_feedback_max_recent_records()]
        ) or "暂无"
        recent_chat_block = "\n".join(f"- {line}" for line in recent_chat_lines) or "暂无"
        signal_summary = self._build_feedback_signal_summary(
            records=records,
            recent_records=recent_records,
            recent_chat_lines=recent_chat_lines,
        )
        prompt = (
            f"新录入的时间记录如下：\n{new_records_block}\n\n"
            f"{signal_summary}\n\n"
            f"最近聊天摘录：\n{recent_chat_block}\n\n"
            f"最近时间记录：\n{recent_records_block}\n"
        )
        if failures:
            prompt += "\n未入库成功的行：\n" + "\n".join(
                f"- {text} ({reason})" for text, reason in failures[:5]
            )
        system_prompt = (
            "你是当前会话里的 AstrBot。现在用户刚记完时间账，你只需要正常回他。\n"
            "回复要求：\n"
            "1. 用中文，1 到 2 段、2 到 6 句，不要标题、列表、emoji。\n"
            "2. 可以比之前更有反应感，可以夸、吐槽、鼓励，也可以带一点熟悉感。\n"
            "3. 学习、工作、运动、推进正事时就夸；明显摸鱼或浪费时间时可以轻轻吐槽并提醒。\n"
            "4. 如果能从最近聊天或本次记录里看出用户有点累、烦、难过、低落，就顺手安慰一句。\n"
            "5. 情绪判断必须基于线索，没把握时用“看起来”“像是”这类谨慎表达。\n"
            "6. 不要机械复述全部记录，不要写成长篇总结，不要编造额外事实。"
        )
        if appendix := self.record_feedback_prompt_appendix():
            system_prompt += appendix
        response = await provider.text_chat(prompt=prompt, system_prompt=system_prompt)
        completion = self._normalize_feedback_text(response.completion_text)
        return completion or None

    async def _get_recent_chat_lines(
        self,
        *,
        session_id: str,
        limit: int,
    ) -> list[str]:
        if limit <= 0:
            return []
        cid = await self.context.conversation_manager.get_curr_conversation_id(session_id)
        if not cid:
            return []
        conversation = await self.context.conversation_manager.get_conversation(session_id, cid)
        if not conversation or not conversation.history:
            return []
        try:
            history = json.loads(conversation.history)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to parse conversation history for record feedback: %s", exc)
            return []

        lines: list[str] = []
        for message in reversed(history):
            role = str(message.get("role", "") or "")
            if role not in {"user", "assistant"}:
                continue
            content = self._extract_message_text(message.get("content"))
            if not content:
                continue
            speaker = "用户" if role == "user" else "AstrBot"
            lines.append(f"{speaker}: {content}")
            if len(lines) >= limit:
                break
        lines.reverse()
        return lines

    def _extract_message_text(self, content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return self._squash_spaces(content)
        if isinstance(content, list):
            parts = [self._extract_message_text(item) for item in content]
            return self._squash_spaces(" ".join(part for part in parts if part))
        if isinstance(content, dict):
            if isinstance(content.get("text"), str):
                return self._squash_spaces(str(content["text"]))
            if isinstance(content.get("content"), str):
                return self._squash_spaces(str(content["content"]))
            nested = content.get("content")
            if isinstance(nested, (list, dict)):
                return self._extract_message_text(nested)
            if content.get("type") == "image_url":
                return "[图片]"
        return ""

    def _normalize_agent_reply(self, text: str, *, preserve_newlines: bool = False) -> str:
        cleaned = text.replace("\r", "\n").strip()
        if not cleaned:
            return ""
        if preserve_newlines:
            paragraphs = []
            for block in re.split(r"\n\s*\n+", cleaned):
                block = re.sub(r"\s+", " ", block).strip()
                if block:
                    paragraphs.append(block)
            return "\n\n".join(paragraphs).strip()
        lines = [line.strip(" -•") for line in cleaned.splitlines() if line.strip()]
        merged = " ".join(lines)
        return re.sub(r"\s+", " ", merged).strip()

    def _normalize_feedback_text(self, text: str) -> str:
        return self._normalize_agent_reply(text, preserve_newlines=True)

    async def _get_or_create_reply_conversation(
        self,
        *,
        session_id: str,
        platform_id: str,
    ) -> Any | None:
        conv_mgr = self.context.conversation_manager
        conversation = None
        cid = await conv_mgr.get_curr_conversation_id(session_id)
        if cid:
            conversation = await conv_mgr.get_conversation(session_id, cid)
        if conversation:
            return conversation
        cid = await conv_mgr.new_conversation(session_id, platform_id)
        if not cid:
            return None
        return await conv_mgr.get_conversation(session_id, cid)

    async def _generate_current_bot_reply(
        self,
        *,
        session_id: str,
        prompt: str,
        system_prompt: str,
        preserve_newlines: bool = False,
    ) -> str | None:
        from astrbot.api.provider import ProviderRequest
        from astrbot.core.astr_main_agent import (
            MainAgentBuildConfig,
            build_main_agent,
        )
        from astrbot.core.cron.events import CronMessageEvent
        from astrbot.core.platform.message_session import MessageSession

        try:
            session = MessageSession.from_str(session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to parse session for AstrBot reply generation: %s", exc)
            return None

        synthetic_event = CronMessageEvent(
            context=self.context,
            session=session,
            message=prompt,
            sender_name="LyubishchevPlugin",
        )
        synthetic_event.platform_meta.support_proactive_message = False

        conversation = await self._get_or_create_reply_conversation(
            session_id=session_id,
            platform_id=synthetic_event.get_platform_id(),
        )
        if not conversation:
            logger.warning("Failed to get conversation for AstrBot reply generation: %s", session_id)
            return None
        try:
            contexts = json.loads(conversation.history or "[]")
            if not isinstance(contexts, list):
                contexts = []
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to parse conversation history for AstrBot reply generation: %s", exc)
            contexts = []
        cfg = self.context.get_config(umo=session_id)
        tool_call_timeout = cfg.get("provider_settings", {}).get("tool_call_timeout", 120)
        req = ProviderRequest(
            prompt=prompt,
            contexts=contexts,
            system_prompt=system_prompt,
            conversation=conversation,
        )
        build_config = MainAgentBuildConfig(
            tool_call_timeout=tool_call_timeout,
            llm_safety_mode=True,
            streaming_response=False,
            add_cron_tools=False,
        )
        result = await build_main_agent(
            event=synthetic_event,
            plugin_context=self.context,
            config=build_config,
            req=req,
        )
        if not result:
            return None
        async for _ in result.agent_runner.step_until_done(20):
            pass
        llm_resp = result.agent_runner.get_final_llm_resp()
        if not llm_resp or llm_resp.role != "assistant":
            return None
        return self._normalize_agent_reply(
            llm_resp.completion_text,
            preserve_newlines=preserve_newlines,
        )

    def _generate_record_feedback_fallback_reference(
        self,
        record: dict[str, Any],
        recent_records: list[dict[str, Any]],
    ) -> str:
        duration = record.get("duration_minutes")
        target = record.get("project") or record.get("category") or record.get("normalized_text") or "这件事"
        has_time_range = bool(record.get("started_at") and record.get("ended_at"))
        same_project_recent = False
        if record.get("project"):
            same_project_recent = any(item.get("project") == record.get("project") for item in recent_records[:5])

        if same_project_recent:
            return f"你这阵子在 {target} 上挺连续的，这种连续记账很有复盘价值。下次顺手补一句产出结果，我后面给建议会更有依据。"
        if duration is not None and duration >= 120:
            return f"这段时间记得很整块，{target} 不是随手碰一下就算了。下次顺手写下阶段产出，后面查账会更有抓手。"
        if duration is not None and duration <= 30:
            return f"这条像个碎片任务，但你能及时记下来就已经很不错。下次尽量补上开始时间，之后回看会更清楚。"
        if has_time_range:
            return f"这条时间段记得挺清楚，时间账本开始有点柳比歇夫那味了。下次再补个项目或结果，我给你的建议会更准。"
        return "先把这条落账是对的，别让时间一转眼就糊掉。下次尽量带上更具体的时长或时段，我后面复盘会更有依据。"

    def _generate_record_feedback_fallback(
        self,
        records: list[dict[str, Any]],
        recent_records: list[dict[str, Any]],
        recent_chat_lines: list[str],
    ) -> str:
        current_text = " ".join(self._collect_feedback_text_fragments(records))
        recent_text = " ".join(self._collect_feedback_text_fragments(recent_records[:6]))
        chat_text = " ".join(recent_chat_lines)
        positive_hits = self._collect_feedback_keyword_hits(current_text, POSITIVE_FEEDBACK_KEYWORDS)
        waste_hits = self._collect_feedback_keyword_hits(current_text, WASTE_FEEDBACK_KEYWORDS)
        comfort_hits = self._collect_feedback_keyword_hits(
            f"{current_text} {chat_text}",
            COMFORT_FEEDBACK_KEYWORDS,
        )
        same_project_recent = False
        current_projects = {
            str(record.get("project"))
            for record in records
            if record.get("project")
        }
        if current_projects:
            same_project_recent = any(
                item.get("project") in current_projects for item in recent_records[:5]
            )

        parts: list[str] = []
        if comfort_hits:
            parts.append("看起来你这阵子多少有点累了，先别把自己拧得太紧。")

        if positive_hits and not waste_hits:
            if len(records) > 1:
                parts.append("这波记录里正事推进得还挺实在，节奏是在线的。")
            else:
                parts.append("这条看着就挺像在认真推进事情，记得不错，也做得不错。")
            if same_project_recent:
                parts.append("而且你最近在这件事上是连续投入的，这种持续性很值钱。")
            else:
                parts.append("继续保持，别小看这种稳稳往前拱的感觉。")
        elif waste_hits and not positive_hits:
            if len(records) > 1:
                parts.append("这波账本一摊开，确实能看出有些时间又被你随手放跑了。")
            else:
                parts.append("这条我得轻轻吐槽你一下，怎么又把时间喂给这种很会偷分钟的事了。")
            parts.append("下次先给自己卡个止损点，别让它一不留神就越滚越大。")
        elif positive_hits and waste_hits:
            parts.append("今天这波记录挺真实，一边有在推进正事，一边也有些时间在偷偷漏。")
            parts.append("好消息是你已经把问题照出来了，下一步就是把容易漏走的那块收一收。")
        else:
            if len(records) > 1:
                parts.append("这一波先完整记下来就是对的，节奏已经比糊成一团强很多了。")
            else:
                parts.append("先把这条落账是对的，能记下来就比让它直接糊过去强。")
            parts.append("后面要是顺手再补一点结果或感受，复盘会更有抓手。")

        if recent_text and len(parts) < 3:
            parts.append("你最近这几条放在一起看，已经能慢慢看出自己的时间习惯了。")
        return " ".join(parts[:3]).strip()

    async def query_memory(
        self,
        *,
        session_id: str,
        question: str,
        with_llm: bool | None = None,
    ) -> dict[str, Any]:
        limit = self.max_query_candidates()
        text_hits = await self.storage.search_memory_chunks_text(
            session_id,
            question,
            limit=limit,
        )
        combined: dict[str, dict[str, Any]] = {}
        for hit in text_hits:
            hit["score"] = 0.45
            combined[hit["chunk_id"]] = hit

        embedding_provider = self.get_embedding_provider()
        if embedding_provider:
            try:
                embedding_provider_id = embedding_provider.meta().id
                query_embedding = await embedding_provider.get_embedding(question)
                vector_hits = await self._vector_search(
                    session_id,
                    query_embedding,
                    embedding_provider_id=embedding_provider_id,
                    limit=limit,
                )
                for hit in vector_hits:
                    existing = combined.get(hit["chunk_id"])
                    if existing:
                        existing["score"] = max(existing["score"], hit["score"])
                    else:
                        combined[hit["chunk_id"]] = hit
            except Exception as exc:  # noqa: BLE001
                logger.warning("Vector search failed: %s", exc)

        candidates = sorted(
            combined.values(),
            key=lambda item: item.get("score", 0),
            reverse=True,
        )[:limit]

        rerank_provider = self.get_rerank_provider()
        if rerank_provider and candidates:
            try:
                rerank_results = await rerank_provider.rerank(
                    question,
                    [candidate["content"] for candidate in candidates],
                    top_n=len(candidates),
                )
                rerank_map = {result.index: result.relevance_score for result in rerank_results}
                for idx, candidate in enumerate(candidates):
                    if idx in rerank_map:
                        candidate["score"] = float(rerank_map[idx])
                candidates.sort(key=lambda item: item.get("score", 0), reverse=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Rerank failed: %s", exc)

        answer = self._format_query_fallback(question, candidates)
        use_llm = self.query_answer_with_llm() if with_llm is None else with_llm
        if candidates and use_llm:
            provider = self.get_chat_provider(session_id)
            if provider:
                try:
                    answer = await self._query_with_llm(provider, question, candidates)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Query answer LLM failed, fallback to evidence list: %s", exc)
        return {"answer": answer, "candidates": candidates}

    async def _vector_search(
        self,
        session_id: str,
        query_embedding: list[float],
        *,
        embedding_provider_id: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        scan_limit = max(limit * 100, 1000)
        chunks = await self.storage.list_memory_chunks_with_embeddings(
            session_id,
            embedding_provider_id=embedding_provider_id,
            limit=scan_limit,
        )
        scored: list[dict[str, Any]] = []
        threshold = self.similarity_threshold()
        for chunk in chunks:
            embedding = chunk.get("embedding_json")
            if not embedding:
                continue
            score = self._cosine_similarity(query_embedding, embedding)
            if score >= threshold:
                chunk["score"] = score
                scored.append(chunk)
        scored.sort(key=lambda item: item["score"], reverse=True)
        return scored[:limit]

    def _cosine_similarity(self, left: list[float], right: list[float]) -> float:
        if len(left) != len(right) or not left:
            return 0.0
        dot = sum(a * b for a, b in zip(left, right))
        left_norm = math.sqrt(sum(a * a for a in left))
        right_norm = math.sqrt(sum(b * b for b in right))
        if left_norm == 0 or right_norm == 0:
            return 0.0
        return dot / (left_norm * right_norm)

    async def _query_with_llm(
        self,
        provider: Provider,
        question: str,
        candidates: list[dict[str, Any]],
    ) -> str:
        evidence_lines = []
        for candidate in candidates[:8]:
            label = f"{candidate['source_type']}:{candidate['source_id'][:8]}"
            evidence_lines.append(f"[{label}] {candidate['content']}")
        prompt = (
            f"用户问题: {question}\n"
            "可用证据如下，请只根据证据回答，并在关键结论后引用对应证据标签：\n"
            + "\n".join(evidence_lines)
        )
        system_prompt = (
            "你是时间记录追查助手。"
            "只能基于给定证据回答，证据不足时要明确说不知道或记录不足。"
            "输出中文，结论尽量简洁。"
        )
        response = await provider.text_chat(prompt=prompt, system_prompt=system_prompt)
        return response.completion_text.strip()

    def _format_query_fallback(self, question: str, candidates: list[dict[str, Any]]) -> str:
        if not candidates:
            return f"没有检索到与“{question}”相关的长期记录。"
        lines = [f"与“{question}”最相关的记录如下："]
        for candidate in candidates[:5]:
            lines.append(
                f"- [{candidate['source_type']}:{candidate['source_id'][:8]}] {candidate['content'].splitlines()[0]}"
            )
        return "\n".join(lines)
