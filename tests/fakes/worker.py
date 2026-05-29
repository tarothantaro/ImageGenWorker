"""Reusable in-memory fakes for the Pub/Sub + GCS seams.

Mirror the inline fakes in ``test_handler_logic.py`` but live here so tests that
wire the *real* :class:`~imagegen.model.ComfyUIModel` into a
:class:`~imagegen.job_handler.JobHandler` can share them.
"""

from __future__ import annotations

import json
from typing import Any


class FakePubsubMessage:
    """A delivered Pub/Sub message that records ack/nack."""

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


class FakeBlob:
    def __init__(self, payload: bytes = b"image-bytes") -> None:
        self.payload = payload
        self.uploaded: list[tuple[bytes, str | None]] = []

    def download_as_bytes(self) -> bytes:
        return self.payload

    def upload_from_string(self, data: bytes, content_type: str | None = None) -> None:
        self.uploaded.append((data, content_type))


class FakeBucket:
    def __init__(self) -> None:
        self.blobs: dict[str, FakeBlob] = {}

    def blob(self, name: str) -> FakeBlob:
        if name not in self.blobs:
            self.blobs[name] = FakeBlob()
        return self.blobs[name]


class FakeStorageClient:
    def __init__(self) -> None:
        self.buckets: dict[str, FakeBucket] = {}

    def bucket(self, name: str) -> FakeBucket:
        if name not in self.buckets:
            self.buckets[name] = FakeBucket()
        return self.buckets[name]


class RecordingPublisherClient:
    """Records published completion bodies; can be told to raise on publish."""

    def __init__(self, raise_on_publish: BaseException | None = None) -> None:
        self.published: list[bytes] = []
        self._raise = raise_on_publish

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
        return _Future()
