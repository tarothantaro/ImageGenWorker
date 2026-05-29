"""End-to-end-in-process tests: a job message flows through the real
JobHandler + real ComfyUIModel + mock ComfyUI container, with the Pub/Sub and
GCS seams faked.

This is the "call the python entry with different job messages and verify the
output/error" suite. It asserts the worker's *observable* behavior — what lands
in GCS, what completion is published, and whether the message is acked or
nacked — for the happy path and for both worker-side and ComfyUI-side failures.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from image_gen_contract import CompletionMessage

from imagegen.gcs import GcsClient
from imagegen.job_handler import JobHandler
from imagegen.model import ComfyUIBadRequest, ComfyUIModel, ComfyUIUnavailable
from imagegen.publisher import CompletionPublisher
from tests.fakes.comfyui import FakeComfyUI, make_png
from tests.fakes.worker import (
    FakeBlob,
    FakePubsubMessage,
    FakeStorageClient,
    RecordingPublisherClient,
)

PNG = make_png(1024, 736)


def _job_payload(*, output_count: int = 2, template_id: str = "3") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "story_id": "s1",
        "user_id": "u1",
        "request_id": "r1",
        "template_id": template_id,
        "configurable_options": {"prompt": "a teacher", "steps": 8, "seed": 7},
        "input_photos": [
            {
                "photo_id": "ph_0",
                "position": 0,
                "gcs_uri": "gs://uploads/u1/photos/ph_0.jpg",
            }
        ],
        "output_count": output_count,
        "output_prefix": "gs://outputs/u1/s1/outputs/",
        "callback_topic": "projects/p/topics/job-completed",
        "enqueued_at": datetime(2026, 5, 5, tzinfo=timezone.utc).isoformat(),
    }


def _seed_inputs(storage: FakeStorageClient, payload: bytes = PNG) -> None:
    storage.bucket("uploads").blobs["u1/photos/ph_0.jpg"] = FakeBlob(payload)


def _build_handler(
    fake_comfyui: FakeComfyUI,
    *,
    storage: FakeStorageClient,
    pub_client: RecordingPublisherClient,
) -> JobHandler:
    model = ComfyUIModel(
        fake_comfyui,
        model_version="mv-test",
        clock=lambda: 0.0,
    )
    return JobHandler(
        gcs=GcsClient(storage),
        model=model,
        publisher=CompletionPublisher(
            pub_client,
            topic="projects/p/topics/job-completed",
            max_attempts=1,
        ),
    )


def _last_completion(client: RecordingPublisherClient) -> CompletionMessage:
    assert client.published, "no completion was published"
    return CompletionMessage.model_validate_json(client.published[-1])


# --- happy path --------------------------------------------------------------


def test_job_produces_outputs_in_gcs_and_completed_completion() -> None:
    msg = FakePubsubMessage(_job_payload(output_count=2))
    storage = FakeStorageClient()
    _seed_inputs(storage)
    pub_client = RecordingPublisherClient()
    handler = _build_handler(FakeComfyUI(), storage=storage, pub_client=pub_client)

    handler.handle(msg)

    assert msg.acks == 1 and msg.nacks == 0

    bucket = storage.bucket("outputs")
    assert sorted(bucket.blobs) == [
        "u1/s1/outputs/0.png",
        "u1/s1/outputs/1.png",
    ]
    for blob in bucket.blobs.values():
        assert blob.uploaded[0] == (PNG, "image/png")

    completion = _last_completion(pub_client)
    assert completion.status == "completed"
    assert completion.model_version == "mv-test"
    assert completion.output_images is not None
    assert {o.gcs_uri for o in completion.output_images} == {
        "gs://outputs/u1/s1/outputs/0.png",
        "gs://outputs/u1/s1/outputs/1.png",
    }
    assert all(o.width == 1024 and o.height == 736 for o in completion.output_images)


# --- ComfyUI-side failures ---------------------------------------------------


def test_transient_comfyui_failure_nacks_without_publishing() -> None:
    msg = FakePubsubMessage(_job_payload())
    storage = FakeStorageClient()
    _seed_inputs(storage)
    pub_client = RecordingPublisherClient()
    fake = FakeComfyUI(fail_queue=ComfyUIUnavailable("container down"))
    handler = _build_handler(fake, storage=storage, pub_client=pub_client)

    handler.handle(msg)

    assert msg.nacks == 1 and msg.acks == 0
    assert pub_client.published == []


def test_ws_execution_error_nacks_without_publishing() -> None:
    msg = FakePubsubMessage(_job_payload())
    storage = FakeStorageClient()
    _seed_inputs(storage)
    pub_client = RecordingPublisherClient()
    fake = FakeComfyUI(execution_error=True)
    handler = _build_handler(fake, storage=storage, pub_client=pub_client)

    handler.handle(msg)

    assert msg.nacks == 1 and msg.acks == 0
    assert pub_client.published == []


def test_bad_prompt_reports_invalid_config_and_acks() -> None:
    msg = FakePubsubMessage(_job_payload())
    storage = FakeStorageClient()
    _seed_inputs(storage)
    pub_client = RecordingPublisherClient()
    fake = FakeComfyUI(fail_queue=ComfyUIBadRequest("bad workflow"))
    handler = _build_handler(fake, storage=storage, pub_client=pub_client)

    handler.handle(msg)

    assert msg.acks == 1 and msg.nacks == 0
    completion = _last_completion(pub_client)
    assert completion.status == "failed"
    assert completion.failure_reason == "invalid_config"


# --- worker-side failures ----------------------------------------------------


def test_unknown_template_reports_unsupported_template_and_acks() -> None:
    msg = FakePubsubMessage(_job_payload(template_id="nope"))
    storage = FakeStorageClient()
    _seed_inputs(storage)
    pub_client = RecordingPublisherClient()
    handler = _build_handler(FakeComfyUI(), storage=storage, pub_client=pub_client)

    handler.handle(msg)

    assert msg.acks == 1 and msg.nacks == 0
    completion = _last_completion(pub_client)
    assert completion.status == "failed"
    assert completion.failure_reason == "unsupported_template"


def test_corrupt_input_reports_corrupt_input_and_acks() -> None:
    msg = FakePubsubMessage(_job_payload())
    storage = FakeStorageClient()
    _seed_inputs(storage, payload=b"this is not an image")
    pub_client = RecordingPublisherClient()
    handler = _build_handler(FakeComfyUI(), storage=storage, pub_client=pub_client)

    handler.handle(msg)

    assert msg.acks == 1 and msg.nacks == 0
    completion = _last_completion(pub_client)
    assert completion.status == "failed"
    assert completion.failure_reason == "corrupt_input"
