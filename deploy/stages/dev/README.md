# Stage: dev

Emulator-backed worker stack for the dev laptop — no GPU, no GCP credentials
(DESIGN.md §9). It wires the **real** worker into the **Application's** local
stack so the API server and the worker share one Pub/Sub emulator: the worker
pulls the jobs the server publishes and writes completions the server can read.

## What it brings up (`docker-compose.yml`)

| Service | Image | Role |
|---|---|---|
| `fake-gcs-server` | `fsouza/fake-gcs-server` | GCS the worker downloads inputs from / uploads outputs to. |
| `imagegen-worker` | built from the repo root `Dockerfile` | The worker — pulls jobs, runs the model, publishes completions. |
| `mock-comfyui` | built from `tests/mock_comfyui` | Drop-in ComfyUI: streams the real WebSocket event sequence + returns PNGs, no models. **Off by default** — opt in with `./up.sh --mock`. |

By default the worker talks to the **host's real ComfyUI** on `:8188`
(reached via `host.docker.internal`), so generation produces real images. Pass
`./up.sh --mock` to run the bundled mock ComfyUI instead (no GPU/models needed).

It deliberately does **not** run a Pub/Sub emulator. The worker attaches to the
Application local stack's docker network (`APPSTACK_NETWORK`, default
`local_default`) and reaches its `pubsub-emulator` by name, on project
`tarostory-local`. Bring that stack up first:

```bash
../../../../Application/server/deploy/stages/local/up.sh
```

> The Application local stack also ships an `image-gen-stub` subscribed to the
> same `image-gen-jobs-worker-sub`. Stop it so the real worker is the sole
> consumer: `docker stop local-image-gen-stub-1`. (Re-running that stack's
> `up.sh` restarts the stub — making the swap permanent is an Application-side
> change: drop/replace the stub service there.)

## Run it

```bash
./up.sh         # build + start against the host's real ComfyUI (:8188)
./up.sh --mock  # ...or against the bundled mock ComfyUI (no GPU/models)
./smoke.sh      # seed GCS → publish a job → print the worker's completions
./down.sh       # stop

docker compose -f docker-compose.yml logs -f imagegen-worker   # tail
```

> Real ComfyUI must be listening on the host's `:8188` (the default backend).
> If it isn't running, use `./up.sh --mock`, or point elsewhere with
> `COMFYUI_URL=... ./up.sh`.

`smoke.sh` runs `smoke.py` inside the worker image (so it reaches the emulators
by service name). A green run prints one `panel_completed` per panel followed by
a terminal `completed` carrying the output `gs://` URIs — exactly what a real
`POST /stories` → result_processor round-trip will carry once the API server
publishes jobs.

## Config (`env.sh`)

Everything is overridable inline. Notable knobs:

- `COMFYUI_BACKEND` — `real` (default) talks to the host's ComfyUI on `:8188`
  via `host.docker.internal`; `mock` runs the bundled `mock-comfyui` (activates
  the `mock` compose profile). `./up.sh --mock` / `--real` are shorthands.
- `COMFYUI_URL` — derived from `COMFYUI_BACKEND`, but wins if set explicitly,
  e.g. `COMFYUI_URL=http://host.docker.internal:8188 ./up.sh` (add the
  host-gateway mapping if your Docker needs it).
- `APPSTACK_NETWORK` — the Application local stack's network (default
  `local_default`; check `docker network ls`).
- `GCS_BUCKET` — bucket `smoke.py` seeds/reads (default `tarostory-local-images`).

## ⚠️ Temporary local-contract bridge

The worker emits incremental `panel_completed` events, but the
`image-gen-contract` ref pinned in `pyproject.toml` (`7ee0c64`) **predates** that
status — so against the baked contract the worker raises on every job. The
commit that adds `panel_completed` lives in the sibling `../ImageGenContract`
clone but **is not yet pushed to GitHub**, so the pin can't be bumped to it.

Until the contract is published and the pins (here **and** in `../Application`)
are bumped — coordinated with the API-side `panel_completed` handling DESIGN.md
§6.4 flags as a cross-repo TODO — the dev worker service runs as root and
force-reinstalls the sibling contract over the baked one at startup (the same
sibling source `tests/run_tests.sh` already installs). Remove the `user`,
`command`, and `/contract` mount from `docker-compose.yml` once the pin is
current.
