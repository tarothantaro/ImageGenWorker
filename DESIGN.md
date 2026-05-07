# Image Gen Service — Containerized Worker (Cloud Pub/Sub)

> **Companion doc to `../Application/DESIGN.md`.** This file specifies how the image generation service — running as a Docker container on a worker host — integrates with the GCP-hosted API server through Cloud Pub/Sub. `../Application/DESIGN.md` references this file from §2, §6.1, and §8. Test strategy and fixtures are in [TESTING.md](./TESTING.md).

---

## 1. Goals and Constraints

The image gen service is **not** deployed on GCP. It runs as a long-lived Docker container on a worker host (typically with a GPU, sometimes behind a home/CGNAT NAT, possibly with intermittent uptime). We want:

1. **No inbound network surface on the worker host.** The host needs no public IP, port forwarding, reverse tunnel, or static DNS. Outbound HTTPS to Google APIs is the only requirement.
2. **Stateless worker.** The container holds no per-job persistent state — no local database, no on-disk dedup table, no attempt counters. Replacing or scaling out the worker requires zero data migration. All retry, dedup, and recovery logic lives upstream in `../Application` (the API server) and in Pub/Sub itself.
3. **At-least-once delivery in both directions** with explicit dedup at the API side. Job dispatch and completion can both be redelivered. The API result processor is idempotent on `story_id` / `event_id`.
4. **No new long-running GCP service.** Job dispatch and completion delivery both run through Pub/Sub directly — there is no extra Cloud Run service, no Cloud Tasks queue, no bridge endpoint.
5. **One image, two environments.** The same Docker image runs in dev (against emulators via `docker-compose.dev.yml`) and prod (against GCP via `docker-compose.yml`). Only configuration injected at startup differs.

## 2. End-to-End Flow

```
┌────────────────────────────────────────────────────────────────────────────┐
│                              GCP project                                   │
│                                                                            │
│  Client ──HTTP──▶  API Server (Cloud Run, FastAPI)                         │
│                       │                                                    │
│                       │ 1. POST /stories  (single Firestore txn — §6.2):  │
│                       │    HOLD credits (credit_held +=) + create photos   │
│                       │    visible=false + write story doc status=queued.  │
│                       │    Then publish job message.                       │
│                       ▼                                                    │
│                  ┌──────────────────────┐                                  │
│                  │ Pub/Sub topic        │   image-gen-jobs                 │
│                  │                      │   ack deadline 600s              │
│                  │                      │   message retention 7d           │
│                  └──────────┬───────────┘                                  │
│                             │ pull subscription                            │
│                             │ image-gen-jobs-worker-sub                    │
│                             │ enable_exactly_once_delivery=true            │
│                             │                                              │
│  ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─│─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─│
│                             │                                              │
│                             ▼                                              │
└─────────────────────────────┼──────────────────────────────────────────────┘
                              │ outbound HTTPS only (StreamingPull gRPC)
                              │
┌─────────────────────────────┼──────────────────────────────────────────────┐
│  Worker host                │  ┌──────────────────────────────────────┐    │
│  (Docker engine + nvidia)   ▼  │ Docker container: imagegen-worker    │    │
│                  ┌────────────────────────────┐                       │    │
│                  │ image_gen_worker (Python)  │                       │    │
│                  │                            │                       │    │
│                  │ 2. Pull job from Pub/Sub   │                       │    │
│                  │ 3. Download input images   │── GCS get (SA creds)  │    │
│                  │ 4. Run model inference     │                       │    │
│                  │ 5. Upload output images    │── GCS put (SA creds)  │    │
│                  │ 6. Publish completion      │                       │    │
│                  │ 7. Ack the job message     │                       │    │
│                  └──────────┬─────────────────┘                       │    │
│                             │      └────────── stateless; no volumes  │    │
│                             │                  except read-only model │    │
│                             │                  weights + SA secret    │    │
└─────────────────────────────┼─────────────────────────────────────────┴────┘
                              │ outbound HTTPS (Pub/Sub publish)
                              │
┌─────────────────────────────┼──────────────────────────────────────────────┐
│                             ▼                              GCP project     │
│                  ┌──────────────────────┐                                  │
│                  │ Pub/Sub topic        │   job-completed                  │
│                  │                      │   ack deadline 60s               │
│                  └──────────┬───────────┘                                  │
│                             │ push subscription                            │
│                             │ job-completed-push-sub                       │
│                             │ OIDC auth → API server                       │
│                             ▼                                              │
│         API Server: POST /internal/jobs/completed                          │
│         (result_processor.py)                                              │
│                             │                                              │
│           Firestore update + Redis pub + FCM push (../Application/DESIGN.md §6.3)         │
└────────────────────────────────────────────────────────────────────────────┘
```

**Direction of every TCP connection on the worker is outbound.** The worker's firewall accepts no inbound traffic. If the host reboots, loses Wi-Fi, or moves networks, jobs simply queue in the Pub/Sub backlog and resume when the container reconnects.

## 3. Why Pub/Sub on its own (no Cloud Tasks)

Cloud Tasks was considered and rejected. Pub/Sub already covers everything we need:

| Concern | How Pub/Sub handles it |
|---|---|
| Per-user `POST /stories` rate limits | API server middleware (Redis sliding window). Already in `../Application/DESIGN.md` §11.3.4. Unrelated to job transport. |
| Worker concurrency cap | Subscriber-side `FlowControl(max_messages=N)` on StreamingPull. The worker is the bottleneck, so capping pull rate caps the whole pipeline. |
| Retry with exponential backoff on failure | Subscription `retry_policy` (`minimum_backoff` / `maximum_backoff`). Worker nacks (or lets the ack lease expire) → Pub/Sub redelivers. |
| Dead letter on persistent failure | `dead_letter_policy` with `max_delivery_attempts=5` → `image-gen-jobs-dlq`. |
| Backlog metric for ETA | `pubsub.googleapis.com/subscription/num_undelivered_messages` is a direct equivalent of Cloud Tasks' `queue/depth`; the queue service uses it to compute wait time (../Application/DESIGN.md §6.2). |
| Transport to a worker without an inbound IP | StreamingPull is outbound-initiated gRPC. |
| Completion event back to the API server | Push subscription on `job-completed` delivers to API server's `/internal/jobs/completed`. |
| Avoiding duplicate model runs | `enable_exactly_once_delivery=true` on the jobs subscription (§6.1). |
| Idempotency on redelivery | Application-level on the API side — see §6. |

What Cloud Tasks would have added that Pub/Sub doesn't: scheduled dispatch (`scheduleTime`) and deterministic task-name dedup at enqueue time. The current design uses neither. If a future feature needs scheduled retries (e.g., delay a soft-failure retry by 10 minutes), revisit this decision and add Cloud Tasks back as a thin layer in front of `image-gen-jobs`.

## 4. GCP Resources

### 4.1 Pub/Sub — `image-gen-jobs` topic + worker subscription

```
topic:                         image-gen-jobs
  message_retention_duration:  7d
  schema:                      none  (JSON payload validated at app layer)

subscription:                  image-gen-jobs-worker-sub
  type:                        pull
  ack_deadline_seconds:        600    # max; worker also extends via modifyAckDeadline
  message_retention_duration:  7d
  enable_exactly_once_delivery: true  # see §6.1
  retry_policy:
    minimum_backoff:           10s
    maximum_backoff:           600s
  dead_letter_topic:           image-gen-jobs-dlq
  dead_letter_max_attempts:    5
  filter:                      ""     # no filter at launch
```

`ack_deadline_seconds = 600` is the Pub/Sub maximum for a single ack. For jobs that take longer than 10 minutes, the worker calls `modify_ack_deadline` on a 5-minute heartbeat (subscribers in the official Python client do this automatically when `flow_control.max_lease_duration` is set).

DLQ is consumed by a small handler on the API server (subscribed via push to `image-gen-jobs-dlq`) that synthesizes a `failed/undeliverable` completion and runs the same failure path as an application-level failure.

### 4.2 Pub/Sub — `job-completed` topic + push subscription

Already specified in `../Application/DESIGN.md` §2. The publisher is the worker.

```
topic:                         job-completed
subscription:                  job-completed-push-sub
  type:                        push
  push_endpoint:               https://api.<env>.<domain>/internal/jobs/completed
  oidc_token.service_account_email: pubsub-pusher@<project>.iam.gserviceaccount.com
  ack_deadline_seconds:        60
  enable_exactly_once_delivery: false # API side dedupes on event_id; cheap to redeliver
  retry_policy:
    minimum_backoff:           10s
    maximum_backoff:           600s
  dead_letter_topic:           job-completed-dlq
```

### 4.3 IAM — worker service account

A dedicated service account `imagegen-worker@<project>.iam.gserviceaccount.com` with **only**:

| Role | Scope | Why |
|---|---|---|
| `roles/pubsub.subscriber` | `image-gen-jobs-worker-sub` | Pull jobs |
| `roles/pubsub.publisher` | `job-completed` | Publish completions |
| `roles/storage.objectViewer` | input prefix `{user_id}/photos/` | Download input images |
| `roles/storage.objectCreator` | output prefix `{user_id}/{story_id}/outputs/` | Upload generated images |

No `roles/storage.admin`, no Firestore access, no broader Pub/Sub access. Even with a fully compromised worker the blast radius is the input/output GCS prefixes for in-flight jobs. The worker cannot read other users' data, cannot delete inputs, and cannot write to Firestore directly.

A JSON key for this service account is provisioned via `gcloud iam service-accounts keys create` and consumed by the container as a Docker secret (§10.3). It is **never** baked into the image.

## 5. Message Schemas

### 5.1 Job (`image-gen-jobs` topic)

```json
{
  "schema_version": 1,
  "story_id": "01HX...ULID",
  "user_id": "uid_abc...",
  "request_id": "req_...",
  "template_id": "tpl_life_lesson_v3",
  "configurable_options": { "...": "..." },
  "input_photos": [
    {
      "photo_id": "ph_...",
      "position": 0,
      "gcs_uri": "gs://growstory-prod-uploads/uid_abc/photos/ph_xyz.jpg"
    }
  ],
  "output_count": 4,
  "output_prefix": "gs://growstory-prod-outputs/uid_abc/01HX.../outputs/",
  "callback_topic": "projects/<project>/topics/job-completed",
  "enqueued_at": "2026-05-05T12:34:56Z"
}
```

The `request_id` propagates from the originating HTTP request through Pub/Sub → worker → completion → result_processor, so a single trace id covers the whole pipeline (../Application/DESIGN.md §11.2.1).

Pub/Sub message attributes (used for filtering / observability, not the schema):
- `story_id` — same as body, lets DLQ tooling group by story without parsing JSON
- `schema_version` — for forward-compat
- `request_id` — log correlation

### 5.2 Completion (`job-completed` topic)

```json
{
  "schema_version": 1,
  "event_id": "evt_01HX...",
  "story_id": "01HX...ULID",
  "user_id": "uid_abc...",
  "request_id": "req_...",
  "status": "completed",
  "output_images": [
    {
      "index": 0,
      "gcs_uri": "gs://growstory-prod-outputs/uid_abc/01HX.../outputs/0.png",
      "width": 1024,
      "height": 1024,
      "bytes": 873421
    }
  ],
  "model_version": "growstory-img-2026-04",
  "processing_seconds": 27.4,
  "completed_at": "2026-05-05T12:35:24Z"
}
```

For failures the worker emits a completion with `status: "failed"` and `failure_reason: "<short code>"` instead of `output_images`. The result processor flips the story to `failed` and refunds credits.

`event_id` is the dedup key on the API side (`../Application/DESIGN.md` §11.3.5 — webhook dedup, 24h TTL in Redis: `seen:event:{event_id}`). The worker generates a fresh `event_id` (ULID) for **every** completion publish, including retries — this lets the API side detect "same completion redelivered" (cheap dedup) versus "different model run produced a second completion for the same story" (defensive dedup, §6.4).

## 6. Idempotency and Retries

The worker is stateless. It does not track which `story_id`s it has seen, does not keep a dedup database, and does not need disk persistence across restarts. All idempotency lives in two places: (a) Pub/Sub's delivery semantics on the jobs topic, and (b) the API server's result processor on the completion topic.

### 6.1 Why exactly-once delivery is enabled on `image-gen-jobs`

`enable_exactly_once_delivery=true` on `image-gen-jobs-worker-sub`. With a stateless worker, this is the only mechanism preventing duplicate model runs from ack-deadline races and concurrent pulls. The reasoning:

- **Without exactly-once.** Pub/Sub may briefly redeliver a message that's already in flight (e.g., a delayed ack-lease extension or a concurrent StreamingPull race). Two threads — or two worker instances — could both run the model on the same job. Each duplicate run costs GPU-minutes and dollars. The API side would dedupe duplicate completions (§6.4) but the duplicate work is wasted.
- **With exactly-once.** Pub/Sub guarantees at most one outstanding delivery per message at any instant, and the ack/nack protocol is stronger (`ack` returns success/failure deterministically). Duplicate model runs are reduced to genuinely rare events: a worker that crashes after the lease expires but before publishing a completion. Latency cost is a few hundred ms on pull/ack — negligible relative to model inference time.

Because model inference is the dominant cost in this system, exactly-once is the right default.

The completion topic (`job-completed`) does **not** need exactly-once delivery: completion messages are tiny, and the API side already dedupes on `event_id`. We pay for the strong guarantee where the cost lives (model runs), not where it doesn't (HTTP push of a 1 KB JSON).

### 6.2 Worker behavior on retry

The worker has no local state. Its retry posture is dictated entirely by Pub/Sub:

| Situation | Worker action | Consequence |
|---|---|---|
| Transient failure during processing (network blip, GCS 5xx, model OOM that may pass on retry) | Log, **nack** the message (or let lease expire) | Pub/Sub redelivers per `retry_policy` (10s → 600s backoff). Worker re-downloads inputs, re-runs, re-publishes completion with a *new* `event_id`. API side dedupes via story status (§6.4). |
| Persistent application failure (corrupt input, unsupported template, repeated model failures past local retry budget) | Publish a `status: 'failed'` completion with `failure_reason`, then **ack** | API side runs the failure path: refund hold, mark story `failed`, delete invisible photos. No further worker retry. |
| Worker crash mid-run | Lease expires, message is redelivered | Another worker (or same one after restart) runs from scratch. Exactly-once limits this to true crashes, not lease races. |
| Repeated transient failures > `dead_letter_max_attempts` | (none — Pub/Sub moves the message to DLQ) | API server's DLQ handler synthesizes a `failed/undeliverable` completion → failure path. |

The worker never schedules its own retries, never sleeps with backoff, and never tracks attempt counts. Transport-level retry is Pub/Sub's job; application-level dedup and failure handling is the API server's job.

### 6.3 Publish side (API server)

`POST /stories` does the following inside a single Firestore transaction (../Application/DESIGN.md §4.1 Charge-on-success invariant + Photo lifecycle invariant):

1. Verify `(users.credit_balance - users.credit_held) >= templates.required_credits`. Otherwise reject with `402 PAYMENT_REQUIRED`.
2. For each new upload in the request: create one `photos/{photo_id}` doc with `visible=false`, `story_count=1`. (The re-encoded bytes were uploaded to GCS at `{user_id}/photos/{photo_id}.{ext}` *before* the txn opened — §11.3.2.)
3. For each reused `photo_id`: validate `visible=true` and ownership, then increment `photos/{id}.story_count`.
4. **Hold credits**: `users.credit_held += credits_spent`. **Do NOT touch `users.credit_balance`. Do NOT write a `credit_transactions` doc.** The actual balance debit is deferred to result_processor on success (§6.4).
5. Write the `stories/{story_id}` doc with `status='queued'`, `credits_spent = templates.required_credits`, and `input_photos[]` referencing the photo IDs from steps 2–3.

After the transaction commits, the handler publishes the job message to `image-gen-jobs`. If the transaction commits but the publish fails (process crash, transient Pub/Sub error), the story is left in `status='queued'` with no message in flight. A reconciler — `reconcile_queued_stories.py`, run on a schedule — finds stories in `queued` older than 60 seconds with no in-flight Pub/Sub message and republishes them. Held credits remain held throughout reconciliation; they're released only when result_processor finalizes the story (or DLQ flushes it).

Republishing is safe: any duplicate that may result from a race between the original publish and the reconciler is short-circuited by the result processor's two-tier dedup (§6.4).

This is the same risk surface a Cloud Tasks–based design has between transaction commit and `CreateTask` — neither approach gives free atomicity with Firestore. The reconciler is the answer in both worlds.

### 6.4 Completion side (API server)

The result processor uses **two-tier idempotency** (../Application/DESIGN.md §6.4.5):

1. **Event-id dedup (cheap path).** `SET seen:event:{event_id} 1 NX EX 86400` against Redis. On duplicate (e.g., Pub/Sub redelivered the completion message itself before the API side acked), return 200 immediately.
2. **Story-status dedup (defensive path).** Even if the `event_id` is fresh, read `stories/{story_id}.status`. If it is already in a terminal state (`pending_selection`, `completed`, `failed`), the work was already finalized — most likely a duplicate model run produced a *different* completion message for the same story (worker crashed after publishing once, Pub/Sub redelivered the original job, a fresh model run produced a second completion with a new `event_id`). Log `orphaned_completion`, best-effort delete the duplicate's output GCS objects under `output_prefix` so we don't keep two copies, write an `audit_log` row, ack, and return.

Otherwise, open one Firestore transaction matching the completion's `status` field:

**`status: 'completed'` (image gen success):**

- `stories/{story_id}.status`: `queued`/`processing` → `pending_selection`.
- Write each output image into `stories/{story_id}/output_images/{image_id}`.
- For every input photo with `visible=false`: set `visible=true`. (The user's library / reuse picker now shows them.)
- `users.credit_held -= credits_spent`.
- `users.credit_balance -= credits_spent`. **← This is the only point at which the user's owned balance changes for this story.**
- Write **one** `credit_transactions(type='create_story', delta_credits=-credits_spent, balance_after=…, reference_id=story_id)` doc. (The History feed shows this row.)

After commit, publish to Redis `notify:{story_id}` (SSE handler relays `story_ready`), clear queue position, send FCM push.

**`status: 'failed'` (image gen failure, reported by worker):**

- `stories/{story_id}.status`: `queued`/`processing` → `failed`. Set `error_message` from `failure_reason`.
- For every input photo with `visible=false`: **delete** the `photos/{photo_id}` doc and queue its `storage_path` + `thumbnail_path` for deletion. (By construction, `visible=false` implies `story_count=1` and the only referencing story is this failed one — ../Application/DESIGN.md §4.1.)
- For every reused (`visible=true`) input photo: `story_count -= 1`.
- `users.credit_held -= credits_spent`. **`users.credit_balance` is not touched. No `credit_transactions` doc is written.** The user is not charged; History shows nothing for this story.

After commit, run the queued GCS deletes (best-effort; a daily reconciler sweeps any stragglers). Publish to Redis `notify:{story_id}` (SSE → `failed`), send FCM push.

**Same code path is used by the `image-gen-jobs-dlq` push handler** (../Application/DESIGN.md §6.4.3): it synthesizes a `failed` completion with `failure_reason='undeliverable'` and runs the failure transaction above.

Net effect: regardless of how many times Pub/Sub redelivers, regardless of whether the worker crashes mid-run, the user is **charged at most once per story** and any **photo that never made it to a successful completion is deleted** — so the user never sees a phantom upload they didn't get a story for.

## 7. Worker Layout

The worker lives in its own repo. The codebase is intentionally small:

```
imagegen-worker/
├── pyproject.toml
├── imagegen/
│   ├── __init__.py
│   ├── config.py              # env-driven config (pubsub names, GCS prefixes, model dir)
│   ├── main.py                # entrypoint: starts the streaming pull
│   ├── puller.py              # google-cloud-pubsub StreamingPull wrapper, ack lease extension
│   ├── job_handler.py         # download → run model → upload → publish completion
│   ├── model.py               # the generation model (out of scope for this design)
│   ├── gcs.py                 # tiny wrapper over google-cloud-storage
│   ├── publisher.py           # google-cloud-pubsub publisher for job-completed
│   ├── healthz.py             # /healthz + /metrics HTTP server (localhost only)
│   └── observability.py       # structlog setup, Prometheus counters
├── Dockerfile
├── docker-compose.yml         # production
├── docker-compose.dev.yml     # development (emulators)
├── scripts/
│   └── init-emulators.sh      # creates topics/subs in pubsub-emulator
└── tests/                     # see TESTING.md
```

There is no `dedup.py`, no SQLite database, no `/var/lib/imagegen` writeable path. The worker is fully stateless — restart it, replace the host, run two of them — there is nothing to migrate.

`main.py` runs forever:

```python
def main() -> None:
    cfg = load_config()
    setup_logging()
    publisher = CompletionPublisher(cfg)
    handler = JobHandler(cfg, publisher)

    puller = Puller(
        subscription=cfg.jobs_subscription,
        flow_control=pubsub_v1.types.FlowControl(
            max_messages=cfg.max_concurrency,
            max_lease_duration=cfg.max_processing_seconds,
        ),
        on_message=handler.handle,
    )
    puller.run_forever()  # blocks; SIGTERM drains in-flight then exits
```

`max_concurrency` is the only knob that controls how fast the worker drains the queue. Set it to the number of model instances the host can run in parallel.

### 7.1 Dockerfile

```dockerfile
# syntax=docker/dockerfile:1.7
FROM nvidia/cuda:12.4.0-runtime-ubuntu22.04 AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 python3.11-venv python3-pip ca-certificates curl tini \
    && rm -rf /var/lib/apt/lists/*

# non-root user
RUN groupadd -r imagegen && useradd -r -g imagegen -u 1001 imagegen

WORKDIR /app
COPY pyproject.toml ./
RUN python3.11 -m venv /app/.venv \
 && /app/.venv/bin/pip install --no-cache-dir -e .
COPY imagegen/ ./imagegen/

USER imagegen
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# /healthz on 9100/tcp (bound to 127.0.0.1 inside the container)
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS http://127.0.0.1:9100/healthz || exit 1

# tini reaps zombies and forwards signals so SIGTERM reaches the puller
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "imagegen.main"]
```

The image is pushed to a private registry (`ghcr.io/<org>/imagegen-worker:<git-sha>`). Production pulls by digest, never `:latest`.

## 8. Server-Side Pieces

The FastAPI server gains one publisher helper and keeps the existing completion handler. There is no bridge endpoint and no Cloud Tasks integration.

### 8.1 `app/services/queue_service.py`

`enqueue_story_job(story_id, ...)` publishes the job message directly to the `image-gen-jobs` Pub/Sub topic. Called from `POST /stories` immediately after the credit-debit Firestore transaction commits.

```python
async def enqueue_story_job(story: Story) -> None:
    msg = build_job_message(story)              # validated against schema_version 1
    await publisher.publish(JOBS_TOPIC, msg, attributes={
        "story_id": story.id,
        "schema_version": "1",
        "request_id": get_request_id(),
    })
```

If publish raises, the story stays in `status='queued'` and the reconciler (§6.3) republishes within 60 seconds. The handler does **not** roll back the credit hold — the user is in the queue regardless of the transient publish failure.

### 8.2 `app/workers/result_processor.py`

Already in the design (../Application/DESIGN.md §8). The publisher of `job-completed` is the worker; nothing else changes about this handler. The completion schema in §5.2 is what `result_processor` parses.

### 8.3 `app/workers/queued_reconciler.py`

A periodic task (Cloud Scheduler → HTTP target on the API server, every 60s) that scans `stories(status='queued', created_at < now - 60s)` for stories whose Pub/Sub message never landed and republishes them. Bounded scan, naturally idempotent: any duplicate that results is caught by the result processor's two-tier dedup (§6.4).

## 9. Development Environment

The development stack runs entirely under `docker-compose.dev.yml`. No GCP credentials are needed; emulators replace Pub/Sub and GCS.

### 9.1 What `docker-compose.dev.yml` brings up

| Service | Image | Purpose |
|---|---|---|
| `pubsub-emulator` | `gcr.io/google.com/cloudsdktool/google-cloud-cli:emulators` | Local Pub/Sub on `:8085`. |
| `fake-gcs-server` | `fsouza/fake-gcs-server` | Local GCS-compatible blob store on `:4443`. |
| `init-emulators` | The same CLI image | One-shot init container that creates topics and subscriptions on startup, then exits. |
| `imagegen-worker` | Locally built (`build:` context) | The worker, mounted with source code for hot iteration. |
| `image-gen-stub-completion-listener` | Built from `tests/stubs/` | Optional. Subscribes to `job-completed` and dumps messages to stdout, for hands-on testing. |

```yaml
# docker-compose.dev.yml (excerpt)
services:
  pubsub-emulator:
    image: gcr.io/google.com/cloudsdktool/google-cloud-cli:emulators
    command: gcloud beta emulators pubsub start --host-port=0.0.0.0:8085
    ports: ["8085:8085"]
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8085"]
      interval: 5s
      retries: 10

  fake-gcs-server:
    image: fsouza/fake-gcs-server
    command: -scheme http -host 0.0.0.0 -port 4443 -public-host fake-gcs-server:4443
    ports: ["4443:4443"]

  init-emulators:
    image: gcr.io/google.com/cloudsdktool/google-cloud-cli:emulators
    depends_on:
      pubsub-emulator:
        condition: service_healthy
    environment:
      PUBSUB_EMULATOR_HOST: pubsub-emulator:8085
    volumes:
      - ./scripts/init-emulators.sh:/init.sh:ro
    entrypoint: ["bash", "/init.sh"]
    restart: "no"

  imagegen-worker:
    build: { context: ., dockerfile: Dockerfile }
    depends_on:
      init-emulators:
        condition: service_completed_successfully
    environment:
      PUBSUB_EMULATOR_HOST: pubsub-emulator:8085
      STORAGE_EMULATOR_HOST: http://fake-gcs-server:4443
      GCP_PROJECT_ID: dev-project
      JOBS_SUBSCRIPTION: projects/dev-project/subscriptions/image-gen-jobs-worker-sub
      COMPLETION_TOPIC: projects/dev-project/topics/job-completed
      MAX_CONCURRENCY: 2
      MAX_PROCESSING_SECONDS: 60
      MODEL_DIR: /app/dev-model
      LOG_LEVEL: debug
    volumes:
      - ./imagegen:/app/imagegen:ro              # hot reload of source
      - ./dev-model:/app/dev-model:ro            # tiny stub model
```

### 9.2 Bringing it up

```bash
docker compose -f docker-compose.dev.yml up --build
```

Topics, subscriptions, and a starter GCS bucket are created by `scripts/init-emulators.sh` before the worker starts. The script is idempotent — re-running compose just no-ops on existing resources.

### 9.3 Iteration loop

- **Code change in `imagegen/`** → `docker compose -f docker-compose.dev.yml restart imagegen-worker`. The source is bind-mounted, so no rebuild is needed for pure-Python changes.
- **Dependency change in `pyproject.toml`** → `docker compose -f docker-compose.dev.yml up --build imagegen-worker`.
- **Logs** → `docker compose -f docker-compose.dev.yml logs -f imagegen-worker`.
- **Publishing a test job by hand** →
  ```bash
  PUBSUB_EMULATOR_HOST=localhost:8085 gcloud pubsub topics publish image-gen-jobs \
      --message="$(cat tests/fixtures/jobs/sample.json)" \
      --attribute=story_id=manual_$(uuidgen),schema_version=1 \
      --project=dev-project
  ```

For the Application repo's own e2e suite, this repo also publishes the **stub image** (`ghcr.io/<org>/imagegen-stub`) used by their `docker-compose.yml`. See [TESTING.md §5](./TESTING.md).

## 10. Production Environment

`docker-compose.yml` runs the worker against real GCP. The worker host needs:

- **OS:** Linux. macOS and Windows do not support GPU passthrough into Docker containers.
- **Docker engine** (24.0+ recommended).
- **NVIDIA Container Toolkit** (`nvidia-ctk`) for GPU passthrough.
- **Outbound HTTPS to `*.googleapis.com`** and the container registry. No inbound ports.
- **Disk:** ~50 GB for the image + model weights, plus scratch space for model intermediates.

### 10.1 `docker-compose.yml`

```yaml
services:
  imagegen-worker:
    image: ghcr.io/<org>/imagegen-worker@sha256:<digest>
    restart: always
    stop_grace_period: 600s            # match ack_deadline_seconds; let in-flight drain
    init: false                        # tini is in the image
    secrets:
      - gcp_sa_key
    environment:
      GOOGLE_APPLICATION_CREDENTIALS: /run/secrets/gcp_sa_key
      GCP_PROJECT_ID: growstory-prod
      JOBS_SUBSCRIPTION: projects/growstory-prod/subscriptions/image-gen-jobs-worker-sub
      COMPLETION_TOPIC: projects/growstory-prod/topics/job-completed
      MAX_CONCURRENCY: ${MAX_CONCURRENCY:-4}
      MAX_PROCESSING_SECONDS: 540      # under the 600s ack deadline
      MODEL_DIR: /app/models
      LOG_LEVEL: info
      METRICS_PORT: 9100
    volumes:
      - /opt/imagegen-models:/app/models:ro
    read_only: true                    # rootfs immutable
    tmpfs:
      - /tmp:size=4g                   # scratch for model intermediates
    cap_drop: ["ALL"]
    security_opt:
      - no-new-privileges:true
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
    logging:
      driver: json-file
      options:
        max-size: "100m"
        max-file: "5"

secrets:
  gcp_sa_key:
    file: /etc/imagegen/sa.json        # root-owned 0400 on host
```

### 10.2 Deploy / upgrade

```bash
# 1. Pull the new image by digest (set in .env)
docker compose pull imagegen-worker

# 2. Recreate gracefully — old container drains, new container starts
docker compose up -d imagegen-worker
```

Compose sends SIGTERM to the old container. The puller stops requesting new messages, waits up to `stop_grace_period` for in-flight jobs to publish their completion + ack, then exits. Anything still in flight when the grace period expires has its lease expire and is redelivered to the new container.

### 10.3 Service account key handling

The key is **never** baked into the image and **never** committed to a repo.

- Provisioned via `gcloud iam service-accounts keys create /etc/imagegen/sa.json` on the host.
- File is `root:root 0400`. Docker secrets read it as root and surface it inside the container at `/run/secrets/gcp_sa_key` (mode `0400`, owned by `root` — the worker runs as `imagegen` UID 1001, which can read it via the secret mount).
- Rotated every **90 days**. A `key-age-monitor` cron on the host fires a Slack alert at 75 days and pages oncall at 85 days.
- Rotation procedure:
  ```bash
  # create new key
  gcloud iam service-accounts keys create /tmp/sa-new.json \
      --iam-account=imagegen-worker@<project>.iam.gserviceaccount.com

  # atomically replace
  sudo install -m 0400 -o root -g root /tmp/sa-new.json /etc/imagegen/sa.json.new
  sudo mv /etc/imagegen/sa.json.new /etc/imagegen/sa.json

  # restart container so the secret remounts
  docker compose restart imagegen-worker

  # disable old key
  gcloud iam service-accounts keys delete <OLD_KEY_ID> \
      --iam-account=imagegen-worker@<project>.iam.gserviceaccount.com
  ```

### 10.4 Configuration matrix

| Var | Dev value | Prod value | Notes |
|---|---|---|---|
| `GCP_PROJECT_ID` | `dev-project` | `growstory-prod` | Drives all subscription/topic lookups. |
| `PUBSUB_EMULATOR_HOST` | `pubsub-emulator:8085` | (unset) | Setting this routes the client to the emulator. |
| `STORAGE_EMULATOR_HOST` | `http://fake-gcs-server:4443` | (unset) | Likewise for GCS. |
| `GOOGLE_APPLICATION_CREDENTIALS` | (unset; emulators are unauthenticated) | `/run/secrets/gcp_sa_key` | Always read-only inside the container. |
| `MAX_CONCURRENCY` | 2 | 4 (per GPU) | Caps in-flight model runs. |
| `MAX_PROCESSING_SECONDS` | 60 | 540 | Must stay under the 600s ack deadline. |
| `LOG_LEVEL` | `debug` | `info` | |
| `MODEL_DIR` | `/app/dev-model` (stub) | `/app/models` (mounted from host) | Read-only in both. |

## 11. Operations

### 11.1 Health and metrics

The worker exposes `:9100/healthz` and `:9100/metrics` on `127.0.0.1` inside the container (not published to the host). Compose's `healthcheck` curls `/healthz`. To scrape metrics from outside, run a sidecar Prometheus or expose the port on `127.0.0.1:9100` only via `ports: ["127.0.0.1:9100:9100"]`.

Key metrics:

| Metric | Type | Meaning |
|---|---|---|
| `imagegen_worker_jobs_in_flight` | gauge | Currently-processing jobs. Should be ≤ `MAX_CONCURRENCY`. |
| `imagegen_worker_jobs_processed_total{status}` | counter | `status` ∈ `{completed, failed, nacked}`. |
| `imagegen_worker_processing_seconds` | histogram | End-to-end per-job latency. |
| `imagegen_worker_pubsub_modack_failures_total` | counter | Non-zero means ack lease extension is failing — jobs will redeliver and waste GPU. Page on > 5/min. |
| `imagegen_worker_pubsub_publish_seconds` | histogram | Completion publish latency. |
| `imagegen_worker_gcs_op_seconds{op}` | histogram | `op` ∈ `{download, upload}`. |
| `imagegen_worker_sa_key_age_days` | gauge | Read at startup from the key file's `iat`. Alert > 75. |
| `imagegen_worker_build_info{version,model_version}` | gauge=1 | Identifies the running build for debugging. |

API-side observability is unchanged (../Application/DESIGN.md §11.2).

### 11.2 Alerts

| Alert | Source | Threshold | Page? |
|---|---|---|---|
| Backlog age — `image-gen-jobs-worker-sub.oldest_unacked_message_age` | GCP Monitoring | 30m warn / 4h critical | Critical pages |
| Worker container `unhealthy` | Compose / host monitoring | 2 consecutive failed healthchecks | Yes |
| `pubsub_modack_failures_total` rate | Prometheus | > 5/min | Yes |
| DLQ depth — `image-gen-jobs-dlq.num_undelivered_messages` | GCP Monitoring | > 0 | Yes (every landing) |
| SA key age | Worker metric / cron | > 75d warn / > 85d critical | Yes at critical |
| Worker offline | `up{job="imagegen-worker"} == 0` | > 5m | Yes |

### 11.3 Worker offline behavior

Jobs accumulate on `image-gen-jobs-worker-sub`:

| Backlog state | Outcome |
|---|---|
| Worker offline < 7d | Jobs pile up. ETA shown to users grows. When the container reconnects it drains the backlog. |
| Worker offline > 7d | Messages exceed retention and are dropped. They also exceed `dead_letter_max_attempts` and land in `image-gen-jobs-dlq`. The DLQ handler marks affected stories `failed` and refunds credits. |

### 11.4 Scaling out

To run more than one worker (e.g., a second GPU box), provision identical compose files on each host pointing at the same subscription. Pub/Sub StreamingPull load-balances across subscribers automatically. Total throughput is `Σ(MAX_CONCURRENCY across hosts)`; no central knob to coordinate. Exactly-once delivery (§6.1) prevents two workers from running the same job concurrently.

### 11.5 Failure modes

In every row, the user-visible outcome is **either** a successful story (charged exactly once, photo visible) **or** a failed story (no charge, newly-uploaded photo deleted, hold released). There is no in-between state.

| Failure | Detection | Recovery | User-visible outcome |
|---|---|---|---|
| Worker crash mid-job | Pub/Sub ack lease expires, job redelivers | New container picks up; runs from scratch (worker is stateless) | Eventually success or DLQ-driven failure |
| Two competing model runs (rare under exactly-once: crash after partial publish) | API result_processor's story-status dedup (§6.4) | First completion wins; second is discarded; its GCS outputs are deleted | Charged exactly once |
| Publish from `POST /stories` fails | Story stays in `status='queued'` past 60s | `queued_reconciler` republishes | Same as a normal run; ETA inflates briefly |
| GCS upload (worker side) fails | Worker raises before publishing completion | Job redelivers; if persistent, after retry budget the worker publishes a `failed` completion → result_processor failure path | Failure path: no charge, photo deleted |
| Completion publish (worker → Pub/Sub) fails | Worker logs + does NOT ack original job | Pub/Sub redelivers; worker re-runs; new completion attempt | Eventually success or DLQ → failure path |
| `/internal/jobs/completed` (push delivery) returns 5xx | Pub/Sub retries with backoff; after `dead_letter_max_attempts` lands in `job-completed-dlq` | DLQ replay handler (oncall-triggered) re-POSTs to the endpoint; result_processor idempotency makes replay safe | Same as normal run; pages oncall on every DLQ landing |
| Worker offline ≥ 7d | `image-gen-jobs-worker-sub` `oldest_unacked_message_age` alert; messages drop / land in DLQ | DLQ handler synthesizes `failed/undeliverable` completions | Failure path: hold released, no charges |
| Held credits drift from sum of in-flight stories | Daily `reconcile_held_credits.py` (../Application/DESIGN.md §6.4.7) | Recomputes `users.credit_held` from `stories.credits_spent` | Invisible to user |
| Service account key compromised | Out-of-band (e.g., GCP audit log alert) | Disable the compromised key; rotate (§10.3); permissions revoke immediately on key delete | Worker stops; in-flight jobs DLQ → failure path → holds released |

## 12. Security

The worker host is treated as a **semi-trusted compute node**, not a trusted member of the GCP project. The container hardens that posture further.

### 12.1 Network

- **Inbound: zero exposed ports.** Compose binds `:9100` to `127.0.0.1` only. Docker does not publish any other port.
- **Outbound:** restricted to `*.googleapis.com` and the container registry by host firewall (`iptables`/`nftables` egress rules) where supported. The worker has no business contacting any other endpoint.
- **No host networking.** Compose uses the default bridge network. The container does not need access to the host's network namespace.

### 12.2 Container hardening

The compose service definition (§10.1) sets:
- `read_only: true` — root filesystem is immutable. Writes are confined to the `/tmp` tmpfs.
- `cap_drop: ["ALL"]` — no Linux capabilities. (CUDA does not need any.)
- `no-new-privileges:true` — `setuid` binaries can't escalate.
- Runs as non-root user `imagegen` (UID 1001).
- Image is pinned by digest in prod, never `:latest`.

### 12.3 Credentials

- Service account key (§10.3) is the only credential the container holds. It's mounted as a Docker secret and never copied into the image.
- Secret file inside the container is `0400`, readable only by the worker UID via the secret mount semantics.
- Key rotation procedure documented in §10.3 with automated alerting on age.

### 12.4 IAM blast radius

Even with full RCE on the worker container, the attacker gets:
- Read access to **input GCS objects** under the user's photos prefix, but only for stories that have a job in flight (because the worker only knows the GCS URIs in its current message backlog). They cannot list arbitrary user uploads.
- Write access to **output GCS objects** under prefixes the worker has been told about. They can publish garbage outputs, but the API server gates whether `pending_selection` stories actually surface to the user — a malicious worker can produce bad outputs the user will reject, but cannot bypass that step.
- Ability to publish messages to `job-completed`. The push subscription is OIDC-authenticated to the API server, but the *content* of those messages is attacker-controlled. The result processor's two-tier dedup (§6.4) plus story-status state-machine guards prevent forged completions for stories the worker shouldn't touch (the attacker can only act on stories whose jobs they pulled).

The attacker cannot:
- Read or write Firestore.
- Read other users' uploaded photos.
- Read user emails, payment info, or auth tokens.
- Delete inputs or escalate IAM permissions.
- Bind a public listener (no inbound ports allowed by Docker).

### 12.5 Audit

- All completions write a `audit_log` entry on the API side (existing pattern, ../Application/DESIGN.md §11.3). Pub/Sub `messageId` and the `request_id` are recorded.
- Worker logs are JSON, structlog format, with `story_id` / `request_id` / `event_id` on every line. Logs ship to the host's log aggregator (operator's choice — the worker writes to stdout; Docker handles rotation per `logging:` in §10.1).
- GCP audit logs on the worker SA capture every Pub/Sub and GCS operation. Anomaly detection (e.g., ListObjects calls outside the worker's normal pattern) lives in GCP Monitoring.

### 12.6 Supply chain

- Base image (`nvidia/cuda:12.4.0-runtime-ubuntu22.04`) is pinned. Vulnerability scans (`trivy`) run in CI.
- Python deps are pinned in `pyproject.toml` with hashes.
- Image is signed with `cosign` at build time and verified at deploy.
- Build provenance via SLSA / GitHub Actions OIDC.

## 13. Open Questions / Future Work

- **Scheduled retries.** If we add a "retry the same job 10 minutes later" feature (e.g., model GPU temporarily oversubscribed), Pub/Sub has no native delayed delivery. Add Cloud Tasks at that point as a thin layer in front of `image-gen-jobs` (Cloud Task → bridge endpoint → publish), or use a delayed-republish worker.
- **Multiple worker tiers** (e.g., a fast-but-low-quality model and a slow-but-high-quality one). Could be modeled as separate Pub/Sub topics + subscriptions, with the API server selecting tier from `template.required_credits`. Out of scope at launch.
- **Observability ingest from worker.** Right now metrics live only on the worker host. If we want them in Cloud Monitoring, push them via the OpenTelemetry collector running locally → Cloud Monitoring exporter using the same SA. Defer until we have more than one worker.
- **Workload Identity Federation** instead of long-lived SA keys. Requires a workload identity pool keyed to a verifiable credential on the worker host. Removes the 90-day rotation burden but adds setup complexity. Worth it once we run more than two workers.
- **Per-job GPU fencing.** Today, two concurrent jobs on the same GPU share VRAM. If models grow, add a per-job CUDA context with `MIG` or use one container per GPU.
