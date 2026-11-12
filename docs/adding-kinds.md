# Adding features to a worker: kinds of job

A worker does one thing: it takes a **job** from Marvin, runs the function registered for the job's **kind**, and writes the outcome back onto the job's record. A **kind** is a Python function. So *adding a feature to a worker means adding a kind*.

**The base image ships no kinds, on purpose.** A worker started from it (`marvin-worker-base`, or a bare `pip install .`) starts, finds nothing it can do, and stops with `this worker has no kinds of job`. That is the expected result of following only the deploy guide: it gets a worker running, and this page gives it something to do. Screenplay Studio's media worker is the same base plus its own kinds ([how it is built](https://github.com/iWobble/ScreenplayStudio/tree/main/docker)).

You need:

- the record types set up in your workspace (`marvin platform workers setup`) and a token pinned to one workspace (`marvin user tokens create --name worker --pin <workspace>`): see [deploy.md](deploy.md), "Before any of them";
- Python 3.11+ for the process route, or Docker/Podman for the container route.

The steps below build the smallest complete example, `hello`, which is in [`examples/hello`](../examples/hello). Copy it and change it.

## 1. Write a kind

A kind lives in a module of your own (a *pack*): a folder with an `__init__.py` and a `jobs.py`.

```
hello_pack/
  __init__.py        # empty: it makes the folder a package
  jobs.py
```

```python
from marvin_worker.jobs import JobError, job_kind


@job_kind(
    "hello",                                                    # the kind: what a job record's `kind` field says
    description="Greet someone",                                # shown on Settings -> Workers
    inputs={
        "name": {"type": "string", "description": "who to greet"},
        "shout": {"type": "boolean", "required": False},        # optional: say so, or a job without it is refused
    },
    produces=[],                                                # media types it gives back, e.g. ["audio/mpeg"]
    needs=[],                                                   # programs the machine must have, e.g. ["ffmpeg"]
)
def hello(job, ctx):
    name = job["args"]["name"].strip()
    if not name:
        raise JobError("name is empty: say who to greet")       # fails the job with this message on its record
    ctx.note(f"greeting {name}")                                # a line in the worker's log and the job's log
    return {"log": f"Hello, {name}!", "result": {"letters": len(name)}}   # these fields are merged onto the job's record
```

What each part does:

| part | what it is |
|---|---|
| `@job_kind("hello", ...)` | registers the function under that name **and declares its contract**. Importing the module is what registers it. |
| `inputs` | the arguments (`job["args"]`). A type name (`"string"`, or `"string?"` for optional) or `{type, required, description, enum, accepts}`. Types: `string`, `number`, `integer`, `boolean`, `object`, `array`, `asset`. **An input is required unless it says `required: False` (or ends in `?`)**, and its type is checked: a number is not a string. |
| `produces`, `needs` | what it gives back, and what the machine must have. Both are advertised, and `needs` is checked by the `kinds` command below. |
| `def hello(job, ctx)` | `job` is `{"job": <record slug>, "args": {...}, ...}`. `ctx.work` is a scratch folder (removed afterwards), `ctx.note(text)` logs a step, `ctx.marvin("platform", "entries", "list")` runs the Marvin CLI as the worker's token and returns its JSON. |
| the return value | a dict whose fields are merged onto the job's record (`log`, `result`, or fields of your own type). The worker adds `status`. |
| `raise JobError(...)` | the plain-words way to fail. Any other exception fails the job too, with its message on the record. |

A job is checked against the contract **before anything runs**: a missing or wrongly typed argument, or a file of the wrong media type, fails the job at once with a message such as `hello: “name” is needed.`

## 2. Check that it loads (no Marvin needed)

```sh
cd examples/hello        # or the folder that holds your pack
WORKER_JOB_MODULES=hello_pack.jobs python -m marvin_worker kinds
```

You should see:

```
WORKER_JOB_MODULES = hello_pack.jobs

1 kind offered:

  hello  -  Greet someone
    takes: name (string), shout (boolean, optional)
```

This is the one check to make before anything else. It names a module that cannot be imported, says when a kind's `needs` are not on the machine (`needs: ffmpeg  <- NOT FOUND`), and exits with an error and the way forward when there are no kinds. A worker that offers no kinds cannot run any job.

## 3. Run it

**As a process on a machine** (the simplest):

```sh
pip install /path/to/marvin-worker            # once
export MARVIN_API_URL=https://api.example.com MARVIN_USER_TOKEN=...   # the pinned token, from your secret store
export WORKER_NAME=my-runner WORKER_JOB_MODULES=hello_pack.jobs
cd examples/hello && python -m marvin_worker work
```

It prints `runner my-runner offers hello (1 loaded); polling Marvin every 5 s`, and shows up on **Settings -> Workers** within a minute with `hello` under the kinds it offers.

**In a container.** The purpose image is the base plus your pack and the name of its module ([`examples/hello/Dockerfile`](../examples/hello/Dockerfile)):

```dockerfile
FROM marvin-worker-base
COPY hello_pack ./hello_pack
ENV WORKER_JOB_MODULES=hello_pack.jobs
```

```sh
docker build -f docker/Dockerfile -t marvin-worker-base .                 # once, from the repo root
docker build -f examples/hello/Dockerfile -t hello-worker examples/hello  # your image
docker run --rm hello-worker python -m marvin_worker kinds     # check it first (the image already sets WORKER_JOB_MODULES)
docker run -d --name hello-runner --restart unless-stopped \
  -e MARVIN_API_URL=https://api.example.com -e MARVIN_USER_TOKEN -e WORKER_NAME=hello-runner \
  hello-worker python -m marvin_worker work
```

If the image needs programs (ffmpeg, a PDF tool), install them in the same Dockerfile before the `COPY`; list them in the kind's `needs`.

## 4. Send it a job

A job is a `job` record: `kind`, `args`, and `status: queued`. A workflow makes one with an ordinary entry step:

```json
{"kind": "entry", "op": "create", "entry_type": "job", "title": "Say hello", "status": "draft",
 "data": {"kind": "hello", "args": {"name": "Ada"}, "status": "queued"}}
```

The runner claims it (`claimed` -> `running`), runs `hello`, and the record ends as `done` with `log: Hello, Ada!` and `result: {"letters": 3}`. A workflow triggered by "job done" can carry on from there. Watch it on Settings -> Workers (the queue, each job's whole log) or on the record itself.

## 5. Adding another kind later

1. Add a function with `@job_kind("name", ...)` to a module that is already in `WORKER_JOB_MODULES`, or add a new module to that list (comma-separated: `WORKER_JOB_MODULES=hello_pack.jobs,hello_pack.more`).
2. Run `python -m marvin_worker kinds` and read what it lists.
3. **Restart the runner, or rebuild the image and recreate the container.** Kinds are read once, at start; a running worker never notices a new function or a changed contract. This is the usual reason a new kind "isn't there".
4. Its contract shows on Settings -> Workers within a minute.

`WORKER_KINDS=hello,render` limits a worker to those kinds (one image, several runners with different jobs). A kind named there that is not loaded is simply not offered; if none of them is loaded, the worker says so and stops.

If you use a **dispatcher** (a container per job) instead of a runner, each kind also needs a `job-kind` record in Marvin: `kind`, `mode` (`docker`, `kubernetes`, ...), the `image`, a time allowed. A runner needs no such record. See [deploy.md](deploy.md).

## When it does not work

| what you see | usual cause |
|---|---|
| `error: this worker has no kinds of job` | No module defines one, or `WORKER_JOB_MODULES` is not set where the worker runs (a container does not see your shell's variables: put `ENV` in the Dockerfile or `-e` on `docker run`). The base image has none. Run `python -m marvin_worker kinds` in the same environment. |
| `WORKER_JOB_MODULES names “x”, which could not be imported` | The module is not in the image or on `PYTHONPATH` (`COPY` it, and check the folder has an `__init__.py`), the name is misspelt, or the module imports something that is not installed. |
| `WORKER_KINDS names none of them` | `WORKER_KINDS` lists kinds that are not the loaded ones: check the spelling, or unset it. |
| The runner is on Settings -> Workers but lists no kinds | It was started before the kinds were added: restart it (or rebuild the image). Also give it a minute. |
| It is not on Settings -> Workers at all | The URL or token is wrong; the token is pinned to another workspace; the workspace has no `worker` record type (`marvin platform workers setup`); or the runner is not running (`docker logs`). |
| A job stays `queued` | No worker offers that kind (the `kind` on the job must be spelt exactly as in `@job_kind`); the runner is busy (it takes one job at a time); or you use a dispatcher and there is no `job-kind` record for it. |
| A job fails at once with `“x” is needed` or `“x” should be a ...` | The job's `args` do not fit the contract. Optional inputs need `required: False`; numbers are numbers, not text. |
| A job fails with `marvin ... failed` | A `ctx.marvin(...)` call was refused: the token's role cannot do that in this workspace. |
| A job fails and the log is short | Add `ctx.note(...)` lines as the job goes: they are kept on the record even when it fails, so you can see how far it got. |

## Testing a kind

A kind is a plain function: test it without Marvin or a worker.

```python
from pathlib import Path
from marvin_worker import jobs

def test_hello():
    ctx = jobs.Context(creds=("http://marvin", "token"), work=Path("."), name="hello-1")
    out = jobs.HANDLERS["hello"]({"args": {"name": "Ada"}}, ctx)      # after importing your module
    assert out["result"] == {"letters": 3}
    jobs.check_args("hello", {})                                       # raises JobError: “name” is needed
```

[`tests/test_kinds.py`](../tests/test_kinds.py) does this for the example. Run the suite with `uvx --from pytest pytest`.
