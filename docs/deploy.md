# Running workers: a process, Docker, Podman, Kubernetes or OpenShift

A worker is a small Python program (`python -m marvin_worker`) that talks to Marvin's API. Nothing reaches in (except the standing HTTP shape
below): it calls out, so it can sit behind any firewall. It authenticates with **a token and nothing else**: `MARVIN_USER_TOKEN` and
`MARVIN_API_URL` in its environment, wherever it runs. Settings -> Workers in Marvin shows these commands with your address filled in.

## The three shapes

| shape | command | lifetime | how it gets jobs | needs something always on |
|---|---|---|---|---|
| **runner** | `work` | stays up | it asks Marvin for jobs it can do (outbound only, no public address) | the runner |
| **dispatcher + one-shot containers** | `dispatch` starts `run` | the container exists for one job, then exits and is removed | the dispatcher watches the queue and starts a container (Docker/Podman) or Kubernetes Job per job | **the dispatcher** |
| **standing HTTP worker** | `serve` | stays up | Marvin POSTs the job to its URL (a `job-kind` of mode `http`) | the worker, and a public URL |

"Ephemeral" jobs are the second row. The container registers nothing and starts nothing: a small dispatcher has to be running to launch it. That
dispatcher is the grey or green "dispatcher" on Settings -> Workers; the one-shot containers never appear there. A runner is the simplest to keep up
on a machine that has the tools (ffmpeg), and a dispatcher is what gives each job a clean, isolated container.

Where each can run:

| | runner | dispatcher | one-shot job container | standing HTTP |
|---|---|---|---|---|
| a process on a machine | yes | yes | (the dispatcher's `local` mode) | yes |
| Docker | yes | on the Docker host (not inside a container) | yes | yes |
| Podman | yes | yes (`WORKER_CONTAINER_CLI=podman`) | yes | yes |
| Kubernetes / OpenShift | a Deployment | a Deployment + Role | a Job per run | a Deployment + Service |

## Before any of them

1. **Add the record types** workers use to the workspace (core ships them; it never overwrites what you have):
   `marvin platform workers setup`, or the "Set up workers" button on Settings -> Workers. If you already have a record type named `job` or
   `worker` for something else, setup says it does not fit and leaves it alone: rename yours first.
2. **Make a token pinned to one workspace**, so the worker can reach nothing else:
   `marvin user tokens create --name worker --pin <workspace-slug>`. Keep it in a secret store, never in an image or a Git repo.
3. **An image**, for a container or pod. Build the base once, then a purpose image from it (see the README, "One base image, many purposes"):

   ```sh
   docker build -f docker/Dockerfile -t marvin-worker-base .       # or: podman build ...
   docker build -t my-worker ./my-pack                              # FROM marvin-worker-base, sets WORKER_JOB_MODULES
   ```

   Rebuild both after the worker code changes: an image built before the runner existed treats `work` as the old standing-server mode.

## A process on a machine

```sh
git clone https://github.com/InnerOpen/marvin-worker && cd marvin-worker && pip install .
export MARVIN_API_URL=https://api.example.com MARVIN_USER_TOKEN=...   # the token, from your secret store
export WORKER_NAME=my-runner WORKER_JOB_MODULES=my_pack.jobs          # the modules that define your kinds of job
python -m marvin_worker work          # a runner;  `dispatch` for a dispatcher
```

Keep it running with systemd (a user unit with `Restart=always` and an `EnvironmentFile=` holding the variables, mode 600) or tmux. It shows up on
Settings -> Workers within a minute. Set `WORKER_POLL_SECONDS=3` to look for jobs more often.

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

## Kubernetes and OpenShift

OpenShift takes the same manifests: use `oc` where this says `kubectl` (`oc create secret generic marvin-worker --from-literal=MARVIN_USER_TOKEN=...`,
`oc apply -f`). It assigns an arbitrary non-root user id, which the images are built for (checked: see the status note at the end).

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

The images run as a non-root user and write nothing but `/tmp`, so they are built to work where the user id is assigned (OpenShift). Checked on OpenShift 4 under `restricted-v2`: the pod ran as user 1000820000, group 0, with `/tmp` writable and nothing else.

Status of these manifests: **run on OpenShift 4** (a dispatcher Deployment with its Role and a Secret, in a fresh project, the images from the cluster's own registry). One
`kubernetes`-mode job went queued, claimed, done in about 15 seconds: the dispatcher made a Job (`command` correct, one attempt, a deadline from the kind's time allowed, clean-up
after ten minutes), the pod ran under `restricted-v2` as an arbitrary user id, and the result and the dispatching pod's name came back on the job record. The test also found that a
dispatcher in a pod, which has no Docker, failed before claiming anything whenever the workspace had a `docker`-mode kind; a dispatcher now takes only the modes it can start (local,
http, docker with the container CLI, kubernetes with the in-cluster token). Not covered: a runner as a Deployment, the standing HTTP worker, other Kubernetes distributions, and the
container output capture, which is for Docker and Podman only (a Job's pod logs are not copied onto the job).

## Checking it works

Settings -> Workers shows each runner and dispatcher with its last-seen time, the kinds on offer with what each takes, gives and needs, and the queue with each
job's whole log. A grey dot means "not running", not an error. If a job fails at once with a short message such as "story is needed", its arguments did not match the kind's contract.
