# Mock ComfyUI container

A runnable stand-in for the real ComfyUI service (`../../../ImageGenComfyui`)
for development and integration testing. It speaks the subset of ComfyUI's
HTTP + WebSocket API that the worker's `HttpComfyUIClient`
(`imagegen/comfyui_client.py`) uses — **including the real-time WebSocket event
stream** — but needs no GPU, no models, and no minutes of compute.

When a prompt is queued it streams the same events a real generation emits
(`execution_start` → per-node `executing` + `progress` → terminal
`executing(node=None)` / `execution_success`), paced by `MOCK_STEP_DELAY`, then
serves a generated PNG for each SaveImage node from `/view`. So the worker
exercises the genuine *blocking-on-WebSocket* path, just fast.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/system_stats` | healthcheck |
| POST | `/upload/image` | multipart upload (echoes the stored name) |
| GET | `/ws?clientId=<id>` | execution event stream |
| POST | `/prompt` | `{"prompt": <workflow>, "client_id": <id>}` |
| GET | `/history/{prompt_id}` | execution status + outputs |
| GET | `/view?filename=...` | produced image bytes |

## Configuration

| Env | Default | Meaning |
|---|---|---|
| `MOCK_PORT` | `8188` | listen port |
| `MOCK_STEP_DELAY` | `2.0` | seconds between per-node events (simulate slow gen; ~0 in tests) |
| `MOCK_FAIL_MODE` | `none` | `none` \| `bad_prompt` (400 on `/prompt`) \| `execution_error` (error on the WS) |
| `MOCK_WIDTH` / `MOCK_HEIGHT` | `1024` / `736` | output PNG dimensions |

## Run it

```bash
# Standalone (foreground):
docker compose -f tests/mock_comfyui/docker-compose.yml up --build

# Then point the worker at it:
COMFYUI_URL=http://localhost:8188 ./deploy/deploy.sh dev
```

The compose file joins the external `imagegen-backend` network (same as the real
ComfyUI), so on that network the worker reaches it at
`http://mock-comfyui:8188`. Create the network once if it doesn't exist:

```bash
docker network create imagegen-backend
```

## In tests

`tests/integration/test_comfyui_client.py` imports `build_app()` from
`server.py`, runs it in-process on a random port, and drives the **real**
`HttpComfyUIClient` + `ComfyUIModel` against it — the only place the real
HTTP+WebSocket transport (otherwise excluded from unit coverage) is exercised.
Unit tests use the lighter in-process fake in `tests/fakes/comfyui.py` instead.
