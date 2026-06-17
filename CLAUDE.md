# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

The job event is a thin selector — `{schema_version, story_id, user_id, request_id, type, id, input_images:[{photo_id, position}]}`. No image bytes, no gcs_uri: the worker downloads inputs by the deterministic name `gs://$GCS_BUCKET/<user_id>_<story_id>_input_<position>.png` and writes outputs to `gs://$GCS_BUCKET/<user_id>/<story_id>/outputs/<index>.png`. `type`/`id` pick the prompt set `prompts/<type>_<id>.json`, rendered through `templates/1`.

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
├── workflows/1/workflow.json   # ComfyUI API-format graph (the only workflow)
├── workflows/1/config.json     # positional node list (what the template may override)
├── templates/1/config.json     # the only render template: 6 per-panel field presets
└── prompts/
    ├── character.json          # {TOKEN} → description for supporting characters
    └── <type>_<id>.json        # story prompts — owned by the `story-prompts` skill
```

There is **one** workflow (`workflows/1`) and **one** render template (`templates/1`, 6 panels). The job's `type`/`id` select which `prompts/<type>_<id>.json` set fills the panels' `text` — `templates/1` no longer binds a story inline. `prepare(template_id, story_ref)` loads + validates the template and applies that prompt set; `.render()` applies per-panel values + `USER_ID`/`STORY_ID` substitution. `{TOKEN}` character placeholders resolve at `prepare()` from `character.json`; `USER_ID`/`STORY_ID` at `render()` per job. Panel count = `len(prompts)` (must equal the template's 6).

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

The two Claude Code skills in `.claude/skills/` own specific files:

- **`story-prompts`** — writes/edits `imagegen/prompts/<type>_<id>.json`
- **`character-config`** — edits `imagegen/prompts/character.json` (the generated supporting cast)

After editing prompts or character tokens, re-run the appropriate `operation/stages/<stage>/sync_story_catalog.sh` to propagate story titles/lessons to the API server's Firestore `templates/` collection.

### Key invariants

- `MAX_PROCESSING_SECONDS` must be < 600 (Pub/Sub ack deadline). For N panels: `N × COMFYUI_REQUEST_TIMEOUT_SECONDS < MAX_PROCESSING_SECONDS`.
- `MAX_DELIVERY_ATTEMPTS` must mirror the subscription's `dead_letter_policy.max_delivery_attempts` (default 5). Mismatch silently changes when the handler converts transients to terminal failures.
- The worker SA (`imagegen-worker@<project>.iam.gserviceaccount.com`) has no Firestore access. The `operation/sync_story_catalog.py` script runs under operator ADC, not the worker SA.
- Message wire format is defined in the sibling repo `../ImageGenContract`. Both repos import `from image_gen_contract import JobMessage, ...` — no local copy of the schema.
