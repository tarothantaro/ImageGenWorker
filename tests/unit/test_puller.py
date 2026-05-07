from __future__ import annotations

import threading
import time
from typing import Any, Callable

import pytest

from imagegen.puller import FlowControl, Puller


class _FakeFuture:
    def __init__(self, raise_on_result: BaseException | None = None) -> None:
        self.cancelled = threading.Event()
        self._raise = raise_on_result
        self._done = threading.Event()

    def cancel(self) -> None:
        self.cancelled.set()
        self._done.set()

    def result(self, timeout: float | None = None) -> object:
        self._done.wait(timeout=timeout)
        if self._raise is not None:
            raise self._raise
        return None


class _FakeSubscriberClient:
    def __init__(self, future: _FakeFuture | None = None) -> None:
        self._future = future or _FakeFuture()
        self.subscribe_calls: list[tuple[str, FlowControl]] = []
        self.callback_received: Callable[[Any], None] | None = None

    def subscribe(
        self,
        subscription: str,
        callback: Callable[[Any], None],
        flow_control: FlowControl,
    ) -> _FakeFuture:
        self.subscribe_calls.append((subscription, flow_control))
        self.callback_received = callback
        return self._future


def _start_runforever(puller: Puller) -> threading.Thread:
    t = threading.Thread(target=puller.run_forever, daemon=True)
    t.start()
    return t


def test_run_forever_subscribes_with_configured_inputs_and_returns_on_stop() -> None:
    fake_future = _FakeFuture()
    client = _FakeSubscriberClient(future=fake_future)
    fc = FlowControl(max_messages=2, max_lease_duration=60)
    received: list[object] = []
    puller = Puller(
        client,
        subscription="projects/p/subscriptions/s",
        flow_control=fc,
        on_message=received.append,
    )

    t = _start_runforever(puller)
    # Give the puller thread a moment to enter the wait loop.
    deadline = time.monotonic() + 2.0
    while client.subscribe_calls == [] and time.monotonic() < deadline:
        time.sleep(0.01)

    assert client.subscribe_calls == [("projects/p/subscriptions/s", fc)]
    assert puller.subscription == "projects/p/subscriptions/s"

    puller.stop()
    t.join(timeout=2.0)

    assert not t.is_alive()
    assert fake_future.cancelled.is_set()


def test_run_forever_swallows_drain_exception_from_future_result() -> None:
    fake_future = _FakeFuture(raise_on_result=RuntimeError("draining"))
    client = _FakeSubscriberClient(future=fake_future)
    puller = Puller(
        client,
        subscription="projects/p/subscriptions/s",
        flow_control=FlowControl(1, 30),
        on_message=lambda _m: None,
    )

    t = _start_runforever(puller)
    deadline = time.monotonic() + 2.0
    while client.subscribe_calls == [] and time.monotonic() < deadline:
        time.sleep(0.01)

    puller.stop()
    t.join(timeout=2.0)

    # Despite the drain raising, run_forever() returned cleanly.
    assert not t.is_alive()


def test_callback_passed_to_subscribe_is_the_one_we_supplied() -> None:
    received: list[object] = []
    client = _FakeSubscriberClient()
    puller = Puller(
        client,
        subscription="projects/p/subscriptions/s",
        flow_control=FlowControl(1, 30),
        on_message=received.append,
    )

    t = _start_runforever(puller)
    deadline = time.monotonic() + 2.0
    while client.callback_received is None and time.monotonic() < deadline:
        time.sleep(0.01)

    assert client.callback_received is not None
    sentinel = object()
    client.callback_received(sentinel)  # type: ignore[arg-type]
    assert received == [sentinel]

    puller.stop()
    t.join(timeout=2.0)


def test_flow_control_is_a_simple_value_holder() -> None:
    fc = FlowControl(max_messages=4, max_lease_duration=540)

    assert fc.max_messages == 4
    assert fc.max_lease_duration == 540
    # frozen=True
    with pytest.raises(Exception):
        fc.max_messages = 99  # type: ignore[misc]
