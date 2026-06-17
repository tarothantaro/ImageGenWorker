"""Worker entrypoint. Wires real google-cloud-* clients into the pure-logic core.

Kept thin so the rest of the worker is testable without GCP SDKs installed.
"""

from __future__ import annotations

import logging
import signal
import sys
from typing import Callable

from .config import WorkerConfig, load_config
from .gcs import GcsClient
from .job_handler import ImageGenModel, JobHandler
from .publisher import CompletionPublisher
from .puller import FlowControl, Puller


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _build_model(
    cfg: WorkerConfig,
) -> ImageGenModel:  # pragma: no cover - real model out of scope
    from .model import load_model  # type: ignore[import-not-found]

    return load_model(cfg)


class _SubscriberClientAdapter:  # pragma: no cover - production wiring
    """Adapt the real ``SubscriberClient.subscribe`` to the Puller's Protocol.

    The Puller (puller.py) is deliberately SDK-agnostic: it passes our own
    ``FlowControl`` dataclass positionally. The real client instead expects a
    ``pubsub_v1.types.FlowControl`` (it splats whatever it's given into one), so
    a bare dataclass blows up with ``TypeError``. Convert at this boundary and
    forward ``flow_control`` as the keyword the SDK documents — keeping puller.py
    free of any google-cloud import.
    """

    def __init__(self, client: object) -> None:
        self._client = client

    def subscribe(
        self,
        subscription: str,
        callback: Callable[[object], None],
        flow_control: FlowControl,
    ) -> object:
        from google.cloud import pubsub_v1  # type: ignore[import-not-found]

        fc = pubsub_v1.types.FlowControl(
            max_messages=flow_control.max_messages,
            max_lease_duration=flow_control.max_lease_duration,
        )
        return self._client.subscribe(  # type: ignore[attr-defined]
            subscription, callback=callback, flow_control=fc
        )


def build_runtime(
    cfg: WorkerConfig,
    *,
    publisher_client_factory: Callable[[], object],
    subscriber_client_factory: Callable[[], object],
    storage_client_factory: Callable[[], object],
    model_factory: Callable[[WorkerConfig], ImageGenModel],
) -> Puller:
    """Wire the runtime graph from injected factories.

    Splitting this from `main()` lets us assert the wiring without bringing
    in google-cloud-* at test time.
    """
    publisher = CompletionPublisher(
        client=publisher_client_factory(),  # type: ignore[arg-type]
        topic=cfg.completion_topic,
    )
    gcs = GcsClient(client=storage_client_factory())  # type: ignore[arg-type]
    handler = JobHandler(
        gcs=gcs,
        gcs_bucket=cfg.gcs_bucket,
        model=model_factory(cfg),
        publisher=publisher,
        max_delivery_attempts=cfg.max_delivery_attempts,
    )
    return Puller(
        client=subscriber_client_factory(),  # type: ignore[arg-type]
        subscription=cfg.jobs_subscription,
        flow_control=FlowControl(
            max_messages=cfg.max_concurrency,
            max_lease_duration=cfg.max_processing_seconds,
        ),
        on_message=handler.handle,
    )


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - production wiring
    del argv
    cfg = load_config()
    _setup_logging(cfg.log_level)

    from google.cloud import pubsub_v1  # type: ignore[import-not-found]
    from google.cloud import storage  # type: ignore[import-not-found]

    puller = build_runtime(
        cfg,
        publisher_client_factory=pubsub_v1.PublisherClient,
        subscriber_client_factory=lambda: _SubscriberClientAdapter(
            pubsub_v1.SubscriberClient()
        ),
        storage_client_factory=storage.Client,
        model_factory=_build_model,
    )

    def _on_signal(_signum: int, _frame: object) -> None:
        logging.getLogger(__name__).info("shutdown_signal_received")
        puller.stop()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    puller.run_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
