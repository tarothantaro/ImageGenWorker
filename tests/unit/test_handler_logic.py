from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest

from imagegen.failure_classification import (
    CorruptInputError,
    GcsTransientError,
    PublishTransientError,
    UnsupportedTemplateError,
)
from image_gen_contract import CompletionMessage

from imagegen.gcs import GcsClient
from imagegen.job_handler import JobHandler
from imagegen.publisher import CompletionPublisher

# --- fakes --------------------------------------------------------------------


class _FakePubsubMessage:
    def __init__(
        self, body: dict[str, Any], *, delivery_attempt: int | None = None
    ) -> None:
        self.data = json.dumps(body).encode("utf-8")
        self.attributes: dict[str, str] = {}
        self.message_id = "mid_test"
        # None mirrors a subscription without a dead_letter_policy.
        self.delivery_attempt = delivery_attempt
        self.acks = 0
        self.nacks = 0

    def ack(self) -> None:
        self.acks += 1

    def nack(self) -> None:
        self.nacks += 1

    def modify_ack_deadline(self, seconds: int) -> None:
        del seconds


class _FakeBlob:
    def __init__(self, payload: bytes = b"image-bytes") -> None:
        self.payload = payload
        self.uploaded: list[tuple[bytes, str | None]] = []

    def download_as_bytes(self) -> bytes:
        return self.payload

    def upload_from_string(self, data: bytes, content_type: str | None = None) -> None:
        self.uploaded.append((data, content_type))


class _FakeBucket:
    def __init__(self) -> None:
        self.blobs: dict[str, _FakeBlob] = {}

    def blob(self, name: str) -> _FakeBlob:
        if name not in self.blobs:
            self.blobs[name] = _FakeBlob()
        return self.blobs[name]


class _FakeStorageClient:
    def __init__(self) -> None:
        self.buckets: dict[str, _FakeBucket] = {}

    def bucket(self, name: str) -> _FakeBucket:
        if name not in self.buckets:
            self.buckets[name] = _FakeBucket()
        return self.buckets[name]


@dataclass
class _StubPanel:
    image: bytes
    width: int = 64
    height: int = 64
    model_version: str = "stub-1"
    processing_seconds: float = 0.1


@dataclass
class _StubModel:
    images: list[bytes] = field(default_factory=lambda: [b"out0", b"out1"])
    raise_on_generate: BaseException | None = None
    seen_kwargs: dict[str, Any] = field(default_factory=dict)

    def generate(
        self,
        *,
        story_id: str,
        user_id: str,
        template_id: str,
        configurable_options: dict[str, object],
        input_images: list[bytes],
    ) -> Any:
        # Mirrors the real model: validate eagerly (raise here), then return an
        # iterator of one panel per output image.
        self.seen_kwargs = {
            "story_id": story_id,
            "user_id": user_id,
            "template_id": template_id,
            "configurable_options": configurable_options,
            "input_images": input_images,
        }
        if self.raise_on_generate is not None:
            raise self.raise_on_generate
        return iter([_StubPanel(image=img) for img in self.images])


class _RecordingPublisherClient:
    def __init__(
        self,
        raise_on_publish: BaseException | None = None,
        raise_on_status: str | None = None,
    ) -> None:
        self.published: list[bytes] = []
        self._raise = raise_on_publish
        self._raise_on_status = raise_on_status

    def publish(
        self, topic: str, data: bytes, **attributes: str
    ) -> Any:  # noqa: ARG002
        self.published.append(data)

        class _Future:
            def result(self, timeout: float | None = None) -> str:
                del timeout
                return "mid_pub"

        if self._raise is not None:
            raise self._raise
        if (
            self._raise_on_status is not None
            and attributes.get("status") == self._raise_on_status
        ):
            raise RuntimeError(f"pubsub down on {self._raise_on_status}")
        return _Future()


# --- helpers ------------------------------------------------------------------


def _job_payload(*, output_count: int = 2) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "story_id": "s1",
        "user_id": "u1",
        "request_id": "r1",
        "template_id": "tpl_v1",
        "configurable_options": {"k": "v"},
        "input_photos": [
            {
                "photo_id": "ph_1",
                "position": 1,
                "gcs_uri": "gs://uploads/u1/photos/ph_1.jpg",
            },
            {
                "photo_id": "ph_0",
                "position": 0,
                "gcs_uri": "gs://uploads/u1/photos/ph_0.jpg",
            },
        ],
        "output_count": output_count,
        "output_prefix": "gs://outputs/u1/s1/outputs/",
        "callback_topic": "projects/p/topics/job-completed",
        "enqueued_at": datetime(2026, 5, 5, tzinfo=timezone.utc).isoformat(),
    }


def _build_handler(
    *,
    model: _StubModel,
    publisher_client: _RecordingPublisherClient,
    storage_client: _FakeStorageClient | None = None,
    max_delivery_attempts: int = 5,
) -> tuple[JobHandler, _FakeStorageClient]:
    storage = storage_client or _FakeStorageClient()
    handler = JobHandler(
        gcs=GcsClient(storage),
        model=model,
        publisher=CompletionPublisher(
            publisher_client,
            topic="projects/p/topics/job-completed",
            max_attempts=1,
        ),
        max_delivery_attempts=max_delivery_attempts,
    )
    return handler, storage


def _last_completion(client: _RecordingPublisherClient) -> CompletionMessage:
    assert client.published, "no completion was published"
    return CompletionMessage.model_validate_json(client.published[-1])


def _all_completions(client: _RecordingPublisherClient) -> list[CompletionMessage]:
    return [CompletionMessage.model_validate_json(b) for b in client.published]


# --- tests --------------------------------------------------------------------


def test_handle_happy_path_uploads_outputs_and_acks() -> None:
    msg = _FakePubsubMessage(_job_payload(output_count=2))
    model = _StubModel(images=[b"out0", b"out1"])
    pub_client = _RecordingPublisherClient()
    handler, storage = _build_handler(model=model, publisher_client=pub_client)

    handler.handle(msg)

    assert msg.acks == 1 and msg.nacks == 0

    # Inputs were downloaded in declared position order (0 before 1).
    assert model.seen_kwargs["template_id"] == "tpl_v1"
    assert model.seen_kwargs["story_id"] == "s1"
    assert model.seen_kwargs["user_id"] == "u1"
    assert len(model.seen_kwargs["input_images"]) == 2

    # Outputs were uploaded under output_prefix as 0.png, 1.png.
    bucket = storage.bucket("outputs")
    assert sorted(bucket.blobs.keys()) == ["u1/s1/outputs/0.png", "u1/s1/outputs/1.png"]
    for name, blob in bucket.blobs.items():
        del name
        assert blob.uploaded[0][1] == "image/png"

    # Published: one panel_completed per image, then a terminal completed.
    published = _all_completions(pub_client)
    assert [c.status for c in published] == [
        "panel_completed",
        "panel_completed",
        "completed",
    ]
    assert [c.panel_index for c in published[:2]] == [0, 1]
    assert all(c.total_panels == 2 for c in published[:2])
    # Each panel_completed carries only its own image.
    assert published[0].output_images is not None
    assert published[0].output_images[0].gcs_uri == "gs://outputs/u1/s1/outputs/0.png"
    assert published[1].output_images[0].gcs_uri == "gs://outputs/u1/s1/outputs/1.png"

    # The terminal completed carries every image.
    completion = published[-1]
    assert completion.status == "completed"
    assert completion.story_id == "s1"
    assert completion.panel_index is None
    assert completion.output_images is not None
    assert {o.gcs_uri for o in completion.output_images} == {
        "gs://outputs/u1/s1/outputs/0.png",
        "gs://outputs/u1/s1/outputs/1.png",
    }


def test_handle_acks_invalid_message_without_running_model() -> None:
    msg = _FakePubsubMessage({"not": "a job"})
    model = _StubModel()
    pub_client = _RecordingPublisherClient()
    handler, _storage = _build_handler(model=model, publisher_client=pub_client)

    handler.handle(msg)

    assert msg.acks == 1
    assert model.seen_kwargs == {}
    assert pub_client.published == []


def test_handle_reports_unsupported_template_as_failed_and_acks() -> None:
    msg = _FakePubsubMessage(_job_payload())
    model = _StubModel(raise_on_generate=UnsupportedTemplateError("nope"))
    pub_client = _RecordingPublisherClient()
    handler, _storage = _build_handler(model=model, publisher_client=pub_client)

    handler.handle(msg)

    assert msg.acks == 1 and msg.nacks == 0
    completion = _last_completion(pub_client)
    assert completion.status == "failed"
    assert completion.failure_reason == "unsupported_template"


def test_handle_reports_corrupt_input_as_failed_and_acks() -> None:
    msg = _FakePubsubMessage(_job_payload())
    model = _StubModel(raise_on_generate=CorruptInputError("bad bytes"))
    pub_client = _RecordingPublisherClient()
    handler, _storage = _build_handler(model=model, publisher_client=pub_client)

    handler.handle(msg)

    assert msg.acks == 1
    completion = _last_completion(pub_client)
    assert completion.status == "failed"
    assert completion.failure_reason == "corrupt_input"


def test_handle_nacks_when_transient_error_lets_pubsub_redeliver() -> None:
    msg = _FakePubsubMessage(_job_payload())
    model = _StubModel(raise_on_generate=GcsTransientError("flake"))
    pub_client = _RecordingPublisherClient()
    handler, _storage = _build_handler(model=model, publisher_client=pub_client)

    handler.handle(msg)

    assert msg.nacks == 1 and msg.acks == 0
    assert pub_client.published == []


def test_handle_nacks_when_unknown_exception_bubbles_up() -> None:
    msg = _FakePubsubMessage(_job_payload())
    model = _StubModel(raise_on_generate=RuntimeError("?"))
    pub_client = _RecordingPublisherClient()
    handler, _storage = _build_handler(model=model, publisher_client=pub_client)

    handler.handle(msg)

    assert msg.nacks == 1 and msg.acks == 0
    assert pub_client.published == []


def test_handle_still_nacks_transient_on_non_final_delivery_attempt() -> None:
    # Attempts before the last still retry: nack, publish nothing.
    msg = _FakePubsubMessage(_job_payload(), delivery_attempt=4)
    model = _StubModel(raise_on_generate=GcsTransientError("flake"))
    pub_client = _RecordingPublisherClient()
    handler, _storage = _build_handler(
        model=model, publisher_client=pub_client, max_delivery_attempts=5
    )

    handler.handle(msg)

    assert msg.nacks == 1 and msg.acks == 0
    assert pub_client.published == []


def test_handle_reports_failed_on_final_attempt_instead_of_dead_lettering() -> None:
    # On the last delivery before Pub/Sub would dead-letter (delivery_attempt ==
    # max), a transient failure must become a terminal 'failed' + ack — so the
    # API flips the story failed and the client shows GEN-FAILED instead of
    # spinning on GEN-TIMEOUT forever.
    msg = _FakePubsubMessage(_job_payload(), delivery_attempt=5)
    model = _StubModel(raise_on_generate=GcsTransientError("still flaking"))
    pub_client = _RecordingPublisherClient()
    handler, _storage = _build_handler(
        model=model, publisher_client=pub_client, max_delivery_attempts=5
    )

    handler.handle(msg)

    assert msg.acks == 1 and msg.nacks == 0
    completion = _last_completion(pub_client)
    assert completion.status == "failed"
    assert completion.failure_reason == "generation_failed"


def test_handle_nacks_transient_when_no_dead_letter_policy() -> None:
    # delivery_attempt is None without a dead_letter_policy → never short-circuit
    # to failed; keep the legacy nack-and-redeliver behavior.
    msg = _FakePubsubMessage(_job_payload(), delivery_attempt=None)
    model = _StubModel(raise_on_generate=GcsTransientError("flake"))
    pub_client = _RecordingPublisherClient()
    handler, _storage = _build_handler(model=model, publisher_client=pub_client)

    handler.handle(msg)

    assert msg.nacks == 1 and msg.acks == 0
    assert pub_client.published == []


def test_handle_nacks_on_final_attempt_when_failed_publish_fails() -> None:
    # Exhausted retries AND the failed-completion publish flakes: we'd rather
    # nack (→ Pub/Sub dead-letters it as a backstop) than ack a job the API
    # never heard failed.
    msg = _FakePubsubMessage(_job_payload(), delivery_attempt=5)
    model = _StubModel(raise_on_generate=GcsTransientError("flake"))
    pub_client = _RecordingPublisherClient(
        raise_on_publish=PublishTransientError("pubsub down")
    )
    handler, _storage = _build_handler(
        model=model, publisher_client=pub_client, max_delivery_attempts=5
    )

    handler.handle(msg)

    assert msg.nacks == 1 and msg.acks == 0


def test_handle_nacks_when_completion_publish_fails_so_job_redelivers() -> None:
    msg = _FakePubsubMessage(_job_payload())
    model = _StubModel()
    pub_client = _RecordingPublisherClient(raise_on_publish=RuntimeError("pubsub down"))
    handler, _storage = _build_handler(model=model, publisher_client=pub_client)

    handler.handle(msg)

    assert msg.nacks == 1 and msg.acks == 0


def test_handle_nacks_when_final_completion_publish_fails() -> None:
    # Panels publish fine, but the terminal 'completed' publish fails → nack,
    # so Pub/Sub redelivers and the whole job re-runs (DESIGN §6.2).
    msg = _FakePubsubMessage(_job_payload(output_count=2))
    model = _StubModel(images=[b"out0", b"out1"])
    pub_client = _RecordingPublisherClient(raise_on_status="completed")
    handler, _storage = _build_handler(model=model, publisher_client=pub_client)

    handler.handle(msg)

    assert msg.nacks == 1 and msg.acks == 0
    statuses = [c.status for c in _all_completions(pub_client)]
    assert statuses == ["panel_completed", "panel_completed", "completed"]


def test_handle_nacks_when_failed_completion_publish_itself_fails() -> None:
    msg = _FakePubsubMessage(_job_payload())
    model = _StubModel(raise_on_generate=UnsupportedTemplateError("nope"))
    pub_client = _RecordingPublisherClient(
        raise_on_publish=PublishTransientError("nope")
    )
    handler, _storage = _build_handler(model=model, publisher_client=pub_client)

    handler.handle(msg)

    assert msg.nacks == 1 and msg.acks == 0


def test_handle_treats_wrong_image_count_as_invalid_config() -> None:
    msg = _FakePubsubMessage(_job_payload(output_count=4))
    model = _StubModel(images=[b"only-one"])
    pub_client = _RecordingPublisherClient()
    handler, _storage = _build_handler(model=model, publisher_client=pub_client)

    handler.handle(msg)

    assert msg.acks == 1
    completion = _last_completion(pub_client)
    assert completion.status == "failed"
    assert completion.failure_reason == "invalid_config"


def test_handle_reports_failed_when_model_overruns_output_count() -> None:
    # Template drift: the worker-side template yields MORE images than the
    # job promised. Deterministic, so it must report failed + ack — never
    # nack — and must stop before publishing a panel_completed whose
    # panel_index would violate the contract's panel_index < total_panels.
    msg = _FakePubsubMessage(_job_payload(output_count=2))
    model = _StubModel(images=[b"out0", b"out1", b"excess"])
    pub_client = _RecordingPublisherClient()
    handler, storage = _build_handler(model=model, publisher_client=pub_client)

    handler.handle(msg)

    assert msg.acks == 1 and msg.nacks == 0

    published = _all_completions(pub_client)
    assert [c.status for c in published] == [
        "panel_completed",
        "panel_completed",
        "failed",
    ]
    assert published[-1].failure_reason == "invalid_config"

    # The excess image was never uploaded.
    bucket = storage.bucket("outputs")
    assert sorted(bucket.blobs.keys()) == ["u1/s1/outputs/0.png", "u1/s1/outputs/1.png"]


def test_handle_passes_input_bytes_in_position_order() -> None:
    msg = _FakePubsubMessage(_job_payload())
    storage = _FakeStorageClient()
    # Make each blob return distinguishable bytes.
    bucket = storage.bucket("uploads")
    bucket.blobs["u1/photos/ph_0.jpg"] = _FakeBlob(b"FIRST")
    bucket.blobs["u1/photos/ph_1.jpg"] = _FakeBlob(b"SECOND")
    model = _StubModel(images=[b"out0", b"out1"])
    pub_client = _RecordingPublisherClient()
    handler, _storage = _build_handler(
        model=model, publisher_client=pub_client, storage_client=storage
    )

    handler.handle(msg)

    assert model.seen_kwargs["input_images"] == [b"FIRST", b"SECOND"]


def test_handle_returns_early_on_invalid_payload_with_correct_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    msg = _FakePubsubMessage({"schema_version": 999})
    model = _StubModel()
    pub_client = _RecordingPublisherClient()
    handler, _storage = _build_handler(model=model, publisher_client=pub_client)

    with caplog.at_level("ERROR"):
        handler.handle(msg)

    assert any("job_message_invalid" in rec.message for rec in caplog.records)
    assert msg.acks == 1
