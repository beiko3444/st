"""Long-polling transport for translation events.

Cloudflare quick tunnels (`*.trycloudflare.com`) do not support Server-Sent
Events, so the page starts a job and fetches its events with ordinary
requests that return as soon as anything new arrives. That works through any
proxy and still shows text within a round trip of it being generated.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from typing import Any, Optional, Sequence

from .images import ImageAttachment
from .service import TranslationService

FINISHED_JOB_TTL = 120.0
MAX_JOBS = 50
# After the first new event, wait this long so one response carries a few
# deltas instead of one each.
COALESCE_SECONDS = 0.04


class Job:
    def __init__(self) -> None:
        self.id = uuid.uuid4().hex
        self.events: list[dict[str, Any]] = []
        self.done = False
        self.finished_at: Optional[float] = None
        self.changed = asyncio.Condition()
        self.task: Optional[asyncio.Task] = None


class JobRegistry:
    def __init__(self, service: TranslationService) -> None:
        self.service = service
        self._jobs: dict[str, Job] = {}

    def start(
        self,
        text: str,
        *,
        style: str,
        targets: Optional[Sequence[str]],
        replaces: Optional[str] = None,
        image: Optional[ImageAttachment] = None,
    ) -> Job:
        if replaces:
            self.cancel(replaces)
        self._prune()
        job = Job()
        self._jobs[job.id] = job
        job.task = asyncio.create_task(self._run(job, text, style, targets, image))
        return job

    async def _run(self, job: Job, text: str, style: str, targets: Optional[Sequence[str]], image: Optional[ImageAttachment]) -> None:
        events = self.service.translate(text, style=style, targets=targets, image=image)
        try:
            async for event in events:
                async with job.changed:
                    job.events.append(event)
                    job.changed.notify_all()
        finally:
            # Closing the generator interrupts unfinished Codex turns.
            with contextlib.suppress(Exception):
                await events.aclose()
            job.done = True
            job.finished_at = time.monotonic()
            async with job.changed:
                job.changed.notify_all()

    async def wait(self, job_id: str, after: int, timeout: float) -> Optional[dict[str, Any]]:
        """Events after index `after`, waiting up to `timeout` seconds for some; `None` if unknown."""
        job = self._jobs.get(job_id)
        if job is None:
            return None
        after = max(0, after)
        async with job.changed:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(job.changed.wait_for(lambda: len(job.events) > after or job.done), timeout)
        if len(job.events) > after and not job.done:
            await asyncio.sleep(COALESCE_SECONDS)
        # No await between reading events and done, so a done job has nothing left.
        events = job.events[after:]
        return {"events": events, "next": after + len(events), "done": job.done}

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None:
            return False
        if job.task is not None and not job.task.done():
            job.task.cancel()
        return True

    async def close(self) -> None:
        tasks = [job.task for job in self._jobs.values() if job.task is not None and not job.task.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def _prune(self) -> None:
        now = time.monotonic()
        for job_id, job in list(self._jobs.items()):
            if job.finished_at is not None and now - job.finished_at > FINISHED_JOB_TTL:
                del self._jobs[job_id]
        # A page that stops polling never collects its jobs; cap the backlog.
        while len(self._jobs) >= MAX_JOBS:
            oldest = next(iter(self._jobs))
            self.cancel(oldest)
            del self._jobs[oldest]
