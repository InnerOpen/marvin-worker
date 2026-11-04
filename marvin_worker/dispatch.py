"""The dispatcher: finds queued `job` records in Marvin and starts a worker for each, in the mode its `job-kind` says.

Marvin holds the queue (a `job` record per task) and the settings (a `job-kind` record per kind: image, mode, time allowed). This
only calls OUT to Marvin, so it can sit beside a cluster or a Docker host without Marvin reaching in or holding any keys to it.
Each pass: fail jobs whose worker ended without reporting (docker mode) or stopped making progress, then claim queued jobs (up to DISPATCH_MAX_PARALLEL running at once) and
start them. A started worker is told Marvin's address and the token, writes the outcome onto its record, and exits.

Modes: `kubernetes` (a Job per run), `docker` (a container per run; the dispatcher needs the Docker CLI and access to a Docker
engine: on the host itself or with the socket, which is root on that host), `http` (POST to a standing worker's /jobs), `local`
(a process on this machine). Run ONE dispatcher: it claims before it starts, but two could still race.

Environment: MARVIN_API_URL, MARVIN_USER_TOKEN, DISPATCH_POLL_SECONDS (10), DISPATCH_MAX_PARALLEL (2),
DISPATCH_WORKER_TOKEN (http mode), DISPATCH_NAMESPACE and DISPATCH_SECRET (kubernetes mode: the namespace to run Jobs in, and the
Secret that holds MARVIN_USER_TOKEN under that key).
"""

import json
import os
import ssl
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from . import marvin_api

KUBE_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
GRACE_SECONDS = 120  # past its time allowed, so a job near its limit isn't failed under the worker's feet
DEFAULT_TIMEOUT = 3600


class LaunchError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _name(slug: str) -> str:
    """A DNS-safe name for a container or Job from a record's slug."""
    safe = "".join(c if c.isalnum() else "-" for c in slug.lower()).strip("-")
    return f"mw-{safe}"[:60].rstrip("-")


def _command(job: dict) -> list[str]:
    return ["python", "-m", "marvin_worker", "run", "--kind", job["kind"], "--job", job["job"], "--args", json.dumps(job["args"])]


def start_local(job: dict, kind: dict, creds: marvin_api.Creds) -> None:
    env = {**os.environ, "MARVIN_API_URL": creds[0], "MARVIN_USER_TOKEN": creds[1]}
    subprocess.Popen([sys.executable, *_command(job)[1:]], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603


def container_cli() -> str:
    """The container command: `docker`, or `podman` (set WORKER_CONTAINER_CLI), whose CLI takes the same arguments."""
    return os.environ.get("WORKER_CONTAINER_CLI") or "docker"


def start_docker(job: dict, kind: dict, creds: marvin_api.Creds) -> None:
    if not kind.get("image"):
        raise LaunchError("this kind has no image")
    resources = kind.get("resources") or {}
    limits = [*(["--cpus", str(resources["cpu"])] if resources.get("cpu") else []), *(["--memory", str(resources["memory"])] if resources.get("memory") else [])]
    # The token goes in by NAME (-e VAR): the docker CLI reads it from its own environment, so it is not in any process listing.
    cmd = [container_cli(), "run", "-d", "--name", _name(job["job"]), "--label", "marvin-worker=1", "--label", f"marvin-job={job['job']}", "-e", "MARVIN_USER_TOKEN", "-e", f"MARVIN_API_URL={creds[0]}", *limits, kind["image"], *_command(job)]
    done = subprocess.run(cmd, env={**os.environ, "MARVIN_USER_TOKEN": creds[1]}, capture_output=True, text=True, timeout=120)  # noqa: S603
    if done.returncode != 0:
        raise LaunchError(f"{container_cli()} run failed: {(done.stderr or done.stdout).strip()[:300]}")


def start_http(job: dict, kind: dict, creds: marvin_api.Creds) -> None:
    if not kind.get("url"):
        raise LaunchError("this kind has no worker URL")
    body = json.dumps({"kind": job["kind"], "job": job["job"], "args": job["args"]}).encode()
    req = urllib.request.Request(
        kind["url"].rstrip("/") + "/jobs", data=body, method="POST",
        headers={"Authorization": f"Bearer {os.environ.get('DISPATCH_WORKER_TOKEN', '')}", "Content-Type": "application/json", "User-Agent": marvin_api.USER_AGENT},
    )  # fmt: skip
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except (urllib.error.URLError, OSError) as e:
        raise LaunchError(f"the worker at {kind['url']} did not take the job: {e}") from e


def job_manifest(job: dict, kind: dict, creds: marvin_api.Creds, *, secret: str) -> dict:
    """The Kubernetes Job for one run: the kind's image, Marvin's address, the token from a Secret (never written into the Job)."""
    if not kind.get("image"):
        raise LaunchError("this kind has no image")
    resources = kind.get("resources") or {}
    container = {
        "name": "worker",
        "image": kind["image"],
        "command": _command(job),  # the whole command: the images set CMD, not ENTRYPOINT, so `args` alone would replace it and run "run"
        "env": [
            {"name": "MARVIN_API_URL", "value": creds[0]},
            {"name": "MARVIN_USER_TOKEN", "valueFrom": {"secretKeyRef": {"name": secret, "key": "MARVIN_USER_TOKEN"}}},
        ],
        **({"resources": {"requests": resources, "limits": resources}} if resources else {}),
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": _name(job["job"]), "labels": {"app": "marvin-worker", "marvin-job": _name(job["job"])}},
        "spec": {
            "backoffLimit": 0,  # a failed job says so on its record; retrying is a decision for whoever queued it
            "activeDeadlineSeconds": int(kind.get("timeout_seconds") or DEFAULT_TIMEOUT),
            "ttlSecondsAfterFinished": 600,
            "template": {"spec": {"restartPolicy": "Never", "containers": [container]}},
        },
    }


def start_kubernetes(job: dict, kind: dict, creds: marvin_api.Creds, *, api: str | None = None) -> None:
    namespace = os.environ.get("DISPATCH_NAMESPACE") or (KUBE_DIR / "namespace").read_text().strip()
    secret = os.environ.get("DISPATCH_SECRET", "marvin-worker")
    api = api or "https://kubernetes.default.svc"
    token = os.environ.get("DISPATCH_KUBE_TOKEN") or (KUBE_DIR / "token").read_text().strip()
    body = json.dumps(job_manifest(job, kind, creds, secret=secret)).encode()
    req = urllib.request.Request(
        f"{api}/apis/batch/v1/namespaces/{namespace}/jobs", data=body, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )  # fmt: skip
    context = ssl.create_default_context(cafile=str(KUBE_DIR / "ca.crt")) if api.startswith("https") and (KUBE_DIR / "ca.crt").exists() else None
    try:
        urllib.request.urlopen(req, timeout=30, context=context).read()
    except urllib.error.HTTPError as e:
        raise LaunchError(f"Kubernetes refused the Job ({e.code}): {e.read()[:300].decode('utf-8', 'replace')}") from e
    except (urllib.error.URLError, OSError) as e:
        raise LaunchError(f"can't reach Kubernetes: {e}") from e


OUTPUT_MARK = "--- container output ---"
OUTPUT_LIMIT = 4000  # characters kept per job: enough for a traceback or a run's last lines, not a whole log


def reap_docker(creds: marvin_api.Creds) -> int:
    """Clean up the worker containers that have exited, keeping what each one said. A job still `claimed` or `running` when its container
    is gone never got an outcome (the worker died, or could not start): fail it with the container's last output, so it shows at once and
    not at the timeout. A job that did report (done or failed) keeps its own log and gains the container's output after it, so a run's
    console output is on the record instead of being thrown away with the container. Returns how many containers it removed. Containers
    are kept after exit (no --rm) exactly so this can read them."""

    def docker(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([container_cli(), *args], capture_output=True, text=True, timeout=60)  # noqa: S603

    def said(name: str, lines: int) -> str:
        out = docker("logs", "--tail", str(lines), name)  # a container's stdout and stderr come back on the two streams
        return (out.stdout + out.stderr).strip()

    # Names from `ps`, the job's slug from `inspect`: the two tools format `ps` labels differently (Docker has .Label, Podman only the
    # .Labels map), but `inspect` reads the same way in both.
    listed = docker("ps", "-a", "--filter", "label=marvin-worker=1", "--filter", "status=exited", "--format", "{{.Names}}")
    if listed.returncode != 0:
        return 0
    removed = 0
    for name in listed.stdout.split():
        slug = docker("inspect", "-f", '{{index .Config.Labels "marvin-job"}}', name).stdout.strip()
        rows = marvin_api.query(creds, {"entries": slug}, limit=1) if slug else []
        data = (rows[0].get("dataJson") or {}) if rows else {}
        status = data.get("status")
        if status in ("claimed", "running"):
            code = docker("inspect", "-f", "{{.State.ExitCode}}", name).stdout.strip()
            marvin_api.update_entry(creds, slug, {"status": "failed", "log": f"The worker container exited (code {code}) before reporting an outcome.\n{said(name, 15)[-600:]}"})
        elif status in ("done", "failed") and slug and OUTPUT_MARK not in str(data.get("log") or ""):
            output = said(name, 60)
            if output:
                kept = f"{str(data.get('log') or '').strip()}\n\n{OUTPUT_MARK}\n".lstrip("\n") + output[-OUTPUT_LIMIT:]
                marvin_api.update_entry(creds, slug, {"log": kept})
        docker("rm", name)
        removed += 1
    return removed


REAPERS: dict[str, Callable] = {"docker": reap_docker}
LAUNCHERS: dict[str, Callable] = {"local": start_local, "docker": start_docker, "http": start_http, "kubernetes": start_kubernetes}


HEARTBEAT_SECONDS = 60  # often enough for Marvin's Workers page to see it alive, rarely enough to cost nothing


def _host() -> str:
    import socket

    return os.environ.get("DISPATCH_NAME") or socket.gethostname()


class Dispatcher:
    def __init__(
        self,
        creds: marvin_api.Creds,
        *,
        max_parallel: int = 2,
        launchers: dict[str, Callable] | None = None,
        reapers: dict[str, Callable] | None = None,
        now: Callable = _now,
        claim_settle: float = 0.4,
    ):
        self.creds, self.max_parallel, self.launchers, self.now = creds, max_parallel, launchers or LAUNCHERS, now
        self.reapers = REAPERS if reapers is None else reapers
        self.name, self.started_at, self.jobs_started, self._beat = _host(), now().isoformat(), 0, None
        self.claim_settle = claim_settle  # seconds to wait for a rival's write before keeping a claim

    def _jobs(self, status: str) -> list[dict]:
        return marvin_api.query(self.creds, {"entry_type": "job", "data": {"status": status}, "sort": {"by": "created_at", "direction": "asc"}})

    def _kinds(self) -> dict[str, dict]:
        rows = marvin_api.query(self.creds, {"entry_type": "job-kind"})
        return {str((r.get("dataJson") or {}).get("kind")): r.get("dataJson") or {} for r in rows}

    def _fail(self, row: dict, why: str) -> None:
        marvin_api.update_entry(self.creds, row["slug"], {"status": "failed", "log": why})

    def _time_out(self, kinds: dict[str, dict]) -> None:
        """A claimed or running job past its time allowed (plus a grace) is failed, so a worker that died doesn't hold a place forever."""
        for status in ("claimed", "running"):
            for row in self._jobs(status):
                data = row.get("dataJson") or {}
                allowed = int((kinds.get(str(data.get("kind"))) or {}).get("timeout_seconds") or DEFAULT_TIMEOUT) + GRACE_SECONDS
                started = data.get("claimed_at")
                if started and (self.now() - datetime.fromisoformat(started)).total_seconds() > allowed:
                    self._fail(row, f"No outcome within {allowed - GRACE_SECONDS} s of being picked up; the worker probably died. Queue it again to retry.")

    def heartbeat(self, kinds: dict[str, dict]) -> None:
        """Say this dispatcher is alive: one `worker` record (named for the machine) whose `last_seen` Marvin's Workers page reads. At
        most once a minute, and never fatal: a workspace without the `worker` type just has no heartbeat."""
        now = self.now()
        if self._beat is not None and (now - self._beat).total_seconds() < HEARTBEAT_SECONDS:
            return
        self._beat = now
        modes = sorted({str(k.get("mode") or "kubernetes") for k in kinds.values()})
        offered = [{"kind": name, "description": f"launched in {k.get('mode') or 'kubernetes'} mode"} for name, k in sorted(kinds.items())]
        try:
            marvin_api.upsert_entry(
                self.creds, "worker", f"worker-dispatcher-{_name(self.name)[3:]}", f"Dispatcher on {self.name}",
                {"role": "dispatcher", "host": self.name, "modes": ", ".join(modes), "kinds": offered, "status": "idle", "last_seen": now.isoformat(), "started_at": self.started_at, "jobs_started": self.jobs_started},
            )  # fmt: skip
        except marvin_api.MarvinError:
            pass

    def tick(self) -> int:
        """One pass. Returns how many jobs it started."""
        kinds = self._kinds()
        self.heartbeat(kinds)
        for mode in {str(k.get("mode") or "kubernetes") for k in kinds.values()}:
            if mode in self.reapers:
                self.reapers[mode](self.creds)  # workers that ended without reporting are failed now, not at the timeout
        self._time_out(kinds)
        free = self.max_parallel - len(self._jobs("claimed")) - len(self._jobs("running"))
        started = 0
        for row in self._jobs("queued"):
            if started >= free:
                break
            data = row.get("dataJson") or {}
            kind = kinds.get(str(data.get("kind")))
            if kind is None:
                self._fail(row, f"No job-kind “{data.get('kind')}” is registered: add one to say which image runs it.")
                continue
            launcher = self.launchers.get(str(kind.get("mode") or "kubernetes"))
            if launcher is None:
                self._fail(row, f"Unknown mode “{kind.get('mode')}” for kind {data.get('kind')}.")
                continue
            if not marvin_api.claim(self.creds, row["slug"], f"dispatcher {self.name}", self.now().isoformat(), settle=self.claim_settle):
                continue  # another worker took it
            try:
                launcher({"kind": data["kind"], "job": row["slug"], "args": data.get("args") or {}}, kind, self.creds)
                started += 1
                self.jobs_started += 1
            except LaunchError as e:
                self._fail(row, str(e))
        return started


def main() -> int:
    try:
        creds = (os.environ["MARVIN_API_URL"].rstrip("/"), os.environ.get("MARVIN_USER_TOKEN") or os.environ["MARVIN_TOKEN"])
    except KeyError as e:
        print(f"error: {e.args[0]} is not set", flush=True)
        return 2
    dispatcher = Dispatcher(creds, max_parallel=int(os.environ.get("DISPATCH_MAX_PARALLEL", "2")))
    poll = float(os.environ.get("DISPATCH_POLL_SECONDS", "10"))
    print(f"dispatching jobs from {creds[0]} every {poll:g} s (up to {dispatcher.max_parallel} at once)", flush=True)
    while True:
        try:
            dispatcher.tick()
        except Exception:  # noqa: BLE001 — Marvin unreachable for a moment must not end the dispatcher
            traceback.print_exc()
        time.sleep(poll)
