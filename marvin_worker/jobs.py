"""What a kind of job is. A kind is a function `(job, ctx) -> dict`: it does the work, using `ctx.work` for scratch files and
`ctx.creds` / `ctx.marvin(...)` to read from or write to Marvin, and returns the fields to put on the job's record (its
`status` and a `log` are set around it by the worker). Register one with `@job_kind("name")`.

The core ships no kinds. An image that builds on it names the modules that define its own in WORKER_JOB_MODULES, so one base image
serves any purpose: media rendering, documents, scraping, anything heavy Marvin should hand off."""

import importlib
import json
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from . import log, marvin_api
from .marvin_api import Creds

HANDLERS: dict[str, Callable] = {}
META: dict[str, dict] = {}  # each kind's contract: what it takes, what it gives, what it needs (what a worker tells Marvin it can do)


class JobError(ValueError):
    """A job the worker can't take, or one that can't be done: say what is wrong."""


INPUT_TYPES = ("string", "number", "integer", "boolean", "object", "array", "asset")


def _input(spec) -> dict:
    """One declared input: a type name ("string", or "string?" for optional), or {type, required, description, enum, accepts}."""
    if isinstance(spec, str):
        spec = {"type": spec.rstrip("?"), "required": not spec.endswith("?")}
    out = {"type": spec.get("type", "string"), "required": bool(spec.get("required", True))}
    for key in ("description", "enum", "accepts"):
        if spec.get(key):
            out[key] = spec[key]
    if out["type"] not in INPUT_TYPES:
        raise ValueError(f"input type must be one of {', '.join(INPUT_TYPES)}, not {out['type']!r}")
    return out


def job_kind(name: str, *, description: str = "", inputs: dict | None = None, accepts: tuple | list = (), produces: tuple | list = (), needs: tuple | list = ()) -> Callable:
    """Register a kind of job, and declare its contract so Marvin knows what it can be sent:
    `inputs` the arguments it takes (`{"story": "string", "size": "integer?", "source": {"type": "asset", "accepts": ["audio/*"]}}`),
    `accepts` the media types of any file it is handed (for an asset input without its own), `produces` what it gives back,
    `needs` what the machine must have (`ffmpeg`, `gpu`). The contract is advertised in the worker's heartbeat and checked before a job runs."""

    def register(fn: Callable) -> Callable:
        HANDLERS[name] = fn
        META[name] = {
            "kind": name,
            "description": description or ((fn.__doc__ or "").strip().splitlines() or [""])[0],
            "inputs": {k: _input(v) for k, v in (inputs or {}).items()},
            "accepts": list(accepts),
            "produces": list(produces),
            "needs": list(needs),
        }
        return fn

    return register


def describe_kinds(only: list[str] | None = None) -> list[dict]:
    """The contracts of the kinds this worker has (all, or just `only`), in name order: what its heartbeat advertises."""
    return [META[k] for k in sorted(HANDLERS) if k in META and (only is None or k in only)]


def _mime_ok(mime: str, patterns: list[str]) -> bool:
    return not patterns or any(fnmatch(mime, p) for p in patterns)


def check_args(kind: str, args: dict, creds: Creds | None = None) -> None:
    """Refuse a job whose arguments do not fit its kind's contract, saying every problem in plain words, before anything runs: a missing
    argument, one of the wrong type, and (with `creds`) a file whose media type the kind does not take. Extra arguments are the kind's own business."""
    contract = META.get(kind)
    if not contract:
        return
    problems: list[str] = []
    for key, spec in contract["inputs"].items():
        if key not in args or args[key] in (None, ""):
            if spec["required"]:
                problems.append(f"“{key}” is needed" + (f" ({spec['description']})" if spec.get("description") else ""))
            continue
        value, kind_of = args[key], spec["type"]
        ok = {
            "string": isinstance(value, str), "asset": isinstance(value, str), "boolean": isinstance(value, bool), "object": isinstance(value, dict), "array": isinstance(value, list),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool), "integer": isinstance(value, int) and not isinstance(value, bool),
        }[kind_of]  # fmt: skip
        if not ok:
            problems.append(f"“{key}” should be a {kind_of}")
        elif spec.get("enum") and value not in spec["enum"]:
            problems.append(f"“{key}” should be one of {', '.join(map(str, spec['enum']))}")
        elif kind_of == "asset" and creds is not None:
            patterns = spec.get("accepts") or contract["accepts"]
            if patterns:
                try:
                    mime = str(marvin_api.asset(creds, value).get("mimeType") or "")
                except marvin_api.MarvinError as e:
                    problems.append(f"“{key}”: can't read that file ({e})")
                    continue
                if not _mime_ok(mime, patterns):
                    problems.append(f"“{key}” is {mime or 'of an unknown type'}, but {kind} takes {', '.join(patterns)}")
    if problems:
        raise JobError(f"{kind}: " + "; ".join(problems) + ".")


def load_kinds(modules: str | None = None) -> list[str]:
    """Import the modules named (comma-separated, default WORKER_JOB_MODULES) so their `@job_kind` functions register. Returns the kinds."""
    for name in (modules if modules is not None else os.environ.get("WORKER_JOB_MODULES", "")).split(","):
        if name.strip():
            importlib.import_module(name.strip())
    return sorted(HANDLERS)


@dataclass
class Context:
    creds: Creds  # Marvin's address and the workspace-pinned token
    work: Path  # a scratch folder, removed when the job ends
    name: str = ""  # the job record's slug, for the lines it logs
    notes: list[str] = field(default_factory=list)  # what the job said it was doing, kept on the record's log
    began: float = field(default_factory=time.monotonic)

    def note(self, text: str) -> None:
        """Say what the job is doing ("pulled 51 files"). It goes on the worker's own log at once, with the time since the job began, and
        onto the job record's `log` when the job ends, so a failure shows how far it got."""
        line = f"[{time.monotonic() - self.began:5.1f}s] {text}"
        self.notes.append(line)
        log.say(f"{self.name or 'job'}: {text}")

    def marvin(self, *args: str) -> object:
        """Run the Marvin CLI as the worker's pinned token (`ctx.marvin("platform", "entries", "list")`) and return its JSON."""
        base, token = self.creds
        env = {**os.environ, "MARVIN_API_URL": base, "MARVIN_USER_TOKEN": token}
        done = subprocess.run(["marvin", *args, "--json"], env=env, capture_output=True, text=True, timeout=300)
        if done.returncode != 0:
            raise JobError(f"marvin {' '.join(args)} failed: {(done.stderr or done.stdout).strip()[:300]}")
        return json.loads(done.stdout) if done.stdout.strip() else None
