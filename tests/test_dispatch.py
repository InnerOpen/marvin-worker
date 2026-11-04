"""The dispatcher: which queued jobs it starts, how each mode starts a worker, and what it tells that worker."""

import json
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from marvin_worker import dispatch

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


class FakeMarvin(BaseHTTPRequestHandler):
    """Records in memory; /entries/query filters on entry_type and data like Marvin's; PATCH merges."""

    rows: list = []

    def _json(self, status, body):
        self.send_response(status)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_GET(self):  # noqa: N802
        if self.path == "/api/platform/entry-types":
            return self._json(200, [{"slug": "worker", "id": "type-worker"}])
        self._json(200, FakeMarvin.rows)

    def do_POST(self):  # noqa: N802
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/platform/entries":  # create
            row = {"id": f"id-{payload['slug']}", "slug": payload["slug"], "entryType": "worker", "dataJson": payload["dataJson"]}
            FakeMarvin.rows.append(row)
            return self._json(201, row)
        spec = payload["spec"]
        if "entries" in spec:  # one record by slug
            hits = [r for r in FakeMarvin.rows if r["slug"] == spec["entries"]]
            return self._json(200, {"entries": hits, "total": len(hits)})
        hits = [
            r for r in FakeMarvin.rows
            if r["entryType"] == spec["entry_type"] and all((r["dataJson"] or {}).get(k) == v for k, v in (spec.get("data") or {}).items())
        ]  # fmt: skip
        self._json(200, {"entries": hits, "total": len(hits)})

    def do_PATCH(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        row = next(r for r in FakeMarvin.rows if f"/{r['id']}" in self.path)
        row["dataJson"] = body["dataJson"]
        self._json(200, row)

    def log_message(self, *args):
        pass


def _job(slug, status="queued", kind="audio-cut", **data):
    return {"id": f"id-{slug}", "slug": slug, "entryType": "job", "dataJson": {"kind": kind, "args": {"story": "s"}, "status": status, **data}}


def _kind(kind="audio-cut", **data):
    return {"id": f"id-kind-{kind}", "slug": f"kind-{kind}", "entryType": "job-kind", "dataJson": {"kind": kind, "mode": "local", **data}}


@pytest.fixture
def marvin():
    FakeMarvin.rows = []
    server = HTTPServer(("127.0.0.1", 0), FakeMarvin)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield (f"http://127.0.0.1:{server.server_port}", "pinned-token")
    server.shutdown()


def _data(slug):
    return next(r for r in FakeMarvin.rows if r["slug"] == slug)["dataJson"]


def _dispatcher(creds, started, **kw):
    return dispatch.Dispatcher(creds, launchers={"local": lambda job, kind, c: started.append((job, kind, c))}, reapers={}, now=lambda: NOW, **kw)


def test_a_queued_job_is_claimed_and_started_with_marvins_address_and_the_token(marvin):
    FakeMarvin.rows = [_kind(), _job("job-1")]
    started = []

    assert _dispatcher(marvin, started).tick() == 1

    (job, kind, creds), = started
    assert job == {"kind": "audio-cut", "job": "job-1", "args": {"story": "s"}} and creds == marvin  # the worker is told where Marvin is, and how to sign in
    assert _data("job-1")["status"] == "claimed" and _data("job-1")["attempts"] == 1 and _data("job-1")["claimed_at"] == NOW.isoformat()


def test_a_job_whose_kind_nobody_registered_fails_with_the_reason(marvin):
    FakeMarvin.rows = [_job("job-1", kind="mystery")]
    started = []

    _dispatcher(marvin, started).tick()

    assert started == [] and _data("job-1")["status"] == "failed" and "mystery" in _data("job-1")["log"]


def test_only_as_many_jobs_start_as_there_are_free_places(marvin):
    FakeMarvin.rows = [_kind(), _job("a", status="running", claimed_at=NOW.isoformat()), _job("b"), _job("c")]
    started = []

    assert _dispatcher(marvin, started, max_parallel=2).tick() == 1  # one running, so one more

    assert [j["job"] for j, _, _ in started] == ["b"] and _data("c")["status"] == "queued"  # c waits its turn


def test_a_job_with_no_outcome_past_its_time_is_failed_so_a_dead_worker_does_not_hold_a_place(marvin):
    long_ago = (NOW - timedelta(seconds=30 + dispatch.GRACE_SECONDS + 1)).isoformat()
    FakeMarvin.rows = [_kind(timeout_seconds=30), _job("stale", status="running", claimed_at=long_ago), _job("fresh", status="running", claimed_at=NOW.isoformat())]

    _dispatcher(marvin, []).tick()

    assert _data("stale")["status"] == "failed" and "No outcome" in _data("stale")["log"] and _data("fresh")["status"] == "running"


def test_a_worker_that_will_not_start_fails_the_job_instead_of_leaving_it_claimed(marvin):
    FakeMarvin.rows = [_kind(), _job("job-1")]

    def refuse(job, kind, creds):
        raise dispatch.LaunchError("no image")

    dispatch.Dispatcher(marvin, launchers={"local": refuse}, reapers={}, now=lambda: NOW).tick()

    assert _data("job-1")["status"] == "failed" and _data("job-1")["log"] == "no image"


def test_docker_gets_the_address_and_the_token_by_name_not_in_the_command(monkeypatch):
    seen = {}

    def run(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw["env"]
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(dispatch.subprocess, "run", run)

    dispatch.start_docker({"kind": "audio-cut", "job": "Job 1", "args": {"story": "s"}}, {"image": "reg/media:1", "resources": {"cpu": "2", "memory": "1g"}}, ("https://marvin.example", "secret-token"))

    cmd = seen["cmd"]
    assert cmd[:3] == ["docker", "run", "-d"] and "--rm" not in cmd and "marvin-job=Job 1" in cmd and "reg/media:1" in cmd and "--cpus" in cmd and "--memory" in cmd
    assert "MARVIN_API_URL=https://marvin.example" in cmd and "MARVIN_USER_TOKEN" in cmd
    assert "secret-token" not in " ".join(cmd) and seen["env"]["MARVIN_USER_TOKEN"] == "secret-token"  # not in any process listing
    assert cmd[cmd.index("reg/media:1") + 1 :][:4] == ["python", "-m", "marvin_worker", "run"] and "mw-job-1" in cmd


def test_the_kubernetes_job_runs_once_with_a_deadline_and_the_token_from_a_secret():
    job = {"kind": "audio-cut", "job": "job-1", "args": {"story": "s"}}

    manifest = dispatch.job_manifest(job, {"image": "reg/media:1", "timeout_seconds": 900, "resources": {"cpu": "1", "memory": "1Gi"}}, ("https://marvin.example", "tok"), secret="marvin-worker")

    spec = manifest["spec"]
    assert (spec["backoffLimit"], spec["activeDeadlineSeconds"], spec["template"]["spec"]["restartPolicy"]) == (0, 900, "Never")
    (container,) = spec["template"]["spec"]["containers"]
    assert container["args"] == ["run", "--kind", "audio-cut", "--job", "job-1", "--args", '{"story": "s"}']
    env = {e["name"]: e for e in container["env"]}
    assert env["MARVIN_API_URL"]["value"] == "https://marvin.example"
    assert "value" not in env["MARVIN_USER_TOKEN"] and env["MARVIN_USER_TOKEN"]["valueFrom"]["secretKeyRef"] == {"name": "marvin-worker", "key": "MARVIN_USER_TOKEN"}
    assert "tok" not in json.dumps(manifest)  # the token is never written into the Job


def test_kubernetes_posts_the_job_to_the_namespace_and_reports_a_refusal(monkeypatch):
    seen = {}

    class Kube(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            seen["path"], seen["auth"] = self.path, self.headers["Authorization"]
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(403 if seen["path"].startswith("/apis/batch/v1/namespaces/locked/") else 201)
            self.end_headers()
            self.wfile.write(b'{"message": "forbidden"}')

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Kube)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    api = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("DISPATCH_KUBE_TOKEN", "sa-token")
    monkeypatch.setenv("DISPATCH_NAMESPACE", "media")
    job = {"kind": "audio-cut", "job": "job-1", "args": {}}
    try:
        dispatch.start_kubernetes(job, {"image": "reg/media:1"}, ("https://marvin.example", "t"), api=api)
        assert seen["path"] == "/apis/batch/v1/namespaces/media/jobs" and seen["auth"] == "Bearer sa-token" and seen["body"]["kind"] == "Job"

        monkeypatch.setenv("DISPATCH_NAMESPACE", "locked")
        with pytest.raises(dispatch.LaunchError, match="403"):
            dispatch.start_kubernetes(job, {"image": "reg/media:1"}, ("https://marvin.example", "t"), api=api)
    finally:
        server.shutdown()


def test_http_mode_posts_the_job_to_the_standing_worker(monkeypatch):
    seen = {}

    class Worker(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            seen["path"], seen["auth"] = self.path, self.headers["Authorization"]
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(202)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Worker)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("DISPATCH_WORKER_TOKEN", "w-secret")
    try:
        dispatch.start_http({"kind": "audio-cut", "job": "job-1", "args": {"a": 1}}, {"url": f"http://127.0.0.1:{server.server_port}/"}, ("u", "t"))
        assert seen == {"path": "/jobs", "auth": "Bearer w-secret", "body": {"kind": "audio-cut", "job": "job-1", "args": {"a": 1}}}
        with pytest.raises(dispatch.LaunchError, match="no worker URL"):
            dispatch.start_http({"kind": "k", "job": "j", "args": {}}, {}, ("u", "t"))
    finally:
        server.shutdown()


class _Docker:
    """Stands in for the docker CLI: `ps` lists exited worker containers, `inspect` gives an exit code, `logs` some output."""

    def __init__(self, containers, code="2", logs="error: WORKER_TOKEN is not set"):
        self.containers, self.code, self.logs, self.calls = containers, code, logs, []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd[1])
        out = {"ps": "".join(f"{n}\t{slug}\n" for n, slug in self.containers), "inspect": self.code + "\n", "logs": self.logs}.get(cmd[1], "")
        return subprocess.CompletedProcess(cmd, 0, out, "")


def test_a_worker_container_that_died_before_reporting_fails_its_job_with_what_it_said_and_is_cleaned_up(marvin, monkeypatch):
    FakeMarvin.rows = [_job("job-1", status="claimed")]
    docker = _Docker([("mw-job-1", "job-1")])
    monkeypatch.setattr(dispatch.subprocess, "run", docker)

    assert dispatch.reap_docker(marvin) == 1

    assert _data("job-1")["status"] == "failed" and "code 2" in _data("job-1")["log"] and "WORKER_TOKEN is not set" in _data("job-1")["log"]
    assert docker.calls[-1] == "rm"  # the container is removed once read


def test_a_container_that_finished_after_reporting_is_only_cleaned_up_not_failed(marvin, monkeypatch):
    FakeMarvin.rows = [_job("job-1", status="done")]
    docker = _Docker([("mw-job-1", "job-1")], code="0")
    monkeypatch.setattr(dispatch.subprocess, "run", docker)

    assert dispatch.reap_docker(marvin) == 1

    assert _data("job-1")["status"] == "done" and "inspect" not in docker.calls and docker.calls[-1] == "rm"


def test_the_dispatcher_reaps_for_the_modes_in_use_before_it_starts_anything(marvin):
    FakeMarvin.rows = [_kind(mode="docker")]
    reaped = []

    dispatch.Dispatcher(marvin, launchers={}, reapers={"docker": lambda creds: reaped.append(creds)}, now=lambda: NOW).tick()

    assert reaped == [marvin]


def test_the_dispatcher_reports_in_once_a_minute_with_what_it_has_done(marvin):
    FakeMarvin.rows = [_kind(), _job("job-1")]
    clock = [NOW]
    dispatcher = dispatch.Dispatcher(marvin, launchers={"local": lambda job, kind, c: None}, reapers={}, now=lambda: clock[0])

    dispatcher.tick()
    beat = next(r for r in FakeMarvin.rows if r["slug"].startswith("worker-dispatcher-"))
    assert beat["dataJson"]["last_seen"] == NOW.isoformat() and beat["dataJson"]["modes"] == "local" and beat["dataJson"]["role"] == "dispatcher"

    clock[0] = NOW + timedelta(seconds=20)
    FakeMarvin.rows.append(_job("job-2"))
    dispatcher.tick()
    assert beat["dataJson"]["last_seen"] == NOW.isoformat()  # not yet a minute: no second write

    clock[0] = NOW + timedelta(seconds=75)
    dispatcher.tick()
    assert beat["dataJson"]["last_seen"] == clock[0].isoformat() and beat["dataJson"]["jobs_started"] == 2  # both jobs counted
    assert len([r for r in FakeMarvin.rows if r["slug"].startswith("worker-dispatcher-")]) == 1  # one record, updated in place
