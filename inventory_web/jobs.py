from __future__ import annotations

import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .serializers import to_jsonable


@dataclass
class Job:
    id: str
    name: str
    status: str = "queued"
    progress: int = 0
    logs: list[str] = field(default_factory=list)
    result: Any = None
    error: str | None = None
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "progress": self.progress,
            "logs": list(self.logs[-200:]),
            "result": to_jsonable(self.result),
            "error": self.error,
            "createdAt": self.created_at.isoformat(),
            "updatedAt": self.updated_at.isoformat(),
        }


class JobRegistry:
    def __init__(self, max_workers: int = 4) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=max_workers)

    def create(self, name: str, work: Callable[[Callable[[str], None], Callable[[int], None]], Any]) -> Job:
        job = Job(id=uuid.uuid4().hex, name=name)
        with self._lock:
            self._jobs[job.id] = job
        self._executor.submit(self._run, job.id, work)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)[:100]

    def _mutate(self, job_id: str, callback: Callable[[Job], None]) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            callback(job)
            job.updated_at = datetime.now()

    def _run(
        self,
        job_id: str,
        work: Callable[[Callable[[str], None], Callable[[int], None]], Any],
    ) -> None:
        self._mutate(job_id, lambda j: setattr(j, "status", "running"))

        def log(message: str) -> None:
            self._mutate(job_id, lambda j: j.logs.append(str(message)))

        def progress(value: int) -> None:
            clean = max(0, min(100, int(value)))
            self._mutate(job_id, lambda j: setattr(j, "progress", clean))

        try:
            progress(5)
            result = work(log, progress)
            def done(job: Job) -> None:
                job.status = "succeeded"
                job.progress = 100
                job.result = result

            self._mutate(job_id, done)
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc(limit=8)

            def failed(job: Job) -> None:
                job.status = "failed"
                job.error = str(exc)
                job.logs.append(tb)

            self._mutate(job_id, failed)


jobs = JobRegistry()
