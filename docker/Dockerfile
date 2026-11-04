# The base image: a generic job worker for Marvin. It holds no tools of its own beyond the Marvin CLI; a purpose image builds
# FROM this one, adds what its jobs need (ffmpeg, a PDF toolkit, a browser...) and names the modules that define its kinds of
# job in WORKER_JOB_MODULES. It keeps nothing of its own (Marvin's records are the state), so it can be restarted or replaced at any time.
#
#   FROM marvin-worker-base
#   RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
#   COPY mypack ./mypack
#   ENV WORKER_JOB_MODULES=mypack.jobs
FROM python:3.12-slim

# node + the Marvin CLI, so a job can talk to Marvin with a supported client: `ctx.marvin("platform", ...)`.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm \
    && npm install -g @inneropen/marvin-cli@3.3.0 \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY marvin_worker ./marvin_worker

# A purpose image runs as a non-root user too (OpenShift assigns an arbitrary one): nothing here is written except /tmp.
ENV PYTHONUNBUFFERED=1 PORT=8080 PYTHONPATH=/app
EXPOSE 8080
USER 1001

# Needs MARVIN_API_URL and MARVIN_USER_TOKEN (a workspace-pinned token: the worker and the `marvin` CLI both read these) and
# WORKER_TOKEN (the secret Marvin sends), plus WORKER_JOB_MODULES from the purpose image.
CMD ["python", "-m", "marvin_worker"]
