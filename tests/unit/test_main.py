from __future__ import annotations

from typing import Any

from imagegen.config import WorkerConfig
from imagegen.main import build_runtime
from imagegen.puller import FlowControl, Puller


def _cfg() -> WorkerConfig:
    return WorkerConfig(
        gcp_project_id="p",
        jobs_subscription="projects/p/subscriptions/jobs",
        completion_topic="projects/p/topics/job-completed",
        max_concurrency=3,
        max_processing_seconds=60,
        log_level="info",
        metrics_port=9100,
        pubsub_emulator_host=None,
        storage_emulator_host=None,
    )


class _StubPubsubPublisherClient:
    def publish(
        self, topic: str, data: bytes, **attributes: str
    ) -> Any:  # noqa: ARG002
        raise NotImplementedError


class _StubPubsubSubscriberClient:
    def subscribe(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ARG002
        raise NotImplementedError


class _StubStorageClient:
    def bucket(self, name: str) -> Any:  # noqa: ARG002
        raise NotImplementedError


class _StubModel:
    def generate(self, **kwargs: Any) -> Any:  # noqa: ARG002
        raise NotImplementedError


def test_build_runtime_returns_a_puller_wired_with_flow_control_from_config() -> None:
    cfg = _cfg()
    pub_client = _StubPubsubPublisherClient()
    sub_client = _StubPubsubSubscriberClient()
    storage_client = _StubStorageClient()
    model = _StubModel()

    puller = build_runtime(
        cfg,
        publisher_client_factory=lambda: pub_client,
        subscriber_client_factory=lambda: sub_client,
        storage_client_factory=lambda: storage_client,
        model_factory=lambda _cfg: model,
    )

    assert isinstance(puller, Puller)
    assert puller.subscription == cfg.jobs_subscription
    assert puller._flow_control == FlowControl(  # type: ignore[attr-defined]
        max_messages=cfg.max_concurrency,
        max_lease_duration=cfg.max_processing_seconds,
    )


def test_build_runtime_calls_each_factory_exactly_once() -> None:
    cfg = _cfg()
    counts = {"pub": 0, "sub": 0, "storage": 0, "model": 0}

    def pub() -> _StubPubsubPublisherClient:
        counts["pub"] += 1
        return _StubPubsubPublisherClient()

    def sub() -> _StubPubsubSubscriberClient:
        counts["sub"] += 1
        return _StubPubsubSubscriberClient()

    def storage() -> _StubStorageClient:
        counts["storage"] += 1
        return _StubStorageClient()

    def model(cfg: WorkerConfig) -> _StubModel:
        del cfg
        counts["model"] += 1
        return _StubModel()

    build_runtime(
        cfg,
        publisher_client_factory=pub,
        subscriber_client_factory=sub,
        storage_client_factory=storage,
        model_factory=model,
    )

    assert counts == {"pub": 1, "sub": 1, "storage": 1, "model": 1}
