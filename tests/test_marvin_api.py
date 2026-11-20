"""The worker's one call to Marvin (marvin_api.request): what failed on the line is tried again, what the request itself got wrong is not,
and a write that may already have happened is never done twice."""

import http.client
import io
import json
import socket
import threading
import urllib.error

import pytest

from marvin_worker import marvin_api

CREDS = ("https://marvin.example", "tok")


class _Reply:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _urlopen(monkeypatch, outcomes):
    calls = []

    def fake(req, timeout=0):
        calls.append(req.get_method())
        outcome = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return _Reply(outcome)

    monkeypatch.setattr(marvin_api.urllib.request, "urlopen", fake)
    return calls


def _http_error(code, body=b"", headers=None):
    return urllib.error.HTTPError("u", code, "x", headers or {}, io.BytesIO(body))


NEVER_SENT = urllib.error.URLError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1010)")  # the handshake was cut
DROPPED = http.client.RemoteDisconnected("Remote end closed connection without response")  # sent, and no answer came


def test_a_read_that_loses_its_connection_is_tried_again_and_then_works(monkeypatch):
    calls = _urlopen(monkeypatch, [NEVER_SENT, ConnectionResetError("reset"), DROPPED, b"ok"])
    assert marvin_api.request(CREDS, "/a", pause=0) == b"ok" and len(calls) == 4


def test_a_busy_answer_is_tried_again_but_a_real_refusal_is_not(monkeypatch):
    calls = _urlopen(monkeypatch, [_http_error(503, b"later"), _http_error(429), b"ok"])
    assert marvin_api.request(CREDS, "/a", method="POST", data=b"{}", pause=0) == b"ok" and len(calls) == 3  # turned away before it was done: any call
    for code in (400, 403, 404, 409, 422, 500):
        calls = _urlopen(monkeypatch, [_http_error(code, b"no")])
        with pytest.raises(marvin_api.MarvinError, match=str(code)) as e:
            marvin_api.request(CREDS, "/a", pause=0)
        assert len(calls) == 1 and not isinstance(e.value, marvin_api.MarvinUnreachable)


def test_a_retry_after_is_waited_for_and_otherwise_each_wait_is_longer(monkeypatch):
    waits = []
    monkeypatch.setattr(marvin_api.time, "sleep", waits.append)
    _urlopen(monkeypatch, [_http_error(429, headers={"Retry-After": "7"}), _http_error(429, headers={"Retry-After": "600"}), b"ok"])
    assert marvin_api.request(CREDS, "/a") == b"ok" and waits == [7.0, marvin_api.PAUSE_CAP]  # a long ask is cut to the cap
    first, third, fifth = (marvin_api._pause(1.0, n, None) for n in (1, 3, 5))
    assert 1 <= first <= 1.5 and 4 <= third <= 6 and 16 <= fifth <= marvin_api.PAUSE_CAP


def test_it_gives_up_after_every_attempt_and_says_why_and_how_many(monkeypatch):
    calls = _urlopen(monkeypatch, [DROPPED, DROPPED, NEVER_SENT])
    with pytest.raises(marvin_api.MarvinUnreachable, match=r"can't reach Marvin \(\[SSL: UNEXPECTED_EOF.*\) after 6 attempts") as e:
        marvin_api.request(CREDS, "/a", pause=0)
    assert isinstance(e.value, marvin_api.MarvinError) and len(calls) == marvin_api.ATTEMPTS == 6  # callers that catch MarvinError still do


def test_a_write_that_never_reached_marvin_is_sent_again(monkeypatch):
    calls = _urlopen(monkeypatch, [NEVER_SENT, NEVER_SENT, b'{"id": "1"}'])
    assert marvin_api.request(CREDS, "/api/platform/assets/upload", method="POST", data=b"x", pause=0) == b'{"id": "1"}' and calls == ["POST"] * 3


def test_a_write_whose_answer_was_lost_is_not_sent_twice_unless_it_is_the_same_sent_twice(monkeypatch):
    calls = _urlopen(monkeypatch, [DROPPED, b"{}"])
    with pytest.raises(marvin_api.MarvinUnreachable, match="may have been done"):
        marvin_api.request(CREDS, "/api/ai/agents/chat/run", method="POST", data=b"{}", pause=0)
    assert calls == ["POST"]
    calls = _urlopen(monkeypatch, [_http_error(502, b"bad gateway"), b"{}"])  # a proxy lost the answer: the same
    with pytest.raises(marvin_api.MarvinError, match="502"):
        marvin_api.request(CREDS, "/api/platform/assets/upload", method="POST", data=b"{}", pause=0)
    assert calls == ["POST"]
    for method, path in (("PATCH", "/api/platform/entries/1"), ("DELETE", "/api/platform/entries/1"), ("POST", "/api/platform/entries/query")):
        calls = _urlopen(monkeypatch, [DROPPED, TimeoutError("timed out"), _http_error(504), b"{}"])
        assert marvin_api.request(CREDS, path, method=method, data=b"{}", pause=0) == b"{}" and calls == [method] * 4


def test_a_delete_whose_lost_answer_was_done_is_not_an_error_the_second_time(monkeypatch):
    _urlopen(monkeypatch, [DROPPED, _http_error(404, b"gone")])
    assert marvin_api.request(CREDS, "/api/platform/assets/a1", method="DELETE", pause=0) == b""
    _urlopen(monkeypatch, [_http_error(404, b"gone")])
    with pytest.raises(marvin_api.MarvinError, match="404"):  # but a first answer of 404 is a real one
        marvin_api.request(CREDS, "/api/platform/assets/a1", method="DELETE", pause=0)


def _creating(monkeypatch, posts, there):
    """POST /entries answers from `posts` in turn; a look by slug finds `there()` (a row or None)."""
    seen = []

    def fake(req, timeout=0):
        path = req.full_url.split("://", 1)[1].split("/", 1)[1]
        seen.append(f"{req.get_method()} {path}")
        if path == "api/platform/entries/query":
            row = there()
            return _Reply(json.dumps({"entries": [row] if row else []}).encode())
        if req.get_method() == "DELETE":
            return _Reply(b"{}")
        outcome = posts.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _Reply(json.dumps(outcome).encode())

    monkeypatch.setattr(marvin_api.urllib.request, "urlopen", fake)
    monkeypatch.setattr(marvin_api.time, "sleep", lambda s: None)
    return seen


def test_a_create_whose_answer_was_lost_is_found_by_its_slug_and_not_made_twice(monkeypatch):
    row = {"id": "e1", "slug": "s-beat-1001", "title": "Beat"}
    seen = _creating(monkeypatch, [DROPPED], lambda: row)  # it landed: Marvin made it, then the answer was lost
    assert marvin_api.create_entry(CREDS, "T", "s-beat-1001", "Beat", {}) == row
    assert seen == ["POST api/platform/entries", "POST api/platform/entries/query"]


def test_a_create_that_did_not_land_is_sent_again(monkeypatch):
    made = {"id": "e1", "slug": "s-beat-1001"}
    seen = _creating(monkeypatch, [DROPPED, made], lambda: None)
    assert marvin_api.create_entry(CREDS, "T", "s-beat-1001", "Beat", {}) == made
    assert seen == ["POST api/platform/entries", "POST api/platform/entries/query", "POST api/platform/entries"]


def test_a_create_whose_first_send_landed_late_keeps_one_record(monkeypatch):
    first, extra = {"id": "e1", "slug": "s-beat-1001"}, {"id": "e2", "slug": "s-beat-1001-2"}  # Marvin gives a taken slug the next free one
    looks = iter([None, first])  # not there when looked for, there once the second send had made its own
    seen = _creating(monkeypatch, [DROPPED, extra], lambda: next(looks))
    assert marvin_api.create_entry(CREDS, "T", "s-beat-1001", "Beat", {}) == first
    assert seen[-1] == "DELETE api/platform/entries/e2"


def test_each_retry_is_a_line_of_the_jobs_own_log(monkeypatch, capsys):
    _urlopen(monkeypatch, [DROPPED, NEVER_SENT, b"ok"])
    said = []
    with marvin_api.telling(said.append):
        marvin_api.request(CREDS, "/a", pause=0)
    assert said == ["retrying after a dropped connection (attempt 2 of 6)", "retrying after a failed connection (attempt 3 of 6)"]
    _urlopen(monkeypatch, [DROPPED, b"ok"])
    marvin_api.request(CREDS, "/a", pause=0)  # no job: the worker's own log
    assert "retrying after a dropped connection (attempt 2 of 6)" in capsys.readouterr().out


def test_a_real_server_that_drops_the_connection_twice_is_reached_on_the_third_try():
    """No fake opener: a socket that reads the request and hangs up, as a proxy cutting a connection does, then answers. Each try is a
    connection of its own."""
    server = socket.create_server(("127.0.0.1", 0))
    connections = []

    def serve():
        for n in range(3):
            conn, _ = server.accept()
            connections.append(n)
            conn.recv(65536)
            if n == 2:
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    base = f"http://127.0.0.1:{server.getsockname()[1]}"
    try:
        assert marvin_api.request((base, "tok"), "/api/platform/entries/e1", method="PATCH", data=b"{}", pause=0) == b"ok"
        assert connections == [0, 1, 2]
    finally:
        server.close()


def _routes(monkeypatch, table):
    """Answer by path: a body, or an HTTPError for a path with no entry."""
    seen = []

    def fake(req, timeout=0):
        path = req.full_url.split("://", 1)[1].split("/", 1)[1]
        seen.append(path)
        if path in table:
            return _Reply(table[path])
        raise urllib.error.HTTPError(req.full_url, 422, "bad id", None, None)

    monkeypatch.setattr(marvin_api.urllib.request, "urlopen", fake)
    return seen


def test_an_asset_named_by_id_is_fetched_directly_with_no_lookup(monkeypatch):
    seen = _routes(monkeypatch, {"api/platform/assets/abc": b'{"mimeType": "audio/mpeg"}', "api/platform/assets/abc/file": b"audio"})
    assert marvin_api.asset(CREDS, "abc") == {"mimeType": "audio/mpeg"}  # no id in the reply is fine
    assert marvin_api.asset(CREDS, "abc") and seen == ["api/platform/assets/abc"] * 2


def test_an_asset_named_by_slug_is_looked_up_only_when_marvin_refuses_it_as_an_id(monkeypatch):
    rows = b'[{"id": "id-1", "slug": "other-take", "name": "x"}, {"id": "id-2", "slug": "take", "name": "Take"}]'
    seen = _routes(monkeypatch, {"api/platform/assets?q=take": rows, "api/platform/assets/id-2": b'{"name": "Take"}', "api/platform/assets/id-2/file": b"audio"})
    assert marvin_api.asset(CREDS, "take")["name"] == "Take"
    assert seen == ["api/platform/assets/take", "api/platform/assets?q=take", "api/platform/assets/id-2"]


def test_a_name_that_matches_nothing_or_two_assets_is_refused_with_the_name(monkeypatch):
    _routes(monkeypatch, {"api/platform/assets?q=none": b"[]", "api/platform/assets?q=dup": b'[{"id": "1", "slug": "dup"}, {"id": "2", "slug": "dup"}]'})
    with pytest.raises(marvin_api.MarvinError, match="no asset “none”"):
        marvin_api.asset(CREDS, "none")
    with pytest.raises(marvin_api.MarvinError, match="no single asset is called “dup”"):
        marvin_api.asset(CREDS, "dup")


def test_entries_reads_every_page_and_keeps_an_entry_that_comes_round_twice_once(monkeypatch):
    monkeypatch.setattr(marvin_api, "PAGE", 2)
    pages = {0: [{"id": "c"}, {"id": "b"}], 2: [{"id": "b"}, {"id": "a"}], 4: [{"id": "z"}]}  # b: something was made between two pages
    asked = []

    def fake(creds, path, **_):
        asked.append(path)
        return json.dumps(pages[int(path.rsplit("offset=", 1)[1])]).encode()

    monkeypatch.setattr(marvin_api, "request", fake)
    assert [row["id"] for row in marvin_api.entries(("http://m", "tok"))] == ["c", "b", "a", "z"]
    assert asked == [f"/api/platform/entries?limit=2&offset={n}" for n in (0, 2, 4)]


def test_entries_stops_at_a_short_first_page(monkeypatch):
    asked = []
    monkeypatch.setattr(marvin_api, "request", lambda creds, path, **_: asked.append(path) or b'{"items": [{"id": "only"}]}')
    assert marvin_api.entries(("http://m", "tok")) == [{"id": "only"}]
    assert len(asked) == 1


def test_entries_keeps_every_row_that_has_no_id(monkeypatch):
    """Two rows without an id are two rows: only an id says that a row is one seen before."""
    monkeypatch.setattr(marvin_api, "request", lambda creds, path, **_: b'[{"slug": "a"}, {"slug": "b"}, {"id": "1"}, {"id": "1"}]')
    assert marvin_api.entries(("http://m", "tok")) == [{"slug": "a"}, {"slug": "b"}, {"id": "1"}]
