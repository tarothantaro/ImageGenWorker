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
├── unit/                  # pure logic, no I/O
├── integration/           # against pubsub-emulator + fake-gcs-server
├── e2e/                   # docker-compose.dev.yml + assertions
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

Run:
```bash
pytest tests/unit/ -q
```

CI target: < 30s.

## 3. Integration Tests

Located at `tests/integration/`. Each test brings up only the emulator(s) it needs and tears them down.

```
tests/integration/
├── conftest.py            # pytest fixtures: pubsub_emulator, fake_gcs, project_id
├── docker-compose.yml     # used by conftest.py via testcontainers / pytest-docker-tools
├── test_puller.py
├── test_publisher.py
├── test_gcs.py
├── test_handler_e2e_in_proc.py
└── test_exactly_once.py
```

| Test | What it pins down |
|---|---|
| `test_puller.py` | Publish to `image-gen-jobs`, the `Puller` calls `on_message` exactly once, ack→no redelivery. Then nack→redelivery within `retry_policy.minimum_backoff`. Lease-extension path: simulate a 30s processing window with `max_lease_duration=60` and verify the message stays leased. |
| `test_publisher.py` | Publish a completion, subscribe with a fresh test-only subscription, verify the message body and attributes match what was sent. |
| `test_gcs.py` | Round-trip a JPEG through fake-gcs-server: upload → download → bytes match. Object is written under the configured `output_prefix`. |
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
  - Generates `output_count` placeholder PNGs (a colored gradient) and uploads them to `output_prefix` on `STORAGE_EMULATOR_HOST`.
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
copy of the schemas or models in this tree. `run_tests.sh` runs
`pip install -e ../ImageGenContract --quiet` before pytest so the editable
install always tracks the sibling repo's source.

The contract repo's own `tests/test_jsonschema_alignment.py` cross-validates
that the JSON Schemas and the Pydantic models accept and reject the same
payloads — that is what protects us from a class of bugs where the worker
emits a slightly off-spec completion that the API parses leniently in dev
but rejects in prod.

## 7. Manual / Smoke Testing

For changes to GPU / model integration that emulators can't catch:

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

- Unit: > 90% line coverage on `imagegen/` excluding `model.py` (out of scope; covered by model team's own tests).
- Integration: every public method on `Puller`, `CompletionPublisher`, and `GcsClient` exercised at least once.
- E2E: every row of the failure modes table (DESIGN §11.5) exercised by at least one named test.

Coverage is enforced in CI: PRs that drop unit coverage below 90% fail.

## 10. Test Data Hygiene

Because the worker is stateless, "test data hygiene" is just "emulator state hygiene":
- Each integration test creates topics and subscriptions with a `_test_<uuid>` suffix and tears them down in teardown.
- E2E tests delete all messages on `image-gen-jobs` and `job-completed` between tests via the emulator's admin API.
- Fake-gcs-server is reset (`docker compose down -v`) once per e2e session, not between tests, to keep the suite fast.

There is no SQLite to reset, no migrations to roll back, no per-test cleanup beyond emulator state.
