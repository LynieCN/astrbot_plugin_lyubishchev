from __future__ import annotations

from datetime import datetime
from typing import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from astrbot.api import logger

from .service import LyubishchevService
from .storage import LyubishchevStorage


class LyubishchevScheduler:
    def __init__(
        self,
        *,
        storage: LyubishchevStorage,
        service: LyubishchevService,
        sender: Callable[[str, str], Awaitable[bool]],
    ) -> None:
        self.storage = storage
        self.service = service
        self.sender = sender
        self.scheduler = AsyncIOScheduler()
        self.started = False

    async def start(self) -> None:
        if self.started:
            return
        self.scheduler.start()
        self.started = True
        await self.reload_rules()

    async def shutdown(self) -> None:
        if not self.started:
            return
        self.scheduler.shutdown(wait=False)
        self.started = False

    async def reload_rules(self) -> None:
        if not self.started:
            return
        for job in list(self.scheduler.get_jobs()):
            self.scheduler.remove_job(job.id)
        rules = await self.storage.list_summary_rules(enabled_only=True)
        for rule in rules:
            self._schedule_rule(rule)

    def get_next_run_time(self, rule_id: str) -> str | None:
        job = self.scheduler.get_job(str(rule_id))
        if not job or not job.next_run_time:
            return None
        return job.next_run_time.isoformat()

    def _schedule_rule(self, rule: dict) -> None:
        try:
            trigger = CronTrigger.from_crontab(
                str(rule["cron_expression"]),
                timezone=str(rule["timezone"]),
            )
            self.scheduler.add_job(
                self._run_rule_job,
                trigger=trigger,
                id=str(rule["rule_id"]),
                args=[str(rule["rule_id"])],
                replace_existing=True,
            )
            next_run = self.get_next_run_time(str(rule["rule_id"]))
            logger.info(
                "Scheduled lyubishchev rule %s (%s), next_run=%s",
                rule.get("rule_id"),
                rule.get("rule_name"),
                next_run,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to schedule rule %s: %s", rule.get("rule_id"), exc)

    async def run_rule_now(self, rule_id: str) -> bool:
        rule = await self.storage.get_summary_rule(rule_id)
        if rule is None:
            return False
        await self._execute_rule(rule)
        return True

    async def _run_rule_job(self, rule_id: str) -> None:
        try:
            rule = await self.storage.get_summary_rule(rule_id)
            if rule is None or not rule.get("enabled", 1):
                return
            await self._execute_rule(rule)
        except Exception as exc:  # noqa: BLE001
            logger.error("Lyubishchev scheduled rule %s failed: %s", rule_id, exc, exc_info=True)

    async def _execute_rule(self, rule: dict) -> None:
        timezone_name = str(rule.get("timezone") or self.service.get_default_timezone())
        now = self.service._now(timezone_name)
        period_type = str(rule["period_type"])
        custom_days = None
        summary_type = period_type
        if period_type.startswith("custom:"):
            summary_type = "custom"
            custom_days = int(period_type.split(":", 1)[1])
        elif period_type == "custom":
            summary_type = "custom"
            custom_days = int(rule.get("lookback_days") or 7)
        start_date, end_date = self.service.get_period_bounds(
            summary_type,
            custom_days=custom_days,
            now=now,
        )
        summary = await self.service.generate_summary(
            session_id=str(rule["session_id"]),
            summary_type=summary_type,
            start_date=start_date,
            end_date=end_date,
            rule_id=str(rule["rule_id"]),
        )
        if summary is None:
            if rule.get("send_empty"):
                message = (
                    f"{rule['rule_name']}\n"
                    f"统计区间: {start_date.isoformat()} -> {end_date.isoformat()}\n"
                    "本周期暂无可用记录。"
                )
                sent = await self.sender(str(rule["session_id"]), message)
                if not sent:
                    logger.warning("Failed to send empty-summary message for rule %s", rule.get("rule_id"))
            return
        message = (
            f"{rule['rule_name']}\n"
            f"触发时间: {now.isoformat(timespec='seconds')}\n\n"
            f"{summary['content']}"
        )
        sent = await self.sender(str(rule["session_id"]), message)
        if not sent:
            logger.warning("Failed to send summary message for rule %s", rule.get("rule_id"))
