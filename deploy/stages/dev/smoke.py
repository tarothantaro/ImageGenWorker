"""Dev end-to-end smoke check for the image-gen worker.

Runs *inside* the worker image, on the dev compose network, so it reaches the
emulators by service name (PUBSUB_EMULATOR_HOST / STORAGE_EMULATOR_HOST, set by
the worker service env). It exercises the exact path a real `POST /stories`
would drive once the API server publishes jobs:

  1. create the GCS bucket + seed one input photo  (stands in for the photo the
     API server re-encodes + uploads before enqueuing — DESIGN.md §6.3 step 2);
  2. publish a JobMessage to `image-gen-jobs`        (stands in for story_publisher);
  3. pull `job-completed-pull-sub` and print every completion the worker emits
     (stands in for the API server's result_processor + SSE relay).

A successful run prints N `panel_completed` events followed by one terminal
`completed` carrying `output_images` under gs://$GCS_BUCKET/<user>/<story>/outputs/.

Env (all provided by the worker service in docker-compose.yml):
    GCP_PROJECT_ID / GOOGLE_CLOUD_PROJECT, PUBSUB_EMULATOR_HOST,
    STORAGE_EMULATOR_HOST, GCS_BUCKET (defaults to tarostory-local-images).
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from google.api_core import exceptions as gax
from google.cloud import pubsub_v1, storage

from image_gen_contract import CURRENT_SCHEMA_VERSION, JobInputImage, JobMessage

# A 1x1 PNG — enough to pass the worker's magic-byte input check (model.py
# `_looks_like_image`); the mock ComfyUI stores the bytes but never decodes them.
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9"
    "awAAAABJRU5ErkJggg=="
)
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _ulid() -> str:
    """26-char Crockford base32 ULID (matches DESIGN.md §5 story_id shape)."""
    value = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    out = []
    for _ in range(26):
        out.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def _seed_input(bucket_name: str, object_name: str) -> str:
    client = storage.Client()
    try:
        client.create_bucket(bucket_name)
        print(f"[smoke] created bucket {bucket_name!r}")
    except gax.Conflict:
        print(f"[smoke] bucket {bucket_name!r} already exists")
    bucket = client.bucket(bucket_name)
    bucket.blob(object_name).upload_from_string(_PNG_1X1, content_type="image/png")
    uri = f"gs://{bucket_name}/{object_name}"
    print(f"[smoke] seeded input photo {uri}")
    return uri


def _publish_job(project: str, bucket: str) -> str:
    story_id = _ulid()
    user_id = f"uid_smoke_{os.urandom(4).hex()}"
    request_id = f"req_smoke_{os.urandom(4).hex()}"
    photo_id = f"ph_{os.urandom(4).hex()}"

    # The worker downloads inputs by the deterministic per-story name; seed the
    # photo there (stands in for what the API writes — DESIGN.md §5.1). The job
    # carries only the prompt selector (type/id) + lightweight input metadata.
    input_uri = _seed_input(bucket, f"{user_id}_{story_id}_input_0.png")

    job = JobMessage(
        schema_version=CURRENT_SCHEMA_VERSION,
        story_id=story_id,
        user_id=user_id,
        request_id=request_id,
        type=1,  # prompt set prompts/1_1.json, rendered through templates/1
        id=1,
        input_images=[JobInputImage(photo_id=photo_id, position=0)],
    )

    publisher = pubsub_v1.PublisherClient()
    jobs_topic = f"projects/{project}/topics/image-gen-jobs"
    body = job.model_dump_json(exclude_none=True).encode("utf-8")
    msg_id = publisher.publish(
        jobs_topic,
        body,
        story_id=story_id,
        schema_version=str(CURRENT_SCHEMA_VERSION),
        request_id=request_id,
    ).result(timeout=30.0)
    print(f"[smoke] published job story_id={story_id} message_id={msg_id}")
    print(f"[smoke] seeded input {input_uri}")
    print(f"[smoke] outputs → gs://{bucket}/{user_id}/{story_id}/outputs/")
    return story_id


def _await_completion(project: str, story_id: str, timeout_s: float = 60.0) -> int:
    sub = f"projects/{project}/subscriptions/job-completed-pull-sub"
    subscriber = pubsub_v1.SubscriberClient()
    print(f"[smoke] waiting on {sub} for story_id={story_id} (≤{timeout_s:.0f}s)...")
    deadline = time.monotonic() + timeout_s
    saw_terminal = False
    rc = 1
    while time.monotonic() < deadline:
        resp = subscriber.pull(
            subscription=sub, max_messages=10, timeout=5, return_immediately=False
        )
        if not resp.received_messages:
            continue
        ack_ids = []
        for rm in resp.received_messages:
            ack_ids.append(rm.ack_id)
            payload = json.loads(rm.message.data.decode("utf-8"))
            if payload.get("story_id") != story_id:
                continue  # a stale completion from a prior run — drain it
            status = payload.get("status")
            if status == "panel_completed":
                imgs = payload.get("output_images", [])
                pi = payload.get("panel_index")
                print(f"[smoke]   panel_completed panel_index={pi} images={_uris(imgs)}")
            elif status == "completed":
                imgs = payload.get("output_images", [])
                print(f"[smoke]   completed event_id={payload.get('event_id')} "
                      f"model={payload.get('model_version')} images={_uris(imgs)}")
                saw_terminal = True
                rc = 0
            elif status == "failed":
                print(f"[smoke]   FAILED reason={payload.get('failure_reason')}")
                saw_terminal = True
                rc = 2
        if ack_ids:
            subscriber.acknowledge(subscription=sub, ack_ids=ack_ids)
        if saw_terminal:
            break
    if not saw_terminal:
        print("[smoke] TIMEOUT — no terminal completion. Check the worker logs:")
        print("[smoke]   docker compose -f deploy/stages/dev/docker-compose.yml logs imagegen-worker")
    return rc


def _uris(images: list[dict]) -> list[str]:
    return [i.get("gcs_uri", "?") for i in images]


def main() -> int:
    project = os.environ.get("GCP_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        print("[smoke] GCP_PROJECT_ID / GOOGLE_CLOUD_PROJECT must be set", file=sys.stderr)
        return 2
    bucket = os.environ.get("GCS_BUCKET", "tarostory-local-images")
    print(f"[smoke] project={project} bucket={bucket} "
          f"pubsub={os.environ.get('PUBSUB_EMULATOR_HOST')} "
          f"gcs={os.environ.get('STORAGE_EMULATOR_HOST')}")
    story_id = _publish_job(project, bucket)
    return _await_completion(project, story_id)


if __name__ == "__main__":
    raise SystemExit(main())
