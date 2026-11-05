"""The runner: a standing worker that registers what it can do and takes the jobs it can do, itself.

Point a container at Marvin with a token and it announces itself (its machine, the kinds it has with each kind's contract, what it
needs, whether it is idle or busy), then claims queued jobs of those kinds and runs them. No dispatcher is needed, and nothing has to
reach in: it only calls out. Add another container for another capability. What it can do comes from the modules the image loads
(WORKER_JOB_MODULES), or a subset of them (WORKER_KINDS).

Environment: MARVIN_API_URL, MARVIN_USER_TOKEN, WORKER_JOB_MODULES, WORKER_NAME (default: the machine's name), WORKER_KINDS
(optional: only advertise and take these), WORKER_POLL_SECONDS (5), plus WORKER_BACKEND / WORKER_SINKS (see backend.py).
"""

import os
import socket
import time
import traceback
from collections.abc import Callable
from datetime import UTC, datetime

from . import jobs, log
from .backend import Backend, load_backend
from .serve import run_job

HEARTBEAT_SECONDS = 60


class Runner:
    def __init__(
        self, backend: Backend, creds: tuple[str, str], *, name: str, only: list[str] | None = None, run: Callable = run_job, now: Callable = lambda: datetime.now(UTC)
    ):
        self.backend, self.creds, self.name, self.run, self.now = backend, creds, name, run, now
        self.contracts = jobs.describe_kinds(only)
        self.kinds = [c["kind"] for c in self.contracts]
        self.tags = sorted({need for c in self.contracts for need in c["needs"]})
        self.started_at, self.jobs_done, self._beat = now().isoformat(), 0, None

    def worker(self, status: str, current: str | None = None) -> dict:
        return {
            "role": "runner", "host": self.name, "status": status, "current_job": current or "", "kinds": self.contracts,
            "tags": ", ".join(self.tags), "last_seen": self.now().isoformat(), "started_at": self.started_at, "jobs_done": self.jobs_done,
            **jobs.identity(),
        }  # fmt: skip

    def announce(self, status: str = "idle", current: str | None = None, *, force: bool = False) -> None:
        """Say so, at most once a minute (or at once when `force`, as when a job starts or ends). Never fatal."""
        now = self.now()
        if not force and self._beat is not None and (now - self._beat).total_seconds() < HEARTBEAT_SECONDS:
            return
        self._beat = now
        try:
            self.backend.announce(self.worker(status, current))
        except Exception:  # noqa: BLE001 — Marvin unreachable for a moment must not stop the worker
            traceback.print_exc()

    def step(self) -> bool:
        """One pass: announce if due, take a job if there is one and run it. True if a job ran."""
        self.announce()
        try:
            job = self.backend.next_job(self.kinds, self.name)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return False
        if job is None:
            return False
        log.say(f"{job['job']}: claimed ({job['kind']}) by {self.name}")
        self.announce("busy", job["job"], force=True)
        self.run(job, self.creds, report=lambda changes: self.backend.report(job, changes))
        self.jobs_done += 1
        self.announce("idle", force=True)
        return True


def main() -> int:
    try:
        creds = (os.environ["MARVIN_API_URL"].rstrip("/"), os.environ.get("MARVIN_USER_TOKEN") or os.environ["MARVIN_TOKEN"])
    except KeyError as e:
        print(f"error: {e.args[0]} is not set", flush=True)
        return 2
    kinds = jobs.load_kinds()
    only = [k.strip() for k in os.environ.get("WORKER_KINDS", "").split(",") if k.strip()] or None
    if not jobs.describe_kinds(only):
        print("error: no job kinds to offer: set WORKER_JOB_MODULES (and check WORKER_KINDS)", flush=True)
        return 2
    runner = Runner(load_backend(creds), creds, name=os.environ.get("WORKER_NAME") or socket.gethostname(), only=only)
    poll = float(os.environ.get("WORKER_POLL_SECONDS", "5"))
    print(f"runner {runner.name} offers {', '.join(runner.kinds)} ({len(kinds)} loaded); polling Marvin every {poll:g} s", flush=True)
    while True:
        if not runner.step():
            time.sleep(poll)
