"""The dispatcher: which queued jobs it starts, how each mode starts a worker, and what it tells that worker."""

import json
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from marvin_worker import backend as backends
from marvin_worker import dispatch, jobs, marvin_api, runner, serve

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
    return dispatch.Dispatcher(creds, launchers={"local": lambda job, kind, c: started.append((job, kind, c))}, reapers={}, now=lambda: NOW, claim_settle=0, **kw)


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

    dispatch.Dispatcher(marvin, launchers={"local": refuse}, reapers={}, now=lambda: NOW, claim_settle=0).tick()

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


def test_what_a_finished_container_printed_is_kept_on_its_job_after_the_jobs_own_log(marvin, monkeypatch):
    FakeMarvin.rows = [_job("job-1", status="failed", log="JobError: story has no lines")]
    monkeypatch.setattr(dispatch.subprocess, "run", _Docker([("mw-job-1", "job-1")], code="0", logs="pulling beats\ncutting 27 lines"))

    dispatch.reap_docker(marvin)

    kept = _data("job-1")["log"]
    assert kept.startswith("JobError: story has no lines") and dispatch.OUTPUT_MARK in kept and kept.endswith("cutting 27 lines")


def test_a_job_whose_container_output_is_already_kept_or_empty_is_left_alone(marvin, monkeypatch):
    FakeMarvin.rows = [_job("job-1", status="done", log=f"ok\n\n{dispatch.OUTPUT_MARK}\nearlier"), _job("job-2", status="done", log="ok")]
    monkeypatch.setattr(dispatch.subprocess, "run", _Docker([("mw-job-1", "job-1"), ("mw-job-2", "job-2")], code="0", logs=""))

    dispatch.reap_docker(marvin)

    assert _data("job-1")["log"].endswith("earlier") and _data("job-2")["log"] == "ok"


def test_the_dispatcher_reaps_for_the_modes_in_use_before_it_starts_anything(marvin):
    FakeMarvin.rows = [_kind(mode="docker")]
    reaped = []

    dispatch.Dispatcher(marvin, launchers={}, reapers={"docker": lambda creds: reaped.append(creds)}, now=lambda: NOW, claim_settle=0).tick()

    assert reaped == [marvin]


def test_the_dispatcher_reports_in_once_a_minute_with_what_it_has_done(marvin):
    FakeMarvin.rows = [_kind(), _job("job-1")]
    clock = [NOW]
    dispatcher = dispatch.Dispatcher(marvin, launchers={"local": lambda job, kind, c: None}, reapers={}, now=lambda: clock[0], claim_settle=0)

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


# ── runner, contracts and the backend seam ────────────────────────────────────


@pytest.fixture
def audio_cut():
    @jobs.job_kind("audio-cut-t", description="Stitch a story's lines", inputs={"story": "string", "source": {"type": "asset", "accepts": ["audio/*"], "required": False}, "size": "integer?"}, produces=["audio/mpeg"], needs=["ffmpeg"])
    def audio_cut(job, ctx):
        return {"log": f"cut {job['args']['story']}", "result": {"seconds": 3}}

    yield audio_cut
    jobs.HANDLERS.pop("audio-cut-t", None)
    jobs.META.pop("audio-cut-t", None)


def test_a_kind_declares_what_it_takes_gives_and_needs_and_the_worker_can_describe_it(audio_cut):
    (contract,) = jobs.describe_kinds(["audio-cut-t"])

    assert contract["description"] == "Stitch a story's lines" and contract["produces"] == ["audio/mpeg"] and contract["needs"] == ["ffmpeg"]
    assert contract["inputs"]["story"] == {"type": "string", "required": True}
    assert contract["inputs"]["size"]["required"] is False and contract["inputs"]["source"]["accepts"] == ["audio/*"]
    assert jobs.describe_kinds(["nothing-like-it"]) == []


def test_arguments_that_do_not_fit_the_contract_are_refused_with_every_problem_in_plain_words(audio_cut):
    with pytest.raises(jobs.JobError) as e:
        jobs.check_args("audio-cut-t", {"size": "big"})
    assert "“story” is needed" in str(e.value) and "“size” should be a integer" in str(e.value)
    jobs.check_args("audio-cut-t", {"story": "the-last-onion", "extra": "kept for the kind"})  # extras are the kind's own business
    jobs.check_args("not-registered", {})  # a kind with no contract takes anything


def test_a_file_of_the_wrong_kind_is_refused_before_anything_runs(audio_cut, marvin, monkeypatch):
    monkeypatch.setattr(marvin_api, "asset", lambda creds, ref: {"mimeType": "application/pdf" if ref == "report" else "audio/mpeg"})

    jobs.check_args("audio-cut-t", {"story": "s", "source": "take-1"}, marvin)  # audio: fine
    with pytest.raises(jobs.JobError, match="“source” is application/pdf, but audio-cut-t takes audio/\\*"):
        jobs.check_args("audio-cut-t", {"story": "s", "source": "report"}, marvin)


def test_a_job_that_fails_its_contract_fails_on_its_record_and_runs_nothing(audio_cut, marvin):
    FakeMarvin.rows = [_job("job-1", status="claimed", kind="audio-cut-t")]
    FakeMarvin.rows[0]["dataJson"]["args"] = {}

    assert serve.run_job({"kind": "audio-cut-t", "job": "job-1", "args": {}}, marvin) is False

    assert _data("job-1")["status"] == "failed" and "“story” is needed" in _data("job-1")["log"]


def test_a_runner_announces_what_it_can_do_then_takes_a_queued_job_of_that_kind_and_reports_the_outcome(audio_cut, marvin):
    FakeMarvin.rows = [_job("other", kind="something-else"), _job("job-1", kind="audio-cut-t")]
    FakeMarvin.rows[1]["dataJson"]["args"] = {"story": "the-last-onion"}
    clock = [NOW]
    r = runner.Runner(backends.MarvinBackend(marvin, settle=0), marvin, name="box-1", only=["audio-cut-t"], now=lambda: clock[0])

    assert r.step() is True  # took job-1, left the one it can't do

    beat = next(x for x in FakeMarvin.rows if x["slug"] == "worker-runner-box-1")["dataJson"]
    assert beat["role"] == "runner" and beat["status"] == "idle" and beat["jobs_done"] == 1 and beat["tags"] == "ffmpeg"
    assert [k["kind"] for k in beat["kinds"]] == ["audio-cut-t"] and beat["kinds"][0]["inputs"]["story"]["required"] is True  # the contract is advertised
    assert _data("job-1")["status"] == "done" and _data("job-1")["claimed_by"] == "box-1" and _data("job-1")["result"] == {"seconds": 3}
    assert _data("other")["status"] == "queued"  # not its kind: left for a worker that has it
    assert r.step() is False  # nothing left it can do


def test_only_one_of_two_workers_reaching_for_a_job_keeps_it(marvin):
    FakeMarvin.rows = [_job("job-1", kind="k")]

    first = marvin_api.claim(marvin, "job-1", "box-1", NOW.isoformat(), settle=0)
    second = marvin_api.claim(marvin, "job-1", "box-2", NOW.isoformat(), settle=0)  # it is not queued any more

    assert (first, second) == (True, False) and _data("job-1")["claimed_by"] == "box-1" and _data("job-1")["attempts"] == 1


def test_a_worker_that_lost_the_race_to_a_later_write_backs_off(marvin, monkeypatch):
    FakeMarvin.rows = [_job("job-1", kind="k")]
    real = marvin_api.update_entry

    def rival_writes_after_us(creds, slug, changes):
        real(creds, slug, changes)
        real(creds, slug, {"claimed_by": "box-2"})  # the other worker's write lands right after ours

    monkeypatch.setattr(marvin_api, "update_entry", rival_writes_after_us)

    assert marvin_api.claim(marvin, "job-1", "box-1", NOW.isoformat(), settle=0) is False


def test_a_runner_can_offer_just_some_of_its_kinds(audio_cut):
    @jobs.job_kind("second-t")
    def second(job, ctx):
        return {}

    try:
        r = runner.Runner(backends.MarvinBackend(("u", "t")), ("u", "t"), name="b", only=["second-t"])
        assert r.kinds == ["second-t"] and r.tags == []
    finally:
        jobs.HANDLERS.pop("second-t", None)
        jobs.META.pop("second-t", None)


def test_the_heartbeat_goes_to_the_sinks_too_and_a_dead_sink_stops_nothing(marvin):
    received = []

    class Sink(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))) | {"auth": self.headers["Authorization"]})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Sink)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        fan = backends.Fanout(backends.MarvinBackend(marvin, settle=0), [backends.HttpSink([f"http://127.0.0.1:{server.server_port}", "http://127.0.0.1:9"], "Bearer s")])
        fan.announce({"role": "runner", "host": "box-1", "status": "idle", "kinds": []})
    finally:
        server.shutdown()

    assert received[0]["event"] == "announce" and received[0]["worker"]["host"] == "box-1" and received[0]["auth"] == "Bearer s"
    assert any(x["slug"] == "worker-runner-box-1" for x in FakeMarvin.rows)  # and Marvin's own record, the primary


def test_a_queue_backend_is_named_by_module_and_loaded_the_same_way_as_a_job_kind(tmp_path, monkeypatch):
    (tmp_path / "fake_queue.py").write_text("from marvin_worker.backend import Backend\n\nclass Q(Backend):\n    def __init__(self, creds):\n        self.creds = creds\n\ndef make_backend(creds):\n    return Q(creds)\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("WORKER_BACKEND", "fake_queue")
    monkeypatch.delenv("WORKER_SINKS", raising=False)

    assert type(backends.load_backend(("u", "t"))).__name__ == "Q"
