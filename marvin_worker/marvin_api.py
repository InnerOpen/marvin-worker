"""The few calls the worker makes to Marvin itself: read records, download an asset, upload an asset, merge fields into a record.
Standard library only. Python's default user agent is refused by Marvin's edge, so every request names this tool. Anything
beyond these calls a job does with the Marvin CLI (`ctx.marvin(...)`) rather than another client here."""

import json
import urllib.error
import urllib.request
import uuid
from pathlib import Path

USER_AGENT = "marvin-worker/0.1"
PAGE = 500


class MarvinError(RuntimeError):
    pass


Creds = tuple[str, str]  # Marvin's address and a token pinned to the workspace


def request(creds: Creds, path: str, *, method: str = "GET", data: bytes | None = None, content_type: str | None = None) -> bytes:
    base, token = creds
    headers = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT, "Accept": "*/*"}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise MarvinError(f"Marvin answered {e.code} for {path}: {e.read()[:200].decode('utf-8', 'replace')}") from e
    except urllib.error.URLError as e:
        raise MarvinError(f"can't reach Marvin ({e.reason})") from e


def entries(creds: Creds) -> list[dict]:
    body = json.loads(request(creds, f"/api/platform/entries?limit={PAGE}"))
    return body if isinstance(body, list) else body.get("items", [])


def query(creds: Creds, spec: dict, *, limit: int = 100) -> list[dict]:
    """Records matching a query spec (`{"entry_type": "job", "data": {"status": "queued"}}`): Marvin does the filtering."""
    body = json.dumps({"spec": spec, "limit": limit}).encode()
    return json.loads(request(creds, "/api/platform/entries/query", method="POST", data=body, content_type="application/json")).get("entries", [])


def asset(creds: Creds, asset_id: str) -> dict:
    return json.loads(request(creds, f"/api/platform/assets/{asset_id}"))


def download_asset(creds: Creds, asset_id: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(request(creds, f"/api/platform/assets/{asset_id}/file"))


def upload_asset(creds: Creds, file: Path, *, slug: str, name: str, mime: str, description: str = "", metadata: dict | None = None) -> dict:
    """Upload a file as a new asset (multipart, like the Assets page). The file keeps its name, because Marvin reads a file's type
    from its extension."""
    boundary = uuid.uuid4().hex
    fields = {"slug": slug, "name": name, "description": description, "metadata": json.dumps(metadata or {})}
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
