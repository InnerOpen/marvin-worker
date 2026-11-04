"""Where a worker announces itself, finds jobs and reports outcomes: one small seam, so none of it is tied to Marvin.

    announce(worker)         "I am alive, and here is what I can do" (the heartbeat and the kinds' contracts)
    next_job(kinds, who)     claim the next queued job of a kind this worker has, or None
    report(job, changes)     the job's status, log and results

`MarvinBackend` (the default) keeps all three in Marvin's records: Marvin is the source of truth and its Workers page reads them.
`HttpSink` is an EXTRA destination for announce and report (any URL that takes JSON). `Fanout` sends to the primary and every sink, so a
heartbeat can also reach another system. A queue (RabbitMQ, Redis) is another implementation of the same three calls: put it in a
module named by WORKER_BACKEND (`package.module:factory`, called with the Marvin credentials), so the library it needs lives in the
image that wants it and the base stays standard-library only.
"""

import importlib
import json
import os
import urllib.request
from datetime import UTC, datetime

from . import marvin_api

Job = dict  # {"kind", "job" (the record's slug), "args"}


class Backend:
    def announce(self, worker: dict) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def next_job(self, kinds: list[str], who: str) -> Job | None:  # pragma: no cover - interface
        raise NotImplementedError

    def report(self, job: Job, changes: dict) -> None:  # pragma: no cover - interface
        raise NotImplementedError


def worker_slug(worker: dict) -> str:
    host = "".join(c if c.isalnum() else "-" for c in str(worker.get("host", "")).lower()).strip("-")
    return f"worker-{worker.get('role', 'worker')}-{host}"


class MarvinBackend(Backend):
    def __init__(self, creds: marvin_api.Creds, *, settle: float = 0.4):
        self.creds, self.settle = creds, settle

    def announce(self, worker: dict) -> None:
        marvin_api.upsert_entry(self.creds, "worker", worker_slug(worker), f"{worker.get('role', 'worker').title()} on {worker.get('host')}", worker)

    def next_job(self, kinds: list[str], who: str) -> Job | None:
        queued = marvin_api.query(self.creds, {"entry_type": "job", "data": {"status": "queued"}, "sort": {"by": "created_at", "direction": "asc"}}, limit=25)
        for row in queued:
            data = row.get("dataJson") or {}
            if data.get("kind") in kinds and marvin_api.claim(self.creds, row["slug"], who, datetime.now(UTC).isoformat(), settle=self.settle):
                return {"kind": data["kind"], "job": row["slug"], "args": data.get("args") or {}}
        return None

    def report(self, job: Job, changes: dict) -> None:
        marvin_api.update_entry(self.creds, job["job"], changes)


class HttpSink(Backend):
    """Also tell another system: POST each announcement and outcome as JSON to these URLs (`auth` is sent as the Authorization header).
    A sink that is down never stops the worker or the primary."""

    def __init__(self, urls: list[str], auth: str | None = None):
        self.urls, self.auth = urls, auth

    def _post(self, body: dict) -> None:
        data = json.dumps(body, default=str).encode()
        for url in self.urls:
            headers = {"Content-Type": "application/json", "User-Agent": marvin_api.USER_AGENT, **({"Authorization": self.auth} if self.auth else {})}
            try:
                urllib.request.urlopen(urllib.request.Request(url, data=data, method="POST", headers=headers), timeout=10).read()
            except OSError:
                pass

    def announce(self, worker: dict) -> None:
        self._post({"event": "announce", "worker": worker})

    def next_job(self, kinds: list[str], who: str) -> Job | None:
        return None

    def report(self, job: Job, changes: dict) -> None:
        self._post({"event": "report", "job": job["job"], "kind": job["kind"], "changes": changes})


class Fanout(Backend):
    """The primary decides what the worker does (intake); announcements and reports go to it and to every sink."""

    def __init__(self, primary: Backend, sinks: list[Backend]):
        self.primary, self.sinks = primary, sinks

    def announce(self, worker: dict) -> None:
        for backend in (self.primary, *self.sinks):
            backend.announce(worker)

    def next_job(self, kinds: list[str], who: str) -> Job | None:
        return self.primary.next_job(kinds, who)

    def report(self, job: Job, changes: dict) -> None:
        for backend in (self.primary, *self.sinks):
            backend.report(job, changes)


def load_backend(creds: marvin_api.Creds) -> Backend:
    """WORKER_BACKEND (`package.module:factory`) else Marvin's records; WORKER_SINKS (comma-separated URLs, with WORKER_SINK_AUTH) adds sinks."""
    spec = os.environ.get("WORKER_BACKEND", "")
    if spec:
        module, _, factory = spec.partition(":")
        primary = getattr(importlib.import_module(module), factory or "make_backend")(creds)
    else:
        primary = MarvinBackend(creds)
    urls = [u.strip() for u in os.environ.get("WORKER_SINKS", "").split(",") if u.strip()]
    return Fanout(primary, [HttpSink(urls, os.environ.get("WORKER_SINK_AUTH") or None)]) if urls else primary
