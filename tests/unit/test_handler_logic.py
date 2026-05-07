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
from imagegen.gcs import GcsClient
from imagegen.job_handler import JobHandler
from imagegen.publisher import CompletionPublisher
from imagegen.schema import CompletionMessage


# --- fakes --------------------------------------------------------------------


class _FakePubsubMessage:
    def __init__(self, body: dict[str, Any]) -> None:
        self.data = json.dumps(body).encode("utf-8")
        self.attributes: dict[str, str] = {}
        self.message_id = "mid_test"
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
class _StubModelResult:
    images: list[bytes]
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
        template_id: str,
        configurable_options: dict[str, object],
        input_images: list[bytes],
        output_count: int,
    ) -> _StubModelResult:
        self.seen_kwargs = {
            "template_id": template_id,
            "configurable_options": configurable_options,
            "input_images": input_images,
            "output_count": output_count,
        }
        if self.raise_on_generate is not None:
            raise self.raise_on_generate
        return _StubModelResult(images=list(self.images))


class _RecordingPublisherClient:
    def __init__(self, raise_on_publish: BaseException | None = None) -> None:
        self.published: list[bytes] = []
        self._raise = raise_on_publish

    def publish(self, topic: str, data: bytes, **attributes: str) -> Any:  # noqa: ARG002
        self.published.append(data)

        class _Future:
            def result(self, timeout: float | None = None) -> str:
                del timeout
                return "mid_pub"

        if self._raise is not None:
            raise self._raise
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
) -> tuple[JobHandler, _FakeStorageClient]:
    storage = storage_client or _FakeStorageClient()
    handler = JobHandler(
        gcs=GcsClient(storage),
        model=model,
        publisher=CompletionPublisher(
            publisher_client, topic="projects/p/topics/job-completed",
            max_attempts=1,
        ),
    )
    return handler, storage


def _last_completion(client: _RecordingPublisherClient) -> CompletionMessage:
    assert client.published, "no completion was published"
    return CompletionMessage.model_validate_json(client.published[-1])


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
    assert len(model.seen_kwargs["input_images"]) == 2

    # Outputs were uploaded under output_prefix as 0.png, 1.png.
    bucket = storage.bucket("outputs")
    assert sorted(bucket.blobs.keys()) == ["u1/s1/outputs/0.png", "u1/s1/outputs/1.png"]
    for name, blob in bucket.blobs.items():
        del name
        assert blob.uploaded[0][1] == "image/png"

    # A 'completed' completion was published.
    completion = _last_completion(pub_client)
    assert completion.status == "completed"
    assert completion.story_id == "s1"
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


def test_handle_nacks_when_completion_publish_fails_so_job_redelivers() -> None:
    msg = _FakePubsubMessage(_job_payload())
    model = _StubModel()
    pub_client = _RecordingPublisherClient(raise_on_publish=RuntimeError("pubsub down"))
    handler, _storage = _build_handler(model=model, publisher_client=pub_client)

    handler.handle(msg)

    assert msg.nacks == 1 and msg.acks == 0


def test_handle_nacks_when_failed_completion_publish_itself_fails() -> None:
    msg = _FakePubsubMessage(_job_payload())
    model = _StubModel(raise_on_generate=UnsupportedTemplateError("nope"))
    pub_client = _RecordingPublisherClient(raise_on_publish=PublishTransientError("nope"))
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
