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


def test_an_asset_named_by_id_is_used_as_it_is(monkeypatch):
    seen = _routes(monkeypatch, {"api/platform/assets/abc": b'{"id": "abc", "name": "A"}'})
    assert marvin_api.asset_id(CREDS, "abc") == "abc" and seen == ["api/platform/assets/abc"]


def test_an_asset_named_by_slug_is_looked_up_in_the_library(monkeypatch):
    rows = b'[{"id": "id-1", "slug": "other-take", "name": "x"}, {"id": "id-2", "slug": "take", "name": "Take"}]'
    seen = _routes(monkeypatch, {"api/platform/assets?q=take": rows, "api/platform/assets/id-2": b'{"id": "id-2", "name": "Take"}', "api/platform/assets/id-2/file": b"audio"})
    assert marvin_api.asset(CREDS, "take")["name"] == "Take"
    assert marvin_api.asset_id(CREDS, "take") == "id-2"
    assert "api/platform/assets/id-2" in seen


def test_a_name_that_matches_nothing_or_two_assets_is_refused_with_the_name(monkeypatch):
    _routes(monkeypatch, {"api/platform/assets?q=none": b"[]", "api/platform/assets?q=dup": b'[{"id": "1", "slug": "dup"}, {"id": "2", "slug": "dup"}]'})
    with pytest.raises(marvin_api.MarvinError, match="no asset “none”"):
        marvin_api.asset_id(CREDS, "none")
    with pytest.raises(marvin_api.MarvinError, match="no single asset is called “dup”"):
        marvin_api.asset_id(CREDS, "dup")
