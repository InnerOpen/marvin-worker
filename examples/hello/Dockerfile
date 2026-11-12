# A purpose image: the base (the worker core, Python and the Marvin CLI; it ships no kinds) plus your pack and the name of its module.
#   docker build -f docker/Dockerfile -t marvin-worker-base .                       # once, from the repo root
#   docker build -f examples/hello/Dockerfile -t hello-worker examples/hello       # this image
FROM marvin-worker-base
# Anything your kinds need goes here, e.g.:  RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY hello_pack ./hello_pack
ENV WORKER_JOB_MODULES=hello_pack.jobs
# The base's own command starts a standing worker; a runner is started with:  docker run ... hello-worker python -m marvin_worker work
