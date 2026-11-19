"""Minimal in-memory background job store.

Redaction can take from seconds to minutes, so the API returns a job ID immediately and
the browser polls. Jobs live in memory next to the workspaces they belong to (single
instance, or sticky sessions if scaled out). Each job is visible only to its own workspace.
"""
import asyncio
import logging
import time
import uuid
from typing import Awaitable, Callable

log = logging.getLogger(__name__)
_MAX_AGE_S = 3600


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, dict] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._owners: dict[str, str] = {}   # job id -> workspace id

    def start(self, work: Callable[[], Awaitable[dict]], owner: str) -> str:
        self._prune()
        job_id = uuid.uuid4().hex
        job = {"id": job_id, "status": "running", "created": time.time()}
        self._jobs[job_id] = job
        self._owners[job_id] = owner

        async def runner() -> None:
            try:
                job.update(await work())
                job["status"] = "succeeded"
            except Exception as exc:
                log.exception("Job %s failed", job_id)
                job.update(status="failed", error=str(exc))
            finally:
                self._tasks.pop(job_id, None)

        self._tasks[job_id] = asyncio.create_task(runner())
        return job_id

    def get(self, job_id: str, owner: str) -> dict | None:
        """The job, only if it belongs to this workspace."""
        return self._jobs.get(job_id) if self._owners.get(job_id) == owner else None

    def _prune(self) -> None:
        cutoff = time.time() - _MAX_AGE_S
        for job_id in [j for j, v in self._jobs.items() if v["created"] < cutoff and v["status"] != "running"]:
            del self._jobs[job_id]
            self._owners.pop(job_id, None)
