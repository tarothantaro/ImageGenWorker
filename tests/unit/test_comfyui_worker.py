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


def _job_payload(*, output_count: int = 1, template_id: str = "3") -> dict[str, Any]:
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


def _all_completions(client: RecordingPublisherClient) -> list[CompletionMessage]:
    return [CompletionMessage.model_validate_json(b) for b in client.published]


# --- happy path --------------------------------------------------------------


def test_job_produces_outputs_in_gcs_and_completed_completion() -> None:
    # Template 3 is one panel that saves two variants (V1, V2) → output_count == 2.
    msg = FakePubsubMessage(_job_payload(output_count=2))
    storage = FakeStorageClient()
    _seed_inputs(storage)
    pub_client = RecordingPublisherClient()
    handler = _build_handler(FakeComfyUI(), storage=storage, pub_client=pub_client)

    handler.handle(msg)

    assert msg.acks == 1 and msg.nacks == 0

    bucket = storage.bucket("outputs")
    assert sorted(bucket.blobs) == ["u1/s1/outputs/0.png", "u1/s1/outputs/1.png"]
    assert bucket.blobs["u1/s1/outputs/0.png"].uploaded[0] == (PNG, "image/png")

    # One incremental panel event per variant, then the terminal completion.
    published = _all_completions(pub_client)
    assert [c.status for c in published] == [
        "panel_completed",
        "panel_completed",
        "completed",
    ]
    assert published[0].panel_index == 0 and published[0].total_panels == 2
    assert published[1].panel_index == 1

    completion = published[-1]
    assert completion.status == "completed"
    assert completion.model_version == "mv-test"
    assert completion.output_images is not None
    assert {o.gcs_uri for o in completion.output_images} == {
        "gs://outputs/u1/s1/outputs/0.png",
        "gs://outputs/u1/s1/outputs/1.png",
    }
    assert all(o.width == 1024 and o.height == 736 for o in completion.output_images)


def test_variants_per_panel_tags_outputs_with_panel_and_variant() -> None:
    # Template 4 is the storybook A/B template: 6 panels, each saving V1 + V2 →
    # 12 outputs. The handler tags each output panel_index = index // V, variant
    # = index % V (V = variants_per_panel).
    payload = _job_payload(output_count=12, template_id="4")
    payload["variants_per_panel"] = 2
    msg = FakePubsubMessage(payload)
    storage = FakeStorageClient()
    _seed_inputs(storage)
    pub_client = RecordingPublisherClient()
    handler = _build_handler(FakeComfyUI(), storage=storage, pub_client=pub_client)

    handler.handle(msg)

    completion = _last_completion(pub_client)
    assert completion.output_images is not None
    tagged = sorted(completion.output_images, key=lambda o: o.index)
    assert [(o.panel_index, o.variant) for o in tagged] == [
        (0, 0),
        (0, 1),
        (1, 0),
        (1, 1),
        (2, 0),
        (2, 1),
        (3, 0),
        (3, 1),
        (4, 0),
        (4, 1),
        (5, 0),
        (5, 1),
    ]


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
