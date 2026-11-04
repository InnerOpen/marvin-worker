# Deploying workers: Docker, Podman, Kubernetes

A worker is a small Python program (`python -m marvin_worker`) that talks to Marvin's API. Nothing reaches in: it only calls out, so it can sit
behind any firewall. Pick a shape, then a platform.

| shape | what runs | use it when |
|---|---|---|
| **runner** (`work`) | one standing container or process per capability; it registers what it can do and takes matching jobs | you want a worker always there (an ffmpeg runner on a spare machine) |
| **dispatcher** (`dispatch`) | one small process that starts a fresh container (Docker/Podman) or Kubernetes Job per queued job, then it exits | you want isolation and no idle workers |
| **standing HTTP worker** (`serve`) | a container with a public URL that Marvin's `request` step calls | the worker is on the public internet and you want Marvin to push jobs |

You always need three things first.

1. **A token pinned to one workspace**, so the worker can reach nothing else: `marvin tokens create --pin <workspace>`. Keep it in a secret store, never
   in an image or a Git repo.
2. **The three record types** in the workspace: `job`, `job-kind`, `worker` (field lists are in the README). A "Workers" package will install them in one step.
3. **An image.** Build the base once, then a purpose image from it (see the README, "One base image, many purposes"):

   ```sh
   docker build -f docker/Dockerfile -t marvin-worker-base .       # or: podman build ...
   docker build -t my-worker ./my-pack                              # FROM marvin-worker-base, sets WORKER_JOB_MODULES
   ```

## Docker

**A runner**, always on:

```sh
docker run -d --name my-runner --restart unless-stopped \
  -e MARVIN_API_URL=https://api.example.com -e MARVIN_USER_TOKEN -e WORKER_NAME=my-runner \
  my-worker python -m marvin_worker work
```

(`-e MARVIN_USER_TOKEN` with no value copies it from your shell, so it is not in a file or a process listing.) Add `-e WORKER_POLL_SECONDS=3` to
look for jobs more often. It shows up on Settings -> Workers within a minute, with the kinds it offers.

**A dispatcher** that starts a container per job: run it on the Docker host itself (the simplest and safest), with the base package installed:

```sh
pip install .                                   # from a clone of this repo
export MARVIN_API_URL=https://api.example.com MARVIN_USER_TOKEN=...
python -m marvin_worker dispatch                # one dispatcher per workspace; keep it running (systemd, tmux)
```

Then add a `job-kind` record per kind: `mode: docker`, `image: my-worker`, a time allowed and optional `resources` (`{"cpu": 1, "memory": "1g"}`).
Containers are kept after they exit so the dispatcher can read them: it appends what each one printed to the job's log (Settings -> Workers shows
it), fails a job whose container died before reporting, and removes the container. Running the dispatcher *inside* a container means mounting the
Docker socket, which is root on the host: avoid it unless you accept that.

## Podman

Podman takes the same arguments as Docker, so every `docker` command above works with `podman` (rootless is fine). Two differences:

- Tell the dispatcher to use it: `export WORKER_CONTAINER_CLI=podman` before `python -m marvin_worker dispatch`.
- Podman keeps its own image store. Build with `podman build`, or copy a Docker image across: `docker save my-worker | podman load`. Use the full name in the
  `job-kind` record if Podman reports it as `docker.io/library/my-worker`.

A runner under Podman: `podman run -d --name my-runner --restart unless-stopped -e MARVIN_API_URL=... -e MARVIN_USER_TOKEN my-worker python -m marvin_worker work`.

Tested on Podman 4.6 (rootless): a started container, its exit code and output read back, the output appended to the job's log, and the container removed.

## Kubernetes

**A dispatcher that makes a Job per run.** It needs a ServiceAccount that may create Jobs in one namespace, and a Secret holding the token. The
Job it creates gets Marvin's address and the token from that Secret (never written into the Job), one attempt (`backoffLimit: 0`), a deadline from
the kind's time allowed, and clean-up ten minutes after it finishes.

```yaml
apiVersion: v1
kind: Secret
metadata: {name: marvin-worker}
stringData: {MARVIN_USER_TOKEN: "<the pinned token>"}
---
apiVersion: v1
kind: ServiceAccount
metadata: {name: marvin-dispatcher}
---
kind: Role
apiVersion: rbac.authorization.k8s.io/v1
metadata: {name: marvin-job-launcher}
rules:
  - {apiGroups: [batch], resources: [jobs], verbs: [create, get, list]}
---
kind: RoleBinding
apiVersion: rbac.authorization.k8s.io/v1
metadata: {name: marvin-job-launcher}
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: Role, name: marvin-job-launcher}
subjects: [{kind: ServiceAccount, name: marvin-dispatcher}]
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: marvin-dispatcher}
spec:
  replicas: 1                                   # one dispatcher
  selector: {matchLabels: {app: marvin-dispatcher}}
  template:
    metadata: {labels: {app: marvin-dispatcher}}
    spec:
      serviceAccountName: marvin-dispatcher
      containers:
        - name: dispatcher
          image: marvin-worker-base              # the dispatcher needs no tools, only the core
          command: ["python", "-m", "marvin_worker", "dispatch"]   # `command`, not `args`: the image sets CMD, so `args` alone would replace it
          env:
            - {name: MARVIN_API_URL, value: "https://api.example.com"}
            - name: MARVIN_USER_TOKEN
              valueFrom: {secretKeyRef: {name: marvin-worker, key: MARVIN_USER_TOKEN}}
```

Then a `job-kind` per kind with `mode: kubernetes`, the purpose image, a time allowed and `resources`. `DISPATCH_NAMESPACE` (default: the pod's own) and
`DISPATCH_SECRET` (default `marvin-worker`) change where Jobs go and which Secret they read.

**A runner**: the same container as the Docker runner, as a Deployment (no RBAC needed):

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: {name: my-runner}
spec:
  replicas: 1
  selector: {matchLabels: {app: my-runner}}
  template:
    metadata: {labels: {app: my-runner}}
    spec:
      containers:
        - name: runner
          image: my-worker
          command: ["python", "-m", "marvin_worker", "work"]
          env:
            - {name: MARVIN_API_URL, value: "https://api.example.com"}
            - {name: WORKER_NAME, value: "my-runner"}
            - name: MARVIN_USER_TOKEN
              valueFrom: {secretKeyRef: {name: marvin-worker, key: MARVIN_USER_TOKEN}}
```

The images run as a non-root user and write nothing but `/tmp`, so they are built to work where the user id is assigned (OpenShift); not yet tried there.

Status of these manifests: written from the code, **not yet run on a cluster**. The Job command was wrong until this guide was written (it set only `args`, which
replaced the image's `CMD`); that is fixed and unit-tested. Run one real job through it before relying on it.

## Checking it works

Settings -> Workers shows each runner and dispatcher with its last-seen time, the kinds on offer with what each takes, gives and needs, and the queue with each
job's whole log. A grey dot means "not running", not an error. If a job fails at once with a short message such as "story is needed", its arguments did not match the kind's contract.
