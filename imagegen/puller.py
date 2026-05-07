"""StreamingPull wrapper.

The real puller wraps `google.cloud.pubsub_v1.SubscriberClient.subscribe`,
which calls our callback for each delivered message; the callback ack/nacks
via methods on the message object. We hide that behind a small Protocol so
tests don't depend on the SDK.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class PubsubMessage(Protocol):
    """Subset of google.cloud.pubsub_v1.subscriber.message.Message we use."""

    @property
    def data(self) -> bytes: ...

    @property
    def attributes(self) -> dict[str, str]: ...

    @property
    def message_id(self) -> str: ...

    def ack(self) -> None: ...

    def nack(self) -> None: ...

    def modify_ack_deadline(self, seconds: int) -> None: ...


@dataclass(frozen=True)
class FlowControl:
    max_messages: int
    max_lease_duration: int  # seconds


class _StreamingPullFutureLike(Protocol):
    def result(self, timeout: float | None = ...) -> object: ...

    def cancel(self) -> object: ...


@runtime_checkable
class PubsubSubscriberClient(Protocol):
    """Subset of SubscriberClient.subscribe(...) we use."""

    def subscribe(
        self,
        subscription: str,
        callback: Callable[[PubsubMessage], None],
        flow_control: FlowControl,
    ) -> _StreamingPullFutureLike: ...


class Puller:
    """Wraps SubscriberClient.subscribe(...) and runs forever.

    `run_forever()` blocks until `stop()` is called (e.g. from a SIGTERM
    handler) or the streaming pull future raises. On stop, the future is
    cancelled and we wait for it to drain, so in-flight messages get a
    chance to finish before the process exits.
    """

    def __init__(
        self,
        client: PubsubSubscriberClient,
        *,
        subscription: str,
        flow_control: FlowControl,
        on_message: Callable[[PubsubMessage], None],
    ) -> None:
        self._client = client
        self._subscription = subscription
        self._flow_control = flow_control
        self._on_message = on_message
        self._stop_event = threading.Event()
        self._future: _StreamingPullFutureLike | None = None

    @property
    def subscription(self) -> str:
        return self._subscription

    def run_forever(self) -> None:
        future = self._client.subscribe(
            self._subscription, self._on_message, self._flow_control
        )
        self._future = future
        logger.info("streaming_pull_started", extra={"subscription": self._subscription})
        try:
            # Block until stop() is called. Wake-ups every second let SIGTERM
            # propagate quickly without busy-spinning.
            while not self._stop_event.is_set():
                self._stop_event.wait(timeout=1.0)
        finally:
            future.cancel()
            try:
                future.result(timeout=30.0)
            except Exception as exc:  # noqa: BLE001 — drain errors are expected on cancel
                logger.info("streaming_pull_drained", extra={"error": str(exc)})

    def stop(self) -> None:
        self._stop_event.set()
