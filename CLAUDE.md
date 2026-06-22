# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Git workflow

Commit directly to the currently checked-out branch (normally `main`). **Do not create new branches** — even when committing on the default branch — unless explicitly asked. This applies to the sibling repos worked on together (`../ImageGenContract`, `../Application`) too.

## Commands

```bash
# Unit tests (with coverage gate — fail_under=100 excluding main.py + comfyui_client.py)
./tests/run_tests.sh                          # installs sibling ImageGenContract, then runs
~/python_env/torch-env/bin/python -m pytest tests/unit/ -q          # unit only, no reinstall
~/python_env/torch-env/bin/python -m pytest tests/unit/test_workflow.py -q  # single file

# Integration tests (require Docker for emulators via testcontainers)
~/python_env/torch-env/bin/python -m pytest tests/integration/ -q

# Lint
ruff check imagegen/
mypy imagegen/

# Story prompt smoke test against a live ComfyUI container on :8188
PYTHONPATH=. ~/python_env/torch-env/bin/python scripts/smoke_real_comfyui.py \
    --url http://localhost:8188 --input tests/assets/test.jpg --out /tmp/smoke_out

# Local batch generate + eval + review (live ComfyUI on :8188; no GCS/Pub/Sub).
# See the `local-batch-eval` skill for the full generate→grade→review loop.
PYTHONPATH=. ~/python_env/torch-env/bin/python scripts/generate_stories.py \
    --input tests/assets/leo.jpg --age "4-year-old" --run-dir eval_runs/latest
PYTHONPATH=. ~/python_env/torch-env/bin/python \
    .claude/skills/prompt-eval/fetch_outputs.py --local-root eval_runs/latest/outputs \
    --log-dir eval_runs/latest/prompt_logs --story 1_1 --user-id leo --story-id 1_1 \
    --out eval_runs/latest/eval/1_1__1_1            # then judge via prompt-eval rubric
~/python_env/torch-env/bin/python tools/review_app/server.py --run-dir eval_runs/latest

# Story catalog sync (writes story metadata → API server's Firestore templates/ collection)
operation/stages/dev/sync_story_catalog.sh [--dry-run]
operation/stages/preprod/sync_story_catalog.sh [--template ID] [--dry-run]
operation/stages/prod/sync_story_catalog.sh [--template ID] [--dry-run]

# Dev stack (emulators + mock ComfyUI + worker container)
deploy/stages/dev/up.sh          # brings up the dev compose stack
deploy/stages/dev/smoke.sh       # end-to-end smoke via emulators
deploy/stages/dev/down.sh        # stop

# Prod deploy (graceful drain + recreate)
IMAGE=ghcr.io/<org>/imagegen-worker@sha256:<digest> deploy/stages/prod/deploy.sh
```

Python environment: always use `~/python_env/torch-env/bin/python` (see global CLAUDE.md).

## Architecture

The worker is a **stateless Pub/Sub consumer**. It pulls image-generation jobs from GCP `image-gen-jobs`, runs them through a ComfyUI container, uploads outputs to GCS, and publishes completions to `job-completed`. All retry, dedup, and failure accounting live in the API server (`../Application`), not here. The worker holds no persistent state — no DB, no dedup table.

### End-to-end flow

```
API server → Pub/Sub image-gen-jobs → [worker pulls] → ComfyUI (HTTP+WS)
         ↓ output PNGs → GCS → Pub/Sub job-completed → API server /internal/jobs/completed
```

The job event is a thin selector — `{schema_version, story_id, user_id, request_id, type, id, input_images:[{photo_id, position}]}`. No image bytes, no gcs_uri: the worker downloads inputs by the deterministic name `gs://$GCS_BUCKET/<user_id>_<story_id>_input_<position>.png` and writes outputs to `gs://$GCS_BUCKET/<user_id>/<story_id>/outputs/<index>.png`. `type`/`id` pick the prompt set `prompts/<type>_<id>.json`, rendered through the live render template (`templates/2`; `_RENDER_TEMPLATE_ID` in `model.py`).

Per-panel streaming: for an N-panel story the worker publishes N non-terminal `panel_completed` events (one per panel as it finishes) followed by one terminal `completed`. The API side is expected to stream these to the client. The job is only ack'd after the terminal event.

### Module map (`imagegen/`)

| File | Role |
|---|---|
| `main.py` | Entrypoint: wires config → model → handler → puller. Excluded from unit coverage. |
| `config.py` | Env-driven `WorkerConfig` dataclass. Required vars: `GCP_PROJECT_ID`, `JOBS_SUBSCRIPTION`, `COMPLETION_TOPIC`, `GCS_BUCKET` (inputs read / outputs written here — the job carries no gcs_uri/output_prefix). |
| `puller.py` | `google-cloud-pubsub` StreamingPull wrapper; handles ack lease extension. |
| `job_handler.py` | `JobHandler.handle()` — parses message → downloads inputs → runs model → uploads outputs → publishes completion. Routes all exceptions through `failure_classification`. |
| `model.py` | `ComfyUIModel` — implements the `ImageGenModel` Protocol; drives a ComfyUI container via `ComfyUITransport`. `generate(story_id, user_id, prompt_type, prompt_id, input_images)` validates inputs eagerly, then yields one `PanelResult` per saved variant lazily. |
| `workflow.py` | Pure, no-I/O: loads workflow/template JSON assets, resolves story prompt arrays + character `{TOKEN}` placeholders, renders an API-format ComfyUI prompt per panel. |
| `comfyui_client.py` | Real `httpx` + `websocket-client` transport. Not unit-tested; pinned by integration test against mock ComfyUI. |
| `failure_classification.py` | Maps exceptions to `Disposition.NACK_RETRY` or `Disposition.REPORT_FAILED`. The policy is exhaustively unit-tested here, separately from the handler. |
| `gcs.py` | Thin `google-cloud-storage` wrapper. |
| `publisher.py` | `CompletionPublisher` — publishes to `job-completed`. |
| `completion_builder.py` | Builds `panel_completed`, `completed`, and `failed` payloads; every call generates a fresh `event_id` (ULID). |

### Failure taxonomy

Three exception families, each with a distinct Pub/Sub posture:

- **Terminal reported** (`UnsupportedTemplateError`, `CorruptInputError`, `InvalidConfigError`) → publish `status='failed'` + ack. User gets a refund; never retried.
- **Transient** (`GcsTransientError`, `ModelTransientError`, `PublishTransientError`) → nack; Pub/Sub redelivers per `retry_policy`. On the final delivery attempt (`delivery_attempt >= max_delivery_attempts`), the handler converts transients to a terminal `failed` rather than letting the job silently dead-letter.
- **Unknown** → nack; eventually DLQ.

### Workflow/template/prompt asset chain

Assets ship as package data (`pyproject.toml [tool.setuptools.package-data]`):

```
imagegen/
├── workflows/{1,2}/workflow.json  # ComfyUI API-format graphs (1 = Flux, 2 = Qwen-Image-Edit-2511)
├── workflows/{1,2}/config.json    # positional node list (what the template may override)
├── templates/{1,2}/config.json    # render templates: 6 per-panel field presets each
└── prompts/
    ├── character.json          # {TOKEN} → description for supporting characters
    └── <type>_<id>.json        # story prompts (`story-prompts` skill) + read-aloud `texts` (`story-text` skill)
```

Two parallel render templates ship (`templates/1`+`workflows/1` = Flux; `templates/2`+`workflows/2` = Qwen-Image-Edit-2511), each 6 panels. The live worker renders **every** story through `_RENDER_TEMPLATE_ID = "2"` (`model.py`); template 1 stays in the asset library as the legacy/alternate. The job's `type`/`id` select which `prompts/<type>_<id>.json` set fills the panels' `text` — the template no longer binds a story inline. `prepare(template_id, story_ref)` loads + validates the template and applies that prompt set; `.render()` applies per-panel values + `USER_ID`/`STORY_ID` substitution. `{TOKEN}` character placeholders resolve at `prepare()` from `character.json`; `USER_ID`/`STORY_ID` at `render()` per job. Panel count = `len(prompts)` (must equal the template's 6).

Workflow 1 saves two variants per panel run: `_V1` (pre-face-swap) and `_V2` (face-restored). The model yields both as separate `PanelResult`s and owns the storybook layout (`index`/`panel_index`/`variant`/`total`) — a 6-panel story with 2 variants = 12 outputs. The job carries no `output_count`/`variants_per_panel`.

### Test structure

```
tests/
├── unit/         # pure logic, no I/O — matches one-to-one with imagegen/ modules
├── integration/  # real ComfyUI client vs. in-process mock; real emulators for GCS/Pub/Sub
├── fakes/
│   ├── comfyui.py   # FakeComfyUI: in-process transport that replays a real WS event stream
│   └── worker.py    # Pub/Sub message + GCS client fakes
└── mock_comfyui/    # aiohttp server — runnable mock ComfyUI container (port 8188 convention)
```

`FakeComfyUI` (in `tests/fakes/comfyui.py`) is the primary test double for `model.py`. It synthesises `/history` outputs from the submitted workflow's SaveImage prefixes, so V1/V2 filename filtering runs against realistic data. The real `comfyui_client.py` is excluded from unit coverage and is tested only in `tests/integration/test_comfyui_client.py` against the runnable mock.

### Prompts / character skill boundary

Three Claude Code skills in `.claude/skills/` own specific files (and, within
`<type>_<id>.json`, specific fields):

- **`story-prompts`** — writes/edits `imagegen/prompts/<type>_<id>.json` (the image `prompts` + metadata, everything but `texts`)
- **`story-text`** — writes/edits the `texts` array of `imagegen/prompts/<type>_<id>.json` (the per-panel read-aloud storybook narration + dialog; never seen by the image model, carries no `{TOKEN}`)
- **`character-config`** — edits `imagegen/prompts/character.json` (the generated supporting cast)

After editing prompts, story text, or character tokens, re-run the appropriate `operation/stages/<stage>/sync_story_catalog.sh` to propagate story titles/lessons + the per-panel `story_text` to the API server's Firestore `templates/` collection.

### Evaluating generated outputs

Two more skills (no file ownership — they read + grade):

- **`prompt-eval`** — judges already-generated panel images against their prompts with the vision model and writes a per-story `report.md`. Its `fetch_outputs.py` reads outputs from the **Application stack's GCS by default**, or from a **local dir tree** when given `--local-root` / `LOCAL_OUTPUT_ROOT` (same `<user>/<story>/outputs/<i>.png` layout).
- **`local-batch-eval`** — the generate-**and**-grade loop that runs entirely on this machine (live ComfyUI only, no Pub/Sub/GCS/Application stack): `scripts/generate_stories.py` drives `ComfyUIModel` directly for the whole catalog from one input photo at a fixed age, writing PNGs + prompt logs to `eval_runs/<run>/`; then `prompt-eval` (in `--local-root` mode) grades them; then `tools/review_app/server.py` serves a one-page web UI (input photo + actual prompts + output image + eval) for review. `eval_runs/` is gitignored.

### Key invariants

- `MAX_PROCESSING_SECONDS` must be < 600 (Pub/Sub ack deadline). For N panels: `N × COMFYUI_REQUEST_TIMEOUT_SECONDS < MAX_PROCESSING_SECONDS`.
- `MAX_DELIVERY_ATTEMPTS` must mirror the subscription's `dead_letter_policy.max_delivery_attempts` (default 5). Mismatch silently changes when the handler converts transients to terminal failures.
- The worker SA (`imagegen-worker@<project>.iam.gserviceaccount.com`) has no Firestore access. The `operation/sync_story_catalog.py` script runs under operator ADC, not the worker SA.
- Message wire format is defined in the sibling repo `../ImageGenContract`. Both repos import `from image_gen_contract import JobMessage, ...` — no local copy of the schema.

### Contract pin

The worker runs only on this local machine, so `image-gen-contract` is **not published as a versioned release** — `pyproject.toml` pins a raw commit of the sibling `../ImageGenContract` clone and the Docker build (`pip install`) fetches that exact commit from GitHub. **Whenever you change the local `../ImageGenContract` repo and the worker depends on it, push that commit to GitHub and bump the pin SHA in `pyproject.toml` to the new contract HEAD.** A pin that lags behind the local HEAD means the build bakes a contract the current worker code rejects. The commit must be reachable from a pushed branch/tag (an unreferenced SHA isn't fetchable), so push before you bump. `../Application` carries its own pin to the same contract — bump it separately when the API side needs the change.
