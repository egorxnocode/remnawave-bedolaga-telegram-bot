"""Disabled-by-default lifecycle for the durable AI support worker."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.services.ai_support.types import AiSupportMode
from app.services.ai_support.worker import (
    AiSupportWorker,
    AiSupportWorkerResult,
    AiSupportWorkerStatus,
    ai_support_worker,
)


logger = structlog.get_logger(__name__)

SessionFactory = Callable[[], Any]


class AiSupportWorkerRunner:
    """Own one in-process worker loop and explicit transaction boundaries."""

    def __init__(
        self,
        *,
        worker: AiSupportWorker = ai_support_worker,
        session_factory: SessionFactory = AsyncSessionLocal,
    ) -> None:
        self._worker = worker
        self._session_factory = session_factory
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._started_at: datetime | None = None
        self._stopped_at: datetime | None = None
        self._last_cycle_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._last_error_at: datetime | None = None
        self._last_error_type: str | None = None
        self._processed_jobs = 0
        self._consecutive_failures = 0

    @staticmethod
    def configured_enabled() -> bool:
        return bool(settings.AI_SUPPORT_WORKER_ENABLED)

    @staticmethod
    def mode() -> AiSupportMode:
        return AiSupportMode(settings.AI_SUPPORT_MODE)

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> bool:
        if self.is_running():
            return True
        if not self.configured_enabled():
            logger.info('AI support worker remains disabled')
            return False
        if self.mode() is AiSupportMode.OFF:
            logger.warning('AI support worker blocked because mode is off')
            return False

        self._stop_event.clear()
        self._started_at = datetime.now(UTC)
        self._stopped_at = None
        self._task = asyncio.create_task(self._run_loop(), name='ai-support-worker')
        logger.info(
            'AI support worker started',
            mode=self.mode().value,
            poll_seconds=settings.AI_SUPPORT_WORKER_POLL_SECONDS,
        )
        return True

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return

        self._stop_event.set()
        try:
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=settings.AI_SUPPORT_WORKER_SHUTDOWN_SECONDS,
            )
        except TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            self._task = None
            self._stopped_at = datetime.now(UTC)
        logger.info('AI support worker stopped')

    async def run_once(self) -> AiSupportWorkerResult:
        async with self._session_factory() as db:
            db: AsyncSession
            try:
                result = await self._worker.process_next(db)
                if result.status in {
                    AiSupportWorkerStatus.ESCALATED,
                    AiSupportWorkerStatus.PROVIDER_UNAVAILABLE,
                }:
                    await db.commit()
                else:
                    await db.rollback()
                return result
            except BaseException:
                await db.rollback()
                raise

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                result = await self.run_once()
                now = datetime.now(UTC)
                self._last_cycle_at = now
                self._last_success_at = now
                self._last_error_type = None
                self._consecutive_failures = 0
                if result.job_id is not None:
                    self._processed_jobs += 1
                if result.status is AiSupportWorkerStatus.IDLE or result.status is AiSupportWorkerStatus.DISABLED:
                    await self._wait_for_stop(settings.AI_SUPPORT_WORKER_POLL_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                now = datetime.now(UTC)
                self._last_cycle_at = now
                self._last_error_at = now
                self._last_error_type = type(error).__name__
                self._consecutive_failures += 1
                logger.error(
                    'AI support worker cycle failed',
                    error_type=self._last_error_type,
                    consecutive_failures=self._consecutive_failures,
                )
                await self._wait_for_stop(settings.AI_SUPPORT_WORKER_POLL_SECONDS)

    async def _wait_for_stop(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=timeout)
        except TimeoutError:
            pass

    def get_status(self) -> dict[str, object]:
        configured = self.configured_enabled()
        mode = self.mode()
        running = self.is_running()
        if not configured:
            state = 'disabled'
        elif mode is AiSupportMode.OFF:
            state = 'blocked_mode_off'
        elif running:
            state = 'running'
        else:
            state = 'stopped'

        return {
            'configured_enabled': configured,
            'mode': mode.value,
            'state': state,
            'running': running,
            'healthy': not configured or running,
            'processed_jobs': self._processed_jobs,
            'consecutive_failures': self._consecutive_failures,
            'last_error_type': self._last_error_type,
            'started_at': self._iso(self._started_at),
            'stopped_at': self._iso(self._stopped_at),
            'last_cycle_at': self._iso(self._last_cycle_at),
            'last_success_at': self._iso(self._last_success_at),
            'last_error_at': self._iso(self._last_error_at),
        }

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None


ai_support_worker_runner = AiSupportWorkerRunner()
