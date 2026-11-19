"""The generic worker: what it accepts over HTTP, how a kind is added, and what it writes back to Marvin's records."""

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from marvin_worker import jobs, marvin_api, serve


class FakeMarvin(BaseHTTPRequestHandler):
    """A Marvin that remembers: records in memory, uploads kept, a PATCH merged. Notes the user agent of every request."""

    entries: list = []
    uploads: list = []
    agents: list = []

    def _json(self, status, body):
        self.send_response(status)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_GET(self):  # noqa: N802
        FakeMarvin.agents.append(self.headers.get("User-Agent"))
        if self.path.startswith("/api/platform/entries"):
            return self._json(200, FakeMarvin.entries)
        if self.path == "/api/platform/assets/a1":
            return self._json(200, {"mimeType": "audio/mpeg"})
        if self.path == "/api/platform/assets/a1/file":
            self.send_response(200)
            self.end_headers()
            return self.wfile.write(b"bytes")
        self._json(404, {})

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.path == "/api/platform/entries/query":
            wanted = json.loads(body)["spec"]["entries"]
            hits = [e for e in FakeMarvin.entries if e["slug"] == wanted]
            return self._json(200, {"entries": hits, "total": len(hits)})
        if self.path == "/api/platform/assets/upload":
            FakeMarvin.uploads.append((self.headers["Content-Type"], body))
            return self._json(201, {"id": "asset-new"})
        self._json(404, {})

    def do_PATCH(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        row = next(e for e in FakeMarvin.entries if f"/{e['id']}" in self.path)
        row["dataJson"] = body["dataJson"]
        self._json(200, row)

    def log_message(self, *args):
        pass


@pytest.fixture
def marvin():
    FakeMarvin.uploads, FakeMarvin.agents = [], []
    FakeMarvin.entries = [{"id": "e1", "slug": "job-1", "dataJson": {"kept": "yes", "status": "queued"}}]
    server = HTTPServer(("127.0.0.1", 0), FakeMarvin)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield (f"http://127.0.0.1:{server.server_port}", "tok")
    server.shutdown()


@pytest.fixture
def hello():
    @jobs.job_kind("hello")
    def hello(job, ctx):
        assert ctx.work.is_dir()  # scratch space for the job
        return {"log": f"hello {job['args']['name']}"}

    yield hello
    del jobs.HANDLERS["hello"]


def _data():
    return FakeMarvin.entries[0]["dataJson"]


def test_a_job_needs_a_known_kind_and_the_record_to_write_to(hello):
    assert serve.validate({"kind": "hello", "job": " j ", "args": {"a": 1}}) == {"kind": "hello", "job": "j", "args": {"a": 1}}
    assert serve.validate({"kind": "hello", "job": "j"})["args"] == {}  # args are the kind's own business
    for bad in (None, [], {"kind": "nope", "job": "j"}, {"kind": "hello", "job": ""}, {"kind": "hello"}, {"kind": "hello", "job": "j", "args": []}):
        with pytest.raises(jobs.JobError):
            serve.validate(bad)


def test_a_new_kind_is_one_function_and_the_core_writes_its_outcome_back(marvin, hello):
    serve.run_job({"kind": "hello", "job": "job-1", "args": {"name": "world"}}, marvin)

    assert _data() == {"kept": "yes", "status": "done", "log": "hello world"}  # the rest of the record is kept


def test_a_job_that_fails_says_why_on_the_record_instead_of_vanishing(marvin):
    @jobs.job_kind("boom")
    def boom(job, ctx):
        raise jobs.JobError("the kettle is empty")

    try:
        serve.run_job({"kind": "boom", "job": "job-1"}, marvin)
    finally:
        del jobs.HANDLERS["boom"]

    assert _data()["status"] == "failed" and "the kettle is empty" in _data()["log"]


def test_the_images_own_modules_are_loaded_by_name(monkeypatch, tmp_path):
    (tmp_path / "pack_for_test.py").write_text('from marvin_worker.jobs import job_kind\n\n@job_kind("from-pack")\ndef f(job, ctx):\n    return {}\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        assert "from-pack" in jobs.load_kinds("pack_for_test")
    finally:
        jobs.HANDLERS.pop("from-pack", None)
    assert jobs.load_kinds("") == sorted(jobs.HANDLERS)  # none named: nothing more is loaded


def _post(base, body, token="secret"):
    req = urllib.request.Request(base + "/jobs", data=json.dumps(body).encode(), method="POST", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_the_service_takes_a_job_only_with_the_token_and_answers_before_doing_it(hello):
    done, seen = threading.Event(), []

    def run(job, creds):
        seen.append(job)
        done.set()

    server = serve.make_server(0, ("http://unused", "t"), "secret", run=run)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        assert _post(base, {"kind": "hello", "job": "j"}, token="wrong")[0] == 401
        assert _post(base, {"kind": "nope", "job": "j"})[0] == 422
        status, body = _post(base, {"kind": "hello", "job": "j", "args": {"name": "x"}})
        assert status == 202 and body["accepted"] == "j"
        assert done.wait(5) and seen == [{"kind": "hello", "job": "j", "args": {"name": "x"}}]
        assert json.loads(urllib.request.urlopen(base + "/health").read())["ok"] is True
    finally:
        server.shutdown()


def test_uploads_keep_the_files_name_and_every_request_names_the_tool(marvin, tmp_path):
    file = tmp_path / "cut.mp3"
    file.write_bytes(b"ID3")

    asset = marvin_api.upload_asset(marvin, file, slug="cut", name="Cut", mime="audio/mpeg", metadata={"seconds": 3})
    marvin_api.entries(marvin)

    assert asset["id"] == "asset-new"
    (content_type, body), = FakeMarvin.uploads
    assert content_type.startswith("multipart/form-data") and b'filename="cut.mp3"' in body and b"Content-Type: audio/mpeg" in body
    assert all(agent and "marvin-worker" in agent for agent in FakeMarvin.agents)  # the default Python agent is refused by Marvin's edge


def test_a_record_that_is_not_there_is_an_error_not_a_silent_skip(marvin):
    with pytest.raises(marvin_api.MarvinError, match="no record"):
        marvin_api.update_entry(marvin, "nope", {"status": "done"})


def test_a_job_can_run_the_marvin_cli_as_the_workers_token(tmp_path, monkeypatch):
    fake = tmp_path / "marvin"
    fake.write_text('#!/bin/sh\necho "{\\"args\\": \\"$*\\", \\"url\\": \\"$MARVIN_API_URL\\", \\"token\\": \\"$MARVIN_USER_TOKEN\\"}"\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")

    seen = jobs.Context(creds=("https://marvin.example", "pinned"), work=tmp_path).marvin("platform", "entries", "list")

    assert seen == {"args": "platform entries list --json", "url": "https://marvin.example", "token": "pinned"}
    fake.write_text("#!/bin/sh\necho nope >&2\nexit 3\n")
    with pytest.raises(jobs.JobError, match="nope"):
        jobs.Context(creds=("u", "t"), work=tmp_path).marvin("platform", "x")


def test_run_once_does_the_job_writes_the_record_and_exits_with_the_outcome(marvin, hello, monkeypatch):
    monkeypatch.setenv("MARVIN_API_URL", marvin[0])
    monkeypatch.setenv("MARVIN_USER_TOKEN", marvin[1])
    monkeypatch.setenv("WORKER_JOB_MODULES", "")

    assert serve.run_once(["--kind", "hello", "--job", "job-1", "--args", '{"name": "once"}']) == 0
    assert _data()["status"] == "done" and _data()["log"] == "hello once"

    monkeypatch.setenv("WORKER_JOB", json.dumps({"kind": "boom-not-registered", "job": "job-1"}))
    assert serve.run_once([]) == 2  # a job this image can't take is refused, not run

    @jobs.job_kind("boom")
    def boom(job, ctx):
        raise jobs.JobError("nope")

    try:
        monkeypatch.setenv("WORKER_JOB", json.dumps({"kind": "boom", "job": "job-1"}))
        assert serve.run_once([]) == 1  # failed: the record says why, and the process exit says so too (a CI step goes red)
    finally:
        del jobs.HANDLERS["boom"]
    assert _data()["status"] == "failed"


def test_a_job_logs_its_steps_and_keeps_what_it_noted_on_the_record_even_when_it_fails(capsys):
    @jobs.job_kind("talky")
    def talky(job, ctx):
        ctx.note("pulled 3 files")
        ctx.note("mixed the audio")
        if job["args"].get("fail"):
            raise jobs.JobError("the audio would not mix")
        return {"result": {"ok": True}, "log": "all done"}

    try:
        done, failed = [], []
        assert serve.run_job({"kind": "talky", "job": "job-9", "args": {}}, ("http://x", "t"), report=done.append) is True
        assert serve.run_job({"kind": "talky", "job": "job-10", "args": {"fail": True}}, ("http://x", "t"), report=failed.append) is False
    finally:
        del jobs.HANDLERS["talky"]

    ok = done[-1]
    assert ok["status"] == "done" and "pulled 3 files" in ok["log"] and "mixed the audio" in ok["log"] and "all done" in ok["log"]
    assert failed[-1]["status"] == "failed" and "pulled 3 files" in failed[-1]["log"] and failed[-1]["log"].endswith("the audio would not mix")  # how far it got, then why it stopped
    out = capsys.readouterr().out
    assert "job-9: started (talky)" in out and "job-9: pulled 3 files" in out and "job-9: done in" in out
    assert "job-10: FAILED in" in out and "the audio would not mix" in out


def _sent_metadata(body: bytes) -> dict:
    """The `metadata` form field of a multipart upload, as the JSON Marvin reads."""
    head = b'name="metadata"\r\n\r\n'
    start = body.index(head) + len(head)
    return json.loads(body[start : body.index(b"\r\n", start)])


def _upload(creds, tmp_path, slug, metadata=None):
    file = tmp_path / f"{slug}.mp3"
    file.write_bytes(b"ID3")
    return marvin_api.upload_asset(creds, file, slug=slug, name=slug, mime="audio/mpeg", metadata=metadata)


def test_a_file_a_job_uploads_says_which_kind_and_job_made_it_and_keeps_the_callers_own_metadata(marvin, tmp_path):
    given = {"kind": "subtitles", "derived_from": "asset-src", "story": "the-last-onion"}

    @jobs.job_kind("stamp-t")
    def stamp(job, ctx):
        _upload(ctx.creds, tmp_path, "plain")  # no metadata at all: called as Studio's jobs call it
        _upload(ctx.creds, tmp_path, "given", given)
        _upload(ctx.creds, tmp_path, "own", {"worker": {"note": "the caller's own"}})
        return {}

    try:
        assert serve.run_job({"kind": "stamp-t", "job": "job-1", "args": {}}, marvin) is True
    finally:
        del jobs.HANDLERS["stamp-t"]

    plain, with_given, own = (_sent_metadata(body) for _, body in FakeMarvin.uploads)
    assert plain == {"worker": {"kind": "stamp-t", "job": "job-1"}}
    assert with_given == {**given, "worker": {"kind": "stamp-t", "job": "job-1"}}  # `kind` and `derived_from` kept as given
    assert own == {"worker": {"note": "the caller's own"}}  # a caller's key is never overwritten
    assert given == {"kind": "subtitles", "derived_from": "asset-src", "story": "the-last-onion"}  # nor is the caller's dict changed


def test_a_file_uploaded_outside_a_job_has_no_stamp_even_after_a_job_that_failed(marvin, tmp_path):
    @jobs.job_kind("fails-t")
    def fails(job, ctx):
        _upload(ctx.creds, tmp_path, "inside")
        raise jobs.JobError("stopped half way")

    _upload(marvin, tmp_path, "before", {"seconds": 3})
    try:
        assert serve.run_job({"kind": "fails-t", "job": "job-1", "args": {}}, marvin) is False
    finally:
        del jobs.HANDLERS["fails-t"]
    _upload(marvin, tmp_path, "after")

    before, inside, after = (_sent_metadata(body) for _, body in FakeMarvin.uploads)
    assert before == {"seconds": 3} and after == {}  # exactly what the caller gave
    assert inside == {"worker": {"kind": "fails-t", "job": "job-1"}}


def test_jobs_running_at_once_on_two_threads_each_stamp_their_own_files(marvin, tmp_path):
    both_in = threading.Barrier(2, timeout=5)
    FakeMarvin.entries = [{"id": f"e{n}", "slug": f"job-{n}", "dataJson": {"status": "queued"}} for n in (1, 2)]

    def make(name):
        @jobs.job_kind(name)
        def interleave(job, ctx):
            both_in.wait()  # both jobs are now running at the same moment
            _upload(ctx.creds, tmp_path, f"{job['job']}-a")
            both_in.wait()
            _upload(ctx.creds, tmp_path, f"{job['job']}-b")
            return {}

    make("left-t"), make("right-t")
    ok = []
    try:
        runs = [threading.Thread(target=lambda k=k, j=j: ok.append(serve.run_job({"kind": k, "job": j, "args": {}}, marvin))) for k, j in (("left-t", "job-1"), ("right-t", "job-2"))]
        for t in runs:
            t.start()
        for t in runs:
            t.join(10)
    finally:
        del jobs.HANDLERS["left-t"], jobs.HANDLERS["right-t"]

    assert ok == [True, True]
    stamps = {}
    for _, body in FakeMarvin.uploads:
        slug = body.split(b'name="slug"\r\n\r\n', 1)[1].split(b"\r\n", 1)[0].decode()
        stamps[slug] = _sent_metadata(body)["worker"]
    assert stamps == {
        "job-1-a": {"kind": "left-t", "job": "job-1"}, "job-1-b": {"kind": "left-t", "job": "job-1"},
        "job-2-a": {"kind": "right-t", "job": "job-2"}, "job-2-b": {"kind": "right-t", "job": "job-2"},
    }  # fmt: skip
