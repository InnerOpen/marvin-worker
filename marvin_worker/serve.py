"""The worker: a small HTTP service that takes the heavy jobs Marvin hands it, and keeps nothing of its own.

Marvin (a workflow's `request` step) posts a job; the worker answers 202 at once and does it in the background:

    POST /jobs   {"kind": "audio-cut", "job": "<record slug>", "args": {"story": "the-last-onion"}}
    GET  /health

`kind` names a function registered with `@job_kind` (the kinds this image can run; see WORKER_JOB_MODULES); `job` is the slug of the Marvin record the outcome is written to
(its `status`, a `log`, and whatever fields the job returns); `args` is the job's own input. Marvin's record is the state, so a
restart loses nothing but a job in flight (its record stays `rendering`: post it again). Jobs run one at a time, in order.

Environment: WORKER_JOB_MODULES (modules that define this image's kinds), MARVIN_API_URL, MARVIN_USER_TOKEN (the `marvin` CLI's own name for it; MARVIN_TOKEN also works; a token pinned to the workspace: it can reach nothing else),
WORKER_TOKEN (the secret Marvin sends as a Bearer token), PORT (default 8080).
"""

import hmac
import json
import os
import queue
import tempfile
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import jobs, marvin_api
from .jobs import Context, JobError

MAX_BODY = 64 * 1024
RUNNING, DONE, FAILED = "rendering", "done", "failed"  # the status words of the job records Marvin keeps (a record's select offers these)


def validate(job: object) -> dict:
    """A job from Marvin: a kind this worker has, the record to write to, and the job's own args."""
    if not isinstance(job, dict):
        raise JobError("the job is a JSON object")
    kind, record, args = job.get("kind"), job.get("job"), job.get("args", {})
    if kind not in jobs.HANDLERS:
        raise JobError(f"kind is one of {', '.join(sorted(jobs.HANDLERS))}")
    if not isinstance(record, str) or not record.strip():
        raise JobError("job (the record's slug) is needed")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise JobError("args is a JSON object")
    return {"kind": kind, "job": record.strip(), "args": args}


def run_job(job: dict, creds: tuple[str, str]) -> bool:
    """Do one job and write the outcome onto its record, whichever way it goes. True if it succeeded."""
    record = job["job"]
    marvin_api.update_entry(creds, record, {"status": RUNNING, "log": ""})
    try:
        with tempfile.TemporaryDirectory(prefix="worker-") as tmp:
            result = jobs.HANDLERS[job["kind"]](job, Context(creds=creds, work=Path(tmp)))
        marvin_api.update_entry(creds, record, {**result, "status": DONE})
        return True
    except Exception as e:  # noqa: BLE001 — whatever went wrong is the record's answer
        marvin_api.update_entry(creds, record, {"status": FAILED, "log": f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}"})
        return False


def make_server(port: int, creds: tuple[str, str], worker_token: str, *, run=run_job) -> ThreadingHTTPServer:
    pending: queue.Queue = queue.Queue()

    def work() -> None:  # one at a time: ffmpeg is the bottleneck, and the order is the order asked
        while True:
            job = pending.get()
            try:
                run(job, creds)
            except Exception:  # noqa: BLE001 — even writing the failure can fail (Marvin unreachable); keep serving
                traceback.print_exc()

    threading.Thread(target=work, daemon=True, name="job-worker").start()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: dict) -> None:
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):  # noqa: N802
            if self.path == "/health":
                return self._send(200, {"ok": True, "queued": pending.qsize()})
            self._send(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if self.path != "/jobs":
                return self._send(404, {"error": "not found"})
            sent = self.headers.get("Authorization", "")
            if not hmac.compare_digest(sent, f"Bearer {worker_token}"):
                return self._send(401, {"error": "unauthorized"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._send(413, {"error": "too large"})
            try:
                job = validate(json.loads(self.rfile.read(length) or b"null"))
            except (JobError, json.JSONDecodeError) as e:
                return self._send(422, {"error": str(e)})
            pending.put(job)
            self._send(202, {"accepted": job["job"], "queued": pending.qsize()})

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer(("0.0.0.0", port), Handler)


def main() -> int:
    try:
        creds = (os.environ["MARVIN_API_URL"].rstrip("/"), os.environ.get("MARVIN_USER_TOKEN") or os.environ["MARVIN_TOKEN"])
        worker_token = os.environ["WORKER_TOKEN"]
    except KeyError as e:
        print(f"error: {e.args[0]} is not set", flush=True)
        return 2
    kinds = jobs.load_kinds()
    if not kinds:
        print("error: no job kinds: set WORKER_JOB_MODULES to the modules that define them", flush=True)
        return 2
    server = make_server(int(os.environ.get("PORT", "8080")), creds, worker_token)
    print(f"worker on :{server.server_port}, kinds: {', '.join(kinds)}", flush=True)
    server.serve_forever()
    return 0


def run_once(argv: list[str]) -> int:
    """Run ONE job and exit: for a container that exists only for the job (a Kubernetes Job, a CI runner, `docker run --rm`).
    The job is `--kind`, `--job` (the record's slug) and `--args` (JSON), or one JSON object in WORKER_JOB. Exit 0 if it succeeded.
    The outcome is on the record either way; the process only needs outbound access to Marvin, so nothing has to reach it."""
    import argparse

    p = argparse.ArgumentParser(prog="marvin_worker run")
    p.add_argument("--kind")
    p.add_argument("--job")
    p.add_argument("--args", default="{}")
    a = p.parse_args(argv)
    try:
        creds = (os.environ["MARVIN_API_URL"].rstrip("/"), os.environ.get("MARVIN_USER_TOKEN") or os.environ["MARVIN_TOKEN"])
        jobs.load_kinds()
        raw = json.loads(os.environ["WORKER_JOB"]) if os.environ.get("WORKER_JOB") else {"kind": a.kind, "job": a.job, "args": json.loads(a.args)}
        job = validate(raw)
    except (KeyError, json.JSONDecodeError, JobError) as e:
        print(f"error: {e.args[0] if e.args else e}", flush=True)
        return 2
    return 0 if run_job(job, creds) else 1
