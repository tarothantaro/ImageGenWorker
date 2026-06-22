# Image Gen Worker — Testing Strategy

> Companion doc to [DESIGN.md](./DESIGN.md). This file specifies the test layers, fixtures, stub services, and CI strategy for the image gen worker. The worker is stateless (DESIGN §1, §6), which simplifies testing: there is no local database to reset between tests, no migrations to run, and no per-test data hygiene beyond emulator state.

---

## 1. Test Layers

| Layer | Scope | Dependencies | Speed | When to add |
|---|---|---|---|---|
| Unit | Pure logic in single modules (config, schema, failure classification, retry decisions) | None | <1s/file | Default — every code change |
| Integration | One module against a real external dep | pubsub-emulator, fake-gcs-server | ~10s | When touching Pub/Sub, GCS, or model-IO interfaces |
| End-to-end | Full worker container + emulators, message flowing through | `docker-compose.dev.yml` | ~60s | When changing the `puller → handler → publisher` flow, or before release |

The pyramid is intentional: most bugs are in the failure-classification and schema-validation logic, both of which test cleanly at the unit layer. Integration tests pin down the wire-format contract with Pub/Sub and GCS. E2E tests exist to catch wiring/config bugs that unit and integration miss — they are not where edge-case logic gets tested.

```
tests/
├── run_tests.sh           # unit suite + coverage (runs from repo root)
├── unit/                  # pure logic, no I/O
├── integration/           # real comfyui_client ↔ mock ComfyUI (and emulators)
│   └── test_comfyui_client.py
├── e2e/                   # docker-compose.dev.yml + assertions
├── fakes/                 # reusable in-process fakes
│   ├── comfyui.py         # in-process WS-replaying ComfyUI fake (see §2.1)
│   └── worker.py          # Pub/Sub message + GCS client fakes
├── mock_comfyui/          # runnable mock ComfyUI container (server + Dockerfile)
├── fixtures/
│   ├── jobs/              # canonical job payloads
│   ├── completions/       # canonical completion payloads
│   └── images/            # tiny sample inputs/outputs
└── stubs/
    └── image-gen-stub/    # see §5 — published as a separate Docker image
```

## 2. Unit Tests

Located at `tests/unit/`. No I/O, no subprocess, no emulators, no `time.sleep`. Each test file matches a single module under `imagegen/`.

| File | Covers |
|---|---|
| `test_config.py` | Env var parsing, defaults, validation. Fail fast on missing required vars. |
| `test_schema.py` | Job + completion message schemas. Reject unknown `schema_version`, missing required fields, extra fields per policy. |
| `test_failure_classification.py` | Given a model exception or GCS error, decide: nack-and-redeliver vs. publish-failed-completion vs. let-Pub/Sub-DLQ. This is the heart of §6.2 — exhaustive coverage matters. |
| `test_completion_builder.py` | Building a `job-completed` payload from a `JobResult`. Asserts that every retry generates a fresh `event_id` (DESIGN §5.2). |
| `test_handler_logic.py` | The pure-logic part of `job_handler.py` with `gcs` and `publisher` mocked, exercising the decision tree. |
| `test_workflow.py` | The pure workflow renderer (`workflow.py`): loads `workflows/1` + `templates/1` (applying a prompt set via `prepare(template_id, story_ref)`), applies placeholders/image-remap, and lists the SaveImage output prefixes in `_V<n>` order (`output_prefixes`; `final_output_prefix` still resolves `_V2`). Malformed assets → `UnsupportedTemplateError`. |
| `test_model.py` | `ComfyUIModel` (`model.py`) against the mock ComfyUI container: the exact workflow + params sent, **every saved variant per panel run** as a separate output (`templates/1` = 6 panels × V1/V2 → 12 outputs, tagged `panel_index`/`variant`/`total`; a crafted multi-panel template asserts per-panel seeds), the per-request timeout, and every worker-side (`CorruptInput`/`UnsupportedTemplate`) and ComfyUI-side error mapping. |
| `test_comfyui_worker.py` | Full `JobHandler` + real `ComfyUIModel` + mock ComfyUI, driven by job messages: asserts GCS outputs, the incremental `panel_completed` events + terminal `completed`, and ack/nack per outcome. |

Run:
```bash
pytest tests/unit/ -q
# or, with the coverage gate + sibling-contract install:
./tests/run_tests.sh
```

CI target: < 30s.

### 2.1 Mocking ComfyUI

`model.py` reaches ComfyUI through the `ComfyUITransport` Protocol, so unit
tests inject `tests/fakes/comfyui.py:FakeComfyUI` — an in-process stand-in. It
implements the transport surface (`upload_image`, `open_events`,
`queue_prompt`, `get_history`, `fetch_image`), and:

- **replays a real-time WebSocket stream** from `open_events`: `status` →
  `execution_start` → per-node `executing` + `progress` → terminal
  `executing(node=None)` / `execution_success`. The model rides that stream
  exactly as in production (a generation takes minutes — we block on the
  socket, we don't poll);
- records every uploaded image and submitted workflow, so tests assert exactly
  what the model sent (the rendered `workflow.json` and each node's params);
- synthesizes `/history` outputs **from the submitted workflow's SaveImage
  prefixes**, so the model's V1/V2 filename filtering runs against realistic
  data;
- returns a real minimal PNG carrying configurable dimensions;
- can fail like a real container — refuse upload/queue, 4xx a bad prompt, raise
  an `execution_error` mid-stream, time out the socket, close the stream early,
  or emit no/garbled output — driving every error path without a GPU.

For a **runnable** mock (real HTTP + WebSocket, Dockerized), see
`tests/mock_comfyui/` — an aiohttp server that streams the same events and
serves generated PNGs, mirroring `../ImageGenComfyui`'s container conventions
(port 8188, `/system_stats` healthcheck, the `imagegen-backend` network). Point
the worker at it with `COMFYUI_URL=http://mock-comfyui:8188`.

The real transport (`comfyui_client.py`, `httpx` + `websocket-client`) is
**not** unit-tested (excluded from coverage like `main.py`); it is pinned by
the §3 integration test against the mock container.

## 3. Integration Tests

Located at `tests/integration/`. Each test brings up only the emulator(s) it needs and tears them down.

```
tests/integration/
├── conftest.py            # pytest fixtures: pubsub_emulator, fake_gcs, project_id
├── docker-compose.yml     # used by conftest.py via testcontainers / pytest-docker-tools
├── test_puller.py
├── test_publisher.py
├── test_gcs.py
├── test_comfyui_client.py
├── test_handler_e2e_in_proc.py
└── test_exactly_once.py
```

| Test | What it pins down |
|---|---|
| `test_comfyui_client.py` | The real `HttpComfyUIClient` (HTTP + WebSocket) driven end-to-end against the in-process mock ComfyUI (`tests/mock_comfyui/server.py`, started on a random port): streams a 2-image job to completion over the WS, maps a `/prompt` 400 to `InvalidConfigError`, and a WS `execution_error` to `ModelTransientError`. No GPU; the only place `comfyui_client.py` runs for real. Auto-skips if `aiohttp`/`websocket-client` are absent. |
| `test_puller.py` | Publish to `image-gen-jobs`, the `Puller` calls `on_message` exactly once, ack→no redelivery. Then nack→redelivery within `retry_policy.minimum_backoff`. Lease-extension path: simulate a 30s processing window with `max_lease_duration=60` and verify the message stays leased. |
| `test_publisher.py` | Publish a completion, subscribe with a fresh test-only subscription, verify the message body and attributes match what was sent. |
| `test_gcs.py` | Round-trip a JPEG through fake-gcs-server: upload → download → bytes match. Plus the derived per-story URIs (`input_uri` = `<user>_<story>_input_<position>.png`, `output_uri` = `<user>/<story>/outputs/<index>.png`). |
| `test_handler_e2e_in_proc.py` | Full `JobHandler.handle()` against both emulators with a stub `model.generate()` that returns deterministic bytes. Verifies: input is downloaded from the right URI, output lands at the right URI, completion is published with `status='completed'` and the expected `output_images[]`. |
| `test_exactly_once.py` | With `enable_exactly_once_delivery=true`, simulate a slow processor (sleep > base ack deadline but extend lease) and verify no duplicate delivery. Then simulate a worker crash by abandoning the lease and verify redelivery happens after lease expiration. |

Conftest pattern:
```python
@pytest.fixture(scope="session")
def pubsub_emulator() -> Generator[str, None, None]:
    container = DockerContainer("gcr.io/google.com/cloudsdktool/google-cloud-cli:emulators") \
        .with_command("gcloud beta emulators pubsub start --host-port=0.0.0.0:8085") \
        .with_exposed_ports(8085)
    container.start()
    host = f"localhost:{container.get_exposed_port(8085)}"
    os.environ["PUBSUB_EMULATOR_HOST"] = host
    yield host
    container.stop()
```

Each test creates its own topic/subscription with a unique suffix to allow parallel test execution within the session.

Run:
```bash
pytest tests/integration/ -q
```

CI target: < 3 minutes.

## 4. End-to-End Tests

Located at `tests/e2e/`. Bring up the full `docker-compose.dev.yml` stack and exercise the worker as a black box.

`tests/e2e/conftest.py` runs `docker compose -f docker-compose.dev.yml up -d --wait` once per session. The worker container is the **production image** (built from the same `Dockerfile`) — not an in-process worker — so we catch any container-only bugs (signal handling, healthcheck wiring, env injection).

```
tests/e2e/
├── conftest.py
├── test_happy_path.py
├── test_failure_paths.py
├── test_redelivery.py
├── test_concurrency.py
└── test_shutdown.py
```

| Test | Scenario |
|---|---|
| `test_happy_path.py` | Publish 1 job with 1 input image. Within 30s, observe a `job-completed` with `status='completed'`, `output_images.length == 4`, and four PNGs at the expected URIs. |
| `test_failure_paths.py` | Stub model raises `UnsupportedTemplateError`; verify completion has `status='failed'`, `failure_reason='unsupported_template'`, and no GCS outputs. |
| `test_redelivery.py` | Stub model raises a transient error on first attempt, succeeds on second. Verify exactly one *successful* completion is observed (the first attempt's failure should not be ack'd as a failed completion — it should nack and redeliver). |
| `test_concurrency.py` | Publish 10 jobs with `MAX_CONCURRENCY=4`. Verify all 10 complete; verify `imagegen_worker_jobs_in_flight` never exceeds 4 (scrape `:9100/metrics` mid-run). |
| `test_shutdown.py` | Publish 2 jobs; while they're processing, send SIGTERM via `docker compose stop`; verify both jobs publish their completions before container exits, within `stop_grace_period`. |

Run:
```bash
make test-e2e
# under the hood:
# docker compose -f docker-compose.dev.yml up -d --wait
# pytest tests/e2e/ -q
# docker compose -f docker-compose.dev.yml down -v
```

CI target: < 5 minutes.

## 5. Stub Image Gen Service (for the Application repo)

The Application repo's e2e suite needs a fake image-gen worker subscribing to `image-gen-jobs`. This repo provides that stub at `tests/stubs/image-gen-stub/` and **publishes it as a standalone Docker image** so the Application repo can pull it without source access.

### 5.1 Behavior

The stub:
- Subscribes to `image-gen-jobs-worker-sub` on `PUBSUB_EMULATOR_HOST`.
- On message receipt:
  - Sleeps for `STUB_DELAY_SECONDS` (default `2`).
  - With probability `STUB_FAIL_PROB` (default `0`), publishes `status='failed'` instead of `'completed'`.
  - Generates placeholder PNGs (a colored gradient) and uploads them to the derived per-story output path `gs://$GCS_BUCKET/<user>/<story>/outputs/<index>.png` on `STORAGE_EMULATOR_HOST`.
  - Publishes a completion to `job-completed` matching the schema in DESIGN §5.2.
  - Acks the original message.
- Logs every step in the same JSON structlog format as the real worker, so the Application repo's e2e logs look uniform.

### 5.2 Image

Built and pushed by this repo's CI on every release tag:
```
ghcr.io/<org>/imagegen-stub:<git-sha>
ghcr.io/<org>/imagegen-stub:latest
```

The Application repo's `docker-compose.yml` references it by tag.

### 5.3 The Application repo's `make test-local` flow

```
test → POST /stories
     → stories router HOLDS credits + creates invisible photos + writes
       story doc, publishes to image-gen-jobs (emulator)
     → image-gen-stub pulls, "generates", publishes job-completed
     → push handler /internal/jobs/completed runs result_processor →
       in one Firestore txn: status → pending_selection, photos visible=true,
       credit_held -= N, credit_balance -= N, credit_transactions written
     → SSE stream sees story_ready (success); on stub-injected failure
       result_processor releases the hold and deletes the invisible photos
       and the SSE stream sees `failed`
```

Configuration knobs the Application repo can set on the stub:
- `STUB_DELAY_SECONDS` — simulate slow generation.
- `STUB_FAIL_PROB` — exercise the failure path.
- `STUB_FAIL_REASON` — pin which `failure_reason` code is emitted, useful for asserting specific UI strings.

## 6. Shared Schema Validation

The wire-format contract for the job and completion messages lives in a
sibling repo, `../ImageGenContract/`:
- `image_gen_contract/schemas/job.json` — DESIGN §5.1
- `image_gen_contract/schemas/completion.json` — DESIGN §5.2
- `image_gen_contract/messages.py` — Pydantic v2 bindings both repos import as `from image_gen_contract import …`

Both repos depend on the `ImageGenContract` package — there is no second
copy of the schemas or models in this tree. `tests/run_tests.sh` runs
`pip install -e ../ImageGenContract --quiet` before pytest so the editable
install always tracks the sibling repo's source.

The contract repo's own `tests/test_jsonschema_alignment.py` cross-validates
that the JSON Schemas and the Pydantic models accept and reject the same
payloads — that is what protects us from a class of bugs where the worker
emits a slightly off-spec completion that the API parses leniently in dev
but rejects in prod.

## 7. Manual / Smoke Testing

For changes to GPU / model integration that emulators can't catch:

### 7.0 Against a live ComfyUI container

`tests/smoke/smoke_real_comfyui.py` drives the real `ComfyUIModel` + `HttpComfyUIClient` (httpx + websocket-client) through template 3 / workflow 2 against a running ComfyUI container, with a real input photo — the GPU/model path the in-process mock and emulators can't cover. No Pub/Sub or GCS involved; it exercises upload → render → submit → live WS stream → fetch the `_V2` image.

```bash
# ComfyUI container up on :8188 (see ../ImageGenComfyui/docker-compose.yml)
PYTHONPATH=. ~/python_env/torch-env/bin/python tests/smoke/smoke_real_comfyui.py \
    --url http://localhost:8188 \
    --input tests/assets/test.jpg \
    --out /tmp/smoke_out --timeout 300
```

It exits non-zero on any failure and writes each produced panel to `--out`. The workflow assets must match the container's installed custom nodes — e.g. the bundled `ReActorOptions` node carries `restore_swapped_only`, required by current `comfyui-reactor`; a missing required input makes ComfyUI silently drop that output branch (`Failed to validate prompt for output …` in the container log) and the worker then reports "no output image matching `…_V2`".

### 7.1 Against a dev GCP project

A separate `docker-compose.dev-gcp.yml` (not committed; checked in as `.example`) targets a dedicated dev GCP project with real (small) Pub/Sub topics:

```bash
docker compose -f docker-compose.dev-gcp.yml up -d
gcloud pubsub topics publish image-gen-jobs-dev \
    --message="$(cat tests/fixtures/jobs/sample.json)" \
    --attribute=story_id=smoke_$(uuidgen),schema_version=1 \
    --project=tarostory-dev
docker compose -f docker-compose.dev-gcp.yml logs -f imagegen-worker
```

### 7.2 Post-deploy production smoke

After every prod deploy, oncall runs a single canary job through prod via the same `gcloud pubsub topics publish` command, against the prod topic, with a flag in the job body that the result_processor recognizes and routes to a no-op (no Firestore write, no FCM push). Confirms the new image successfully pulls, runs the model, and publishes a completion end-to-end.

## 8. CI Pipeline

GitHub Actions runs on every PR and on `main`:

| Job | Steps | Target time |
|---|---|---|
| `lint` | `ruff`, `mypy`, schema validation | < 30s |
| `unit` | `pytest tests/unit/ --cov=imagegen` | < 30s |
| `integration` | Service containers for pubsub-emulator and fake-gcs-server; `pytest tests/integration/` | < 3m |
| `e2e` | `make test-e2e` | < 5m |
| `build-image` | `docker buildx build` + push to ghcr.io (only on `main` and tags) | < 4m |
| `scan-image` | `trivy image` against the built image; fail on HIGH or CRITICAL vulns | < 2m |
| `sign-image` | `cosign sign` with OIDC keyless signing (only on tags) | < 30s |

The service-account key needed for production tests is **never** present in CI. The post-deploy smoke test (§7.2) runs on a dedicated worker host, not in CI.

GPU tests are not run in CI (GitHub-hosted runners have no GPUs). Instead, a self-hosted runner labeled `gpu` runs the full e2e suite nightly with a real (small) model loaded; failures page the model team, not infra.

## 9. Coverage Targets

- Unit: 100% line coverage on `imagegen/` (`fail_under = 100`), excluding the
  thin I/O glue that has no logic to unit-test: `main.py` (production wiring) and
  `comfyui_client.py` (real ComfyUI HTTP+WebSocket transport — pinned by the §3
  integration test). `model.py` and `workflow.py` are fully unit-tested via the
  in-process mock ComfyUI (§2.1).
- Integration: every public method on `Puller`, `CompletionPublisher`, and `GcsClient` exercised at least once.
- E2E: every row of the failure modes table (DESIGN §11.5) exercised by at least one named test.

Coverage is enforced in CI: PRs that drop unit coverage below 90% fail.

## 10. Test Data Hygiene

Because the worker is stateless, "test data hygiene" is just "emulator state hygiene":
- Each integration test creates topics and subscriptions with a `_test_<uuid>` suffix and tears them down in teardown.
- E2E tests delete all messages on `image-gen-jobs` and `job-completed` between tests via the emulator's admin API.
- Fake-gcs-server is reset (`docker compose down -v`) once per e2e session, not between tests, to keep the suite fast.

There is no SQLite to reset, no migrations to roll back, no per-test cleanup beyond emulator state.
