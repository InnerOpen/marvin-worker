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
Jobs run one at a time, in the order asked. A job that fails says why on its record.

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
