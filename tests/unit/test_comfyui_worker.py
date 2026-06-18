"""End-to-end-in-process tests: a job message flows through the real
JobHandler + real ComfyUIModel + mock ComfyUI container, with the Pub/Sub and
GCS seams faked.

This is the "call the python entry with different job messages and verify the
output/error" suite. It asserts the worker's *observable* behavior — what lands
in GCS, what completion is published, and whether the message is acked or
nacked — for the happy path and for both worker-side and ComfyUI-side failures.
"""

from __future__ import annotations

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
_BUCKET = "bkt"


def _job_payload(*, prompt_type: int = 1, prompt_id: int = 1) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "story_id": "s1",
        "user_id": "u1",
        "request_id": "r1",
        "type": prompt_type,
        "id": prompt_id,
        "input_images": [{"photo_id": "ph_0", "position": 0}],
    }


def _seed_inputs(storage: FakeStorageClient, payload: bytes = PNG) -> None:
    storage.bucket(_BUCKET).blobs["u1_s1_input_0.png"] = FakeBlob(payload)


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
        gcs_bucket=_BUCKET,
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
    # templates/2 has 6 panels; each ComfyUI run saves two variants (V1, V2) →
    # 12 outputs. The prompt set (type=1, id=1) fills the panels' text.
    msg = FakePubsubMessage(_job_payload())
    storage = FakeStorageClient()
    _seed_inputs(storage)
    pub_client = RecordingPublisherClient()
    handler = _build_handler(FakeComfyUI(), storage=storage, pub_client=pub_client)

    handler.handle(msg)

    assert msg.acks == 1 and msg.nacks == 0

    bucket = storage.bucket(_BUCKET)
    outputs = {k for k in bucket.blobs if k.startswith("u1/s1/outputs/")}
    assert outputs == {f"u1/s1/outputs/{i}.png" for i in range(12)}
    assert bucket.blobs["u1/s1/outputs/0.png"].uploaded[0] == (PNG, "image/png")

    # One incremental panel event per image, then the terminal completion.
    published = _all_completions(pub_client)
    assert [c.status for c in published] == ["panel_completed"] * 12 + ["completed"]
    assert published[0].panel_index == 0 and published[0].total_panels == 12

    completion = published[-1]
    assert completion.status == "completed"
    assert completion.model_version == "mv-test"
    assert completion.output_images is not None
    assert {o.gcs_uri for o in completion.output_images} == {
        f"gs://bkt/u1/s1/outputs/{i}.png" for i in range(12)
    }
    assert all(o.width == 1024 and o.height == 736 for o in completion.output_images)


def test_variants_per_panel_tags_outputs_with_panel_and_variant() -> None:
    # templates/2 is the storybook A/B template: 6 panels, each saving V1 + V2 →
    # 12 outputs. The model tags each output panel_index = index // 2, variant
    # = index % 2.
    msg = FakePubsubMessage(_job_payload())
    storage = FakeStorageClient()
    _seed_inputs(storage)
    pub_client = RecordingPublisherClient()
    handler = _build_handler(FakeComfyUI(), storage=storage, pub_client=pub_client)

    handler.handle(msg)

    completion = _last_completion(pub_client)
    assert completion.output_images is not None
    tagged = sorted(completion.output_images, key=lambda o: o.index)
    assert [(o.panel_index, o.variant) for o in tagged] == [
        (panel, variant) for panel in range(6) for variant in range(2)
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
    # No prompts/1_999.json exists → UnsupportedTemplateError (missing asset).
    msg = FakePubsubMessage(_job_payload(prompt_id=999))
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
