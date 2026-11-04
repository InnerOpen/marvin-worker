"""The worker's one call to Marvin (marvin_api.request): a read is tried again after a blink, a write never is."""

import io
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


def test_a_read_that_loses_its_connection_is_tried_again_and_then_works(monkeypatch):
    calls = _urlopen(monkeypatch, [urllib.error.URLError("EOF occurred in violation of protocol"), ConnectionResetError("reset"), b"ok"])
    assert marvin_api.request(CREDS, "/a", pause=0) == b"ok" and len(calls) == 3


def test_a_busy_answer_is_tried_again_but_a_real_refusal_is_not(monkeypatch):
    busy = urllib.error.HTTPError("u", 503, "busy", {}, io.BytesIO(b"later"))
    calls = _urlopen(monkeypatch, [busy, b"ok"])
    assert marvin_api.request(CREDS, "/a", pause=0) == b"ok" and len(calls) == 2
    denied = urllib.error.HTTPError("u", 403, "no", {}, io.BytesIO(b"no"))
    calls = _urlopen(monkeypatch, [denied])
    with pytest.raises(marvin_api.MarvinError, match="403") and pytest.raises(marvin_api.MarvinError):
        marvin_api.request(CREDS, "/a", pause=0)
    assert len(calls) == 1


def test_it_gives_up_after_a_few_goes_and_says_it_cannot_reach_marvin(monkeypatch):
    calls = _urlopen(monkeypatch, [urllib.error.URLError("down")])
    with pytest.raises(marvin_api.MarvinError, match="can't reach Marvin"):
        marvin_api.request(CREDS, "/a", pause=0)
    assert len(calls) == marvin_api.READ_ATTEMPTS


def test_a_write_is_never_repeated(monkeypatch):
    calls = _urlopen(monkeypatch, [urllib.error.URLError("dropped")])
    with pytest.raises(marvin_api.MarvinError):
        marvin_api.request(CREDS, "/a", method="POST", data=b"{}", pause=0)
    assert calls == ["POST"]
