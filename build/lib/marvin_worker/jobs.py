"""What a kind of job is. A kind is a function `(job, ctx) -> dict`: it does the work, using `ctx.work` for scratch files and
`ctx.creds` / `ctx.marvin(...)` to read from or write to Marvin, and returns the fields to put on the job's record (its
`status` and a `log` are set around it by the worker). Register one with `@job_kind("name")`.

The core ships no kinds. An image that builds on it names the modules that define its own in WORKER_JOB_MODULES, so one base image
serves any purpose: media rendering, documents, scraping, anything heavy Marvin should hand off."""

import importlib
import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .marvin_api import Creds

HANDLERS: dict[str, Callable] = {}


class JobError(ValueError):
    """A job the worker can't take, or one that can't be done: say what is wrong."""


def job_kind(name: str) -> Callable:
    def register(fn: Callable) -> Callable:
        HANDLERS[name] = fn
        return fn

    return register


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

    def marvin(self, *args: str) -> object:
        """Run the Marvin CLI as the worker's pinned token (`ctx.marvin("platform", "entries", "list")`) and return its JSON."""
        base, token = self.creds
        env = {**os.environ, "MARVIN_API_URL": base, "MARVIN_USER_TOKEN": token}
        done = subprocess.run(["marvin", *args, "--json"], env=env, capture_output=True, text=True, timeout=300)
        if done.returncode != 0:
            raise JobError(f"marvin {' '.join(args)} failed: {(done.stderr or done.stdout).strip()[:300]}")
        return json.loads(done.stdout) if done.stdout.strip() else None
