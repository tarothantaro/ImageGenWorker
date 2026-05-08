from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from image_gen_contract import CompletionMessage

from imagegen.failure_classification import PublishTransientError
from imagegen.publisher import CompletionPublisher


def _msg() -> CompletionMessage:
    return CompletionMessage.model_validate(
        {
            "schema_version": 1,
            "event_id": "evt_42",
            "story_id": "s1",
            "user_id": "u1",
            "request_id": "r1",
            "status": "failed",
            "completed_at": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
            "failure_reason": "corrupt_input",
        }
    )


class _StaticFuture:
    def __init__(self, message_id: str = "mid_1") -> None:
        self._mid = message_id

    def result(self, timeout: float | None = None) -> str:
        del timeout
        return self._mid


class _RecordingClient:
    """Minimal PubsubPublisherClient that records calls and emits a future."""

    def __init__(
        self, future_or_exc_seq: list[Any] | None = None, future: Any | None = None
    ) -> None:
        self.calls: list[tuple[str, bytes, dict[str, str]]] = []
        if future_or_exc_seq is not None:
            self._seq = list(future_or_exc_seq)
        elif future is not None:
            self._seq = [future]
        else:
            self._seq = [_StaticFuture()]

    def publish(self, topic: str, data: bytes, **attributes: str) -> Any:
        self.calls.append((topic, data, dict(attributes)))
        nxt = self._seq.pop(0) if len(self._seq) > 1 else self._seq[0]
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


def test_publisher_publishes_with_expected_attributes_on_success() -> None:
    client = _RecordingClient()
    pub = CompletionPublisher(client, topic="projects/p/topics/job-completed")

    message_id = pub.publish(_msg())

    assert message_id == "mid_1"
    assert client.calls == [
        (
            "projects/p/topics/job-completed",
            client.calls[0][1],  # body checked below
            {
                "story_id": "s1",
                "schema_version": "1",
                "request_id": "r1",
                "event_id": "evt_42",
                "status": "failed",
            },
        )
    ]
    body = json.loads(client.calls[0][1].decode("utf-8"))
    assert body["story_id"] == "s1"
    assert body["status"] == "failed"
    assert body["failure_reason"] == "corrupt_input"
    assert "output_images" not in body  # exclude_none on serialization


def test_publisher_topic_property_returns_configured_topic() -> None:
    pub = CompletionPublisher(_RecordingClient(), topic="projects/p/topics/x")

    assert pub.topic == "projects/p/topics/x"


def test_publisher_rejects_invalid_max_attempts() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        CompletionPublisher(_RecordingClient(), topic="t", max_attempts=0)


def test_publisher_retries_until_success_and_sleeps_with_backoff() -> None:
    sleeps: list[float] = []
    client = _RecordingClient(future_or_exc_seq=[RuntimeError("flake"), _StaticFuture("mid_2")])
    pub = CompletionPublisher(
        client,
        topic="projects/p/topics/x",
        max_attempts=3,
        backoff_seconds=0.5,
        sleep=sleeps.append,
    )

    message_id = pub.publish(_msg())

    assert message_id == "mid_2"
    assert len(client.calls) == 2
    assert sleeps == [0.5]  # one sleep between the failed attempt and the retry


def test_publisher_raises_publish_transient_error_after_exhausting_retries() -> None:
    sleeps: list[float] = []
    err1 = RuntimeError("flake-1")
    err2 = RuntimeError("flake-2")
    err3 = RuntimeError("flake-3")
    client = _RecordingClient(future_or_exc_seq=[err1, err2, err3])
    pub = CompletionPublisher(
        client,
        topic="t",
        max_attempts=3,
        backoff_seconds=0.25,
        sleep=sleeps.append,
    )

    with pytest.raises(PublishTransientError) as exc_info:
        pub.publish(_msg())

    assert "after 3 attempts" in str(exc_info.value)
    assert exc_info.value.__cause__ is err3
    assert len(client.calls) == 3
    # Backoff doubles: 0.25, 0.5
    assert sleeps == [0.25, 0.5]


def test_publisher_uses_real_time_sleep_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float] = []
    monkeypatch.setattr("time.sleep", seen.append)

    client = _RecordingClient(future_or_exc_seq=[RuntimeError("x"), _StaticFuture()])
    pub = CompletionPublisher(client, topic="t", max_attempts=2, backoff_seconds=0.01)

    pub.publish(_msg())

    assert seen == [0.01]
