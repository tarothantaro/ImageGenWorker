# syntax=docker/dockerfile:1.7
#
# Image-gen worker — "one image, two environments" (DESIGN.md §1): the same
# image runs in dev (emulators, via deploy/stages/dev) and prod (real GCP, via
# deploy/stages/prod). Only the env injected at startup differs.
#
# NOTE — deliberate deviation from DESIGN.md §7.1: that section pins an
# `nvidia/cuda` base + a :9100/healthz HEALTHCHECK, both written when the model
# ran in-process. The model is now a *ComfyUI HTTP client* (DESIGN.md §7.2,
# imagegen/model.py + comfyui_client.py) — the GPU lives in the separate ComfyUI
# container, not here — so the worker needs no CUDA, and there is no health
# server yet (imagegen/healthz.py is still future work). Hence python:slim and
# no HEALTHCHECK. Revisit §7.1 if/when healthz.py lands.
FROM python:3.12-slim AS base

# git  — pip installs image-gen-contract from a GitHub ref pinned in pyproject.
# tini — PID 1 that forwards SIGTERM to the puller so run_forever() drains
#        in-flight jobs before exit (DESIGN.md §10.2 graceful drain).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git tini \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user (DESIGN.md §12.2).
RUN groupadd -r imagegen && useradd -r -g imagegen -u 1001 imagegen

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Copy the package + project metadata, then install with the `runtime` extra
# (google-cloud-pubsub/storage, httpx, websocket-client). package-data ships the
# workflows/ + templates/ JSON the model resolves at runtime (pyproject.toml).
COPY pyproject.toml ./
COPY imagegen/ ./imagegen/
RUN pip install ".[runtime]"

USER imagegen

# tini reaps zombies and forwards signals; `python -m imagegen.main` blocks in
# the streaming pull until SIGTERM. WORKDIR=/app keeps the local imagegen/
# package importable, so a dev bind-mount over /app/imagegen hot-reloads source.
ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "imagegen.main"]
