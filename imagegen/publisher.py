"""CompletionPublisher — publishes a CompletionMessage to job-completed.

The transport (google-cloud-pubsub `PublisherClient`) is injected via Protocol
so tests can swap in a fake. Production wires the real client in main.py.
"""

from __future__ import annotations

import logging
import time
from typing import Protocol, runtime_checkable

from image_gen_contract import CompletionMessage

from .failure_classification import PublishTransientError

logger = logging.getLogger(__name__)


class _PublishFutureLike(Protocol):
    def result(self, timeout: float | None = ...) -> str: ...


@runtime_checkable
class PubsubPublisherClient(Protocol):
    """Subset of google.cloud.pubsub_v1.PublisherClient we depend on."""

    def publish(
        self,
        topic: str,
        data: bytes,
        **attributes: str,
    ) -> _PublishFutureLike: ...


class CompletionPublisher:
    """Publishes completion messages with bounded in-process retries.

    DESIGN.md §6.4.1 row 3: worker retries the publish up to N times
    in-process; if all retries fail it raises `PublishTransientError`
    so the caller does NOT ack the original job message — Pub/Sub will
    redeliver the job, the worker will run from scratch, and a new
    completion with a new event_id will be published.
    """

    def __init__(
        self,
        client: PubsubPublisherClient,
        topic: str,
        *,
        max_attempts: int = 3,
        backoff_seconds: float = 0.5,
        sleep: object | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._client = client
        self._topic = topic
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self._sleep = sleep if sleep is not None else time.sleep

    @property
    def topic(self) -> str:
        return self._topic

    def publish(self, msg: CompletionMessage) -> str:
        """Publish a completion. Returns the Pub/Sub messageId.

        Raises PublishTransientError after exhausting retries — the caller
        must NOT ack in that case.
        """
        body = msg.model_dump_json(exclude_none=True).encode("utf-8")
        attrs = {
            "story_id": msg.story_id,
            "schema_version": str(msg.schema_version),
            "request_id": msg.request_id,
            "event_id": msg.event_id,
            "status": msg.status,
        }
        last_exc: BaseException | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                fut = self._client.publish(self._topic, body, **attrs)
                return fut.result(timeout=30.0)
            except Exception as exc:  # noqa: BLE001 — Pub/Sub raises many shapes
                last_exc = exc
                logger.warning(
                    "completion_publish_failed",
                    extra={
                        "story_id": msg.story_id,
                        "event_id": msg.event_id,
                        "attempt": attempt,
                        "error": str(exc),
                    },
                )
                if attempt < self._max_attempts:
                    self._sleep(self._backoff_seconds * (2 ** (attempt - 1)))
        raise PublishTransientError(
            f"completion publish failed after {self._max_attempts} attempts: {last_exc}"
        ) from last_exc
