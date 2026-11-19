"""The few calls the worker makes to Marvin itself: read records, download an asset, upload an asset, merge fields into a record.
Standard library only. Python's default user agent is refused by Marvin's edge, so every request names this tool. Anything
beyond these calls a job does with the Marvin CLI (`ctx.marvin(...)`) rather than another client here."""

import http.client
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from . import log

USER_AGENT = "marvin-worker/0.1"
PAGE = 500


class MarvinError(RuntimeError):
    pass


class MarvinUnreachable(MarvinError):
    """The line to Marvin failed, not the request: Marvin could not be reached (or stayed busy) through every attempt, or the connection
    dropped after a write was sent that cannot safely be sent twice. The same job run again later can carry on."""


Creds = tuple[str, str]  # Marvin's address and a token pinned to the workspace


ATTEMPTS = 6  # the first try and five more, about 30 to 45 s of waiting in all: enough for a proxy's blink, not for an outage
PAUSE_CAP = 20.0  # the longest wait between two tries, a Retry-After included
TIMEOUT = 120  # seconds for each step on the socket (connecting, each read), not for the whole call: an AI run or a big upload answers slowly
NOT_DONE = (429, 503)  # Marvin turned the call away before doing it: any call can be sent again
ANSWER_LOST = (502, 504)  # a proxy lost Marvin's answer to a call Marvin may have done: the same as a connection that dropped
RESENDABLE = ("GET", "HEAD", "PUT", "PATCH", "DELETE")  # sent twice, Marvin ends as if sent once (a PATCH here always sends the whole dataJson)
READS = ("/api/platform/entries/query",)  # a POST that only reads

_tell: ContextVar["Callable[[str], None] | None"] = ContextVar("marvin_api_tell", default=None)


@contextmanager
def telling(say: Callable[[str], None]) -> Iterator[None]:
    """While a job runs, each retry is a line of its own log (`ctx.note`), so the record shows the blinks it lived through."""
    token = _tell.set(say)
    try:
        yield
    finally:
        _tell.reset(token)


_job: ContextVar["dict[str, str] | None"] = ContextVar("marvin_api_job", default=None)


@contextmanager
def working(kind: str, job: str) -> Iterator[None]:
    """While a job runs, every file it uploads is stamped `metadata.worker = {kind, job}` (see `upload_asset`), so a workflow can tell a
    worker's file from one a person uploaded. The worker sets this around each job; a job never has to. A ContextVar, so each thread (and
    so each job) sees only its own, and nothing is left behind when the job ends, however it ends."""
    token = _job.set({"kind": kind, "job": job})
    try:
        yield
    finally:
        _job.reset(token)


def stamped(metadata: dict | None) -> dict:
    """The metadata an upload sends: the caller's, untouched, plus `worker` when a job is running and the caller did not set one."""
    meta = dict(metadata or {})
    if (who := _job.get()) is not None:
        meta.setdefault("worker", dict(who))
    return meta


def _say(text: str) -> None:
    (_tell.get() or log.say)(text)


def _pause(pause: float, attempt: int, retry_after: "float | None") -> float:
    """How long to wait before the next try: what Marvin asked for, else twice as long each time with a little jitter, so workers that lost
    the line together do not all come back at the same moment."""
    if retry_after is not None:
        return min(PAUSE_CAP, retry_after)
    return min(PAUSE_CAP, pause * 2 ** (attempt - 1) * random.uniform(1.0, 1.5))


def _retry_after(e: urllib.error.HTTPError) -> "float | None":
    try:
        return max(0.0, float((e.headers or {}).get("Retry-After") or ""))
    except (TypeError, ValueError):
        return None  # absent, or a date: the usual pause


def request(
    creds: Creds,
    path: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    content_type: str | None = None,
    pause: float = 1.0,
    attempts: int = ATTEMPTS,
    done: "Callable[[], bytes | None] | None" = None,
) -> bytes:
    """One call to Marvin, tried again (up to `attempts` times, waiting longer each time) when what failed was the line and not the request.

    Each try opens its own connection (urllib sends `Connection: close` and never reuses one), so a retry never rides a connection that died.
    - Never sent (refused, a TLS handshake cut, the request not sent whole: urllib's URLError) or turned away (429, 503): any call is sent again.
    - Sent, and the answer lost (the connection dropped while Marvin answered, a read timed out, a proxy's 502 or 504): only a call that is the
      same sent twice is sent again: a read, a PATCH, a DELETE, a query. A create passes `done`, which looks after a pause for what it would
      have made and returns Marvin's answer if it is there (it is then not sent again) or None. Any other write (an upload, an AI run) is
      not repeated: it fails with MarvinUnreachable, saying it may have been done.
    - Any other answer (a 4xx but 429, a 500) is the request's own and fails at once, as MarvinError.
    The final failure is MarvinUnreachable, naming the last reason and how many tries were made."""
    base, token = creds
    headers = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT, "Accept": "*/*"}
    if content_type:
        headers["Content-Type"] = content_type
    resendable = method in RESENDABLE or (method == "POST" and path.split("?")[0] in READS)
    lost = False  # a try was sent and its answer never came
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method, headers=headers)
        wait = None
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404 and method == "DELETE" and lost:
                return b""  # the try whose answer was lost did it
            sent = e.code in ANSWER_LOST
            if e.code not in NOT_DONE + ANSWER_LOST or (sent and not resendable and done is None):
                raise MarvinError(f"Marvin answered {e.code} for {path}: {e.read()[:200].decode('utf-8', 'replace')}") from e
            reason, wait = f"Marvin answered {e.code}", _retry_after(e)
            what = "a busy answer" if e.code in NOT_DONE else f"a lost answer ({e.code})"
        except urllib.error.URLError as e:  # urllib raises it only while connecting or sending: the request never reached Marvin whole
            sent, reason, what = False, str(e.reason), "a failed connection"
        except (OSError, http.client.HTTPException) as e:  # sent, then the connection dropped (or timed out) before the whole answer came
            sent, reason, what = True, str(e) or type(e).__name__, "a dropped connection"
            if not resendable and done is None:
                raise MarvinUnreachable(f"can't reach Marvin ({reason}): the connection dropped after {method} {path} was sent, and it is not sent twice, as it may have been done") from e
        lost = lost or sent
        look = sent and done is not None  # a create whose answer was lost: did it happen?
        if attempt < attempts or look:
            time.sleep(_pause(pause, attempt, wait))  # before looking too: the lost try may still be landing
        if look and (answer := done()) is not None:
            return answer
        if attempt == attempts:
            raise MarvinUnreachable(f"can't reach Marvin ({reason}) after {attempts} attempts")
        _say(f"retrying after {what} (attempt {attempt + 1} of {attempts})")
    raise MarvinUnreachable(f"can't reach Marvin for {path}")  # not reached


def entries(creds: Creds) -> list[dict]:
    body = json.loads(request(creds, f"/api/platform/entries?limit={PAGE}"))
    return body if isinstance(body, list) else body.get("items", [])


def query(creds: Creds, spec: dict, *, limit: int = 100) -> list[dict]:
    """Records matching a query spec (`{"entry_type": "job", "data": {"status": "queued"}}`): Marvin does the filtering."""
    body = json.dumps({"spec": spec, "limit": limit}).encode()
    return json.loads(request(creds, "/api/platform/entries/query", method="POST", data=body, content_type="application/json")).get("entries", [])


def asset_id(creds: Creds, ref: str) -> str:
    """The id of the asset a person named by slug (or the exact name): found in the library, and refused when nothing, or more than one
    thing, has that name. Only used when the reference was not itself an id (see `asset` and `download_asset`)."""
    rows = json.loads(request(creds, "/api/platform/assets?q=" + urllib.parse.quote(ref)))
    hits = [r for r in rows if r.get("slug") == ref] or [r for r in rows if r.get("name") == ref]
    if len(hits) != 1:
        raise MarvinError(f"no single asset is called “{ref}”" if hits else f"no asset “{ref}”")
    return str(hits[0]["id"])


def _by_ref(creds: Creds, ref: str, tail: str = "") -> bytes:
    """GET an asset (or its file) by the reference given: as it is first (an id), and only if Marvin refuses it, as a slug or name."""
    try:
        return request(creds, f"/api/platform/assets/{ref}{tail}")
    except MarvinError:
        return request(creds, f"/api/platform/assets/{asset_id(creds, ref)}{tail}")


def asset(creds: Creds, ref: str) -> dict:
    return json.loads(_by_ref(creds, ref))


def download_asset(creds: Creds, ref: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(_by_ref(creds, ref, "/file"))


def upload_asset(creds: Creds, file: Path, *, slug: str, name: str, mime: str, description: str = "", metadata: dict | None = None) -> dict:
    """Upload a file as a new asset (multipart, like the Assets page). The file keeps its name, because Marvin reads a file's type
    from its extension. Inside a job the metadata also carries `worker: {kind, job}` (see `working`); the caller's own keys are kept as given."""
    boundary = uuid.uuid4().hex
    fields = {"slug": slug, "name": name, "description": description, "metadata": json.dumps(stamped(metadata))}
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields.items() if v != ""]
    head = f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{file.name}"\r\nContent-Type: {mime}\r\n\r\n'.encode()
    body = b"".join(parts) + head + file.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    return json.loads(request(creds, "/api/platform/assets/upload", method="POST", data=body, content_type=f"multipart/form-data; boundary={boundary}"))


def update_entry(creds: Creds, slug: str, changes: dict) -> dict:
    """Merge `changes` into the record's fields (a PATCH replaces dataJson, so the rest is read and sent back with it)."""
    row = next(iter(query(creds, {"entries": slug}, limit=1)), None)  # Marvin looks the one record up; no listing the whole workspace
    if row is None:
        raise MarvinError(f"no record “{slug}” to write to")
    data = {**(row.get("dataJson") or row.get("data") or {}), **changes}
    return json.loads(request(creds, f"/api/platform/entries/{row['id']}", method="PATCH", data=json.dumps({"dataJson": data}).encode(), content_type="application/json"))


def upsert_entry(creds: Creds, entry_type: str, slug: str, title: str, data: dict) -> dict:
    """Create the record if it is not there, else merge `data` into it: for a record the worker itself keeps (its heartbeat)."""
    if query(creds, {"entries": slug}, limit=1):
        return update_entry(creds, slug, data)
    types = {t["slug"]: t["id"] for t in json.loads(request(creds, "/api/platform/entry-types"))}
    if entry_type not in types:
        raise MarvinError(f"this workspace has no “{entry_type}” entry type")
    return create_entry(creds, types[entry_type], slug, title, data)


def create_entry(creds: Creds, entry_type_id: str, slug: str, title: str, data: dict, *, status: str = "draft") -> dict:
    """Make a record under the slug given, safely tried again. Marvin gives a create whose slug is taken the next free one (`slug-2`) rather
    than refusing it, so a create sent twice would make a second record. After a lost answer the slug is looked up instead: the record
    there is the answer, and the create is sent again only if there is none. Should the first send land after that look (a race), the
    second record it leaves is moved to the Trash and the first is the answer."""
    body = json.dumps({"entryTypeId": entry_type_id, "title": title, "slug": slug, "status": status, "dataJson": data}).encode()
    looked = False

    def found() -> bytes | None:
        nonlocal looked
        looked = True
        row = next(iter(query(creds, {"entries": slug}, limit=1)), None)
        return json.dumps(row).encode() if row else None

    made = json.loads(request(creds, "/api/platform/entries", method="POST", data=body, content_type="application/json", done=found))
    if looked and made.get("slug") != slug and (first := found()):
        request(creds, f"/api/platform/entries/{made['id']}", method="DELETE")
        return json.loads(first)
    return made


def claim(creds: Creds, slug: str, who: str, at: str, *, settle: float = 0.4) -> bool:
    """Take a queued job for `who`: write it as claimed, wait a moment for any rival's write to land, then read it back and keep it only
    if it is still ours. Two workers reaching for the same job end with exactly one winner, except in a narrow window at heavy load; a
    broker would remove even that (see BACKLOG.md). False if it is not queued any more or another worker won."""
    import time

    row = next(iter(query(creds, {"entries": slug}, limit=1)), None)
    data = (row or {}).get("dataJson") or {}
    if row is None or data.get("status") != "queued":
        return False
    update_entry(creds, slug, {"status": "claimed", "claimed_by": who, "claimed_at": at, "attempts": int(data.get("attempts") or 0) + 1, "log": ""})
    if settle:
        time.sleep(settle)
    again = next(iter(query(creds, {"entries": slug}, limit=1)), None)
    return ((again or {}).get("dataJson") or {}).get("claimed_by") == who
