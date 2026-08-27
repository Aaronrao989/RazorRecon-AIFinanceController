"""In-memory background job store for reconciliation runs.

Each /reconcile call kicks off a Job on a worker thread. The engine's
``progress_cb`` appends events to the job; the SSE endpoint streams them. Results
and the audit trail are kept on the job for later retrieval/download.

In-memory is intentional for a free-tier single-process deployment (Render). For
horizontal scaling this would move to Redis/DB, but the engine itself is
stateless so that swap is isolated to this file.
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from recon_engine import reconcile, SimulatedLLMProvider, SimulatedAgentModel


@dataclass
class Job:
    id: str
    status: str = "pending"  # pending | running | done | error
    events: list[dict[str, Any]] = field(default_factory=list)
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add_event(self, ev: dict[str, Any]) -> None:
        with self._lock:
            ev = dict(ev)
            ev["seq"] = len(self.events)
            ev["t"] = round(time.time() - self.created_at, 3)
            self.events.append(ev)

    def events_since(self, seq: int) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.events[seq:])


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def create(self) -> Job:
        job = Job(id=uuid.uuid4().hex[:12])
        with self._lock:
            self._jobs[job.id] = job
        return job

    def start(
        self,
        data_dir: str,
        *,
        use_llm: bool = True,
        simulate: bool = False,
        use_agent: bool = False,
    ) -> Job:
        job = self.create()

        def run() -> None:
            job.status = "running"
            try:
                if use_agent:
                    result = reconcile(
                        data_dir,
                        agent_model=SimulatedAgentModel() if simulate else None,
                        use_agent=True,
                        progress_cb=job.add_event,
                    )
                else:
                    provider = SimulatedLLMProvider() if simulate else None
                    result = reconcile(
                        data_dir,
                        provider=provider,
                        use_llm=use_llm,
                        progress_cb=job.add_event,
                    )
                job.result = result.to_dict()
                job.status = "done"
            except Exception as e:  # noqa: BLE001 - surface any failure to the client
                job.error = f"{e}\n{traceback.format_exc()}"
                job.add_event({"phase": "error", "message": str(e)})
                job.status = "error"
            finally:
                job.finished_at = time.time()

        threading.Thread(target=run, daemon=True).start()
        return job
