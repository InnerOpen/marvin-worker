# marvin-worker

A generic job worker for Marvin. Marvin keeps the state, the approvals and the records; this takes the **heavy jobs** (anything that
needs a binary tool, a long run, or more memory than the API pod should spend) over HTTP, does them, and writes the outcome back
onto a record in Marvin. It keeps nothing of its own.

```
POST /jobs   Authorization: Bearer $WORKER_TOKEN
             {"kind": "audio-cut", "job": "<record slug>", "args": {"story": "the-last-onion"}}      → 202 at once
GET  /health
```

`kind` is a function registered with `@job_kind("name")` (`(job, ctx) -> fields for the record`); `job` is the slug of the Marvin
record that gets `status` (`rendering` → `done` / `failed`), a `log`, and whatever the job returns; `args` is the job's own input.
A standing worker runs jobs one at a time, in the order asked. A job that fails says why on its record.

## The queue: jobs are records in Marvin

A task is a `job` record in Marvin (`kind`, `args`, `status`: queued → claimed → running → done / failed, plus `result` and `log`). A
workflow creates one with an ordinary entry step. A **dispatcher** (`python -m marvin_worker dispatch`) watches for queued jobs, claims
each, and starts a worker for it in the mode its **`job-kind`** record says (image, mode, time allowed, resources). The worker writes the
outcome back onto the record, and a workflow triggered by "job done" can carry on. The dispatcher only calls *out* to Marvin, so
nothing has to reach in and Marvin never holds keys to the cluster. A started worker is handed Marvin's address and the token.

| mode | starts | needs |
|---|---|---|
| `kubernetes` | a Job per run (one attempt, a deadline, the token from a Secret) | a service account that may create Jobs in one namespace (below) |
| `docker` | `docker run --rm` per run | the Docker CLI and an engine: run the dispatcher on the host, or mount the socket (that is root on the host) |
| `http` | POST to a standing worker's `/jobs` (`python -m marvin_worker`) | the worker's URL, and `DISPATCH_WORKER_TOKEN` |
| `local` | a process on the dispatcher's own machine | the kind's module installed there |

A job past its kind's time allowed, with no outcome, is failed so a dead worker doesn't hold a place. In docker mode a worker container that dies before reporting fails its job at once, with its last output. Run one dispatcher. It also keeps a `worker` record (its machine, the modes it starts, `last_seen` rewritten about once a minute) that Marvin's **Settings → Workers** page reads to say whether anything is watching the queue.

The record types a workspace needs for this: `job` (kind, args, status, result, log, attempts, claimed_at, claimed_by), `job-kind` (kind, mode, image, url, timeout_seconds, resources, notes) and `worker` (role, host, modes, kinds, status, current_job, tags, last_seen, started_at, jobs_started, jobs_done).
Environment: `MARVIN_API_URL`, `MARVIN_USER_TOKEN`, `DISPATCH_POLL_SECONDS` (10), `DISPATCH_MAX_PARALLEL` (2), and for Kubernetes
`DISPATCH_NAMESPACE` (default: its own) and `DISPATCH_SECRET` (a Secret holding `MARVIN_USER_TOKEN`).

```yaml
# the only cluster access the dispatcher needs
kind: Role
metadata: {name: marvin-job-launcher}
rules:
  - {apiGroups: [batch], resources: [jobs], verbs: [create, get, list]}
```

`python -m marvin_worker run --kind K --job <record> --args '{...}'` does one job and exits (what a Job or `docker run` executes).

## Runners: workers that register what they can do

`python -m marvin_worker work` is a **runner**: a standing worker you can add anywhere (a container on any Docker host, a machine under a desk).
Give it `MARVIN_API_URL` and a token pinned to the workspace and it **announces itself** (its machine, whether it is idle or busy, and every
kind it has with that kind's contract), then **claims queued jobs of those kinds and runs them**. No dispatcher is needed and nothing has to
reach in. Add another container for another capability: an ffmpeg runner, a documents runner. What it offers comes from the modules its image
loads (`WORKER_JOB_MODULES`), or a subset (`WORKER_KINDS`).

A kind declares its **contract** next to the function, and the worker advertises it in its heartbeat (the `kinds` field of its `worker` record):

```python
@job_kind("audio-cut", description="Stitch a story's recorded lines",
          inputs={"story": "string", "source": {"type": "asset", "accepts": ["audio/*"], "required": False}, "size": "integer?"},
          produces=["audio/mpeg"], needs=["ffmpeg"])
```

`inputs` are the arguments (`"string"`, `"integer?"` for optional, or `{type, required, description, enum, accepts}`; types: string, number,
integer, boolean, object, array, asset), `accepts` the media types of files it takes, `produces` what it gives back, `needs` what the machine must
have. **A job is checked against its kind's contract before anything runs**: missing or wrongly typed arguments, and a file of the wrong media
type (a PDF sent to an audio cutter), fail the job at once with a plain message on its record.

Two workers reaching for one job: the claim writes `claimed_by`, waits a moment, reads it back and keeps it only if it is still theirs.

### Where it announces and finds jobs: a backend

Announce (heartbeat and capabilities), intake (claim the next job) and report (status, log, results) sit behind one seam (`backend.py`).
The default keeps all three in Marvin's records (Marvin stays the source of truth; Settings -> Workers reads them). `WORKER_SINKS` (comma-separated
URLs, `WORKER_SINK_AUTH` for an Authorization header) ALSO posts every announcement and outcome as JSON to those addresses. A queue (RabbitMQ,
Redis) is another implementation of the same three calls, named by `WORKER_BACKEND=package.module:factory` so its library lives in the image that
wants it.

## One base image, many purposes

`docker/Dockerfile` is the **base**: this core, Python and the Marvin CLI. It ships no kinds of job. A purpose image builds `FROM` it,
adds its tools, and names the modules that define its kinds:

```dockerfile
FROM marvin-worker-base
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY mypack ./mypack
ENV WORKER_JOB_MODULES=mypack.jobs
```

The Storyworld media worker (ffmpeg + `audio-cut`) is the first one, in the Storyworld repo. No container platform? It is plain Python:
`WORKER_JOB_MODULES=mypack.jobs python -m marvin_worker` runs it as a process, on any machine Marvin can reach.

## Configuration

On the worker: `MARVIN_API_URL`, `MARVIN_USER_TOKEN` (a token pinned to one workspace, so it can reach nothing else; mint it with
`marvin tokens create --pin <workspace>`; the `marvin` CLI reads the same two), `WORKER_TOKEN`, `WORKER_JOB_MODULES`, `PORT` (8080).

In Marvin, the worker's address and secret are an ordinary workspace Variable and Secret, so a workflow's `request` step reads them and
nothing is written into a workflow (its `request` step reaches public hosts only, so the worker needs a public URL):

```json
{"kind": "request", "method": "POST", "url": "{{WORKER_URL}}/jobs", "secret_ref": "WORKER_TOKEN",
 "body": {"kind": "audio-cut", "job": "${entry.slug}", "args": {"story": "the-last-onion"}}}
```

## Writing a kind of job

```python
from marvin_worker.jobs import job_kind

@job_kind("thumbnails")
def thumbnails(job, ctx):
    entries = ctx.marvin("platform", "entries", "list")        # any CLI call, as the pinned token
    ...                                                         # ctx.work is a scratch folder, removed afterwards
    return {"log": "made 12 thumbnails"}                        # fields merged onto the job's record
```

`marvin_worker.marvin_api` has the few calls the worker makes itself (read records, download and upload an asset, merge fields into a
record); anything else goes through the CLI.

Tests: `uvx --from pytest pytest`.
