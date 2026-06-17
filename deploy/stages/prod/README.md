# Stage: prod

The pinned worker image running against real GCP on a worker host (DESIGN.md
§10). Outbound HTTPS only — no inbound ports (§12.1).

## Prerequisites

- Docker engine on a Linux host.
- A **ComfyUI** reachable at `COMFYUI_URL` (default `http://host.docker.internal:8188`
  — the compose adds the host-gateway mapping). The GPU lives in *that* container;
  the worker is a ComfyUI HTTP client (§7.2), so the worker host itself needs no GPU.
- The worker **service-account key** at `$SA_KEY_FILE` (`root:root 0400`,
  default `/etc/imagegen/sa.json`) — mounted as a Docker secret, never baked
  into the image (§10.3).
- A **pinned image** in `IMAGE` — deploy by digest, not `:latest` (§7.1).

## Deploy / upgrade (`deploy.sh`)

```bash
# Pull the pinned image, then recreate detached so the old container drains.
IMAGE=ghcr.io/<org>/imagegen-worker@sha256:<digest> ./deploy.sh
```

Override any default inline, e.g. `GCP_PROJECT_ID=tarostory-staging
MAX_CONCURRENCY=8 ./deploy.sh`. Config lives in `env.sh`.

## Deviations from DESIGN.md §10.1

Kept faithful otherwise (read-only rootfs, dropped caps, non-root user via the
image, SA-key secret, 600s graceful drain), with two corrections:

- **No GPU reservation.** §10.1 reserved an nvidia device back when the model
  ran in-process. The model is now a ComfyUI HTTP client (§7.2) — the GPU
  belongs to the separate ComfyUI container, not the worker.
- **No `:9100` healthcheck.** `imagegen/healthz.py` (§11.1) doesn't exist yet.
  Add a `HEALTHCHECK` here and in the `Dockerfile` once it lands.

Scaling out is just running this compose on another host pointed at the same
subscription — Pub/Sub load-balances StreamingPull across them (§11.4).
