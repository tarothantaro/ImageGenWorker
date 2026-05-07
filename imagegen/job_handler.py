"""Per-message handler — parse → download inputs → run model → upload outputs → publish completion.

DESIGN.md §6.2 + §7. The model is injected via Protocol so unit tests can
substitute a deterministic stub. Failure classification routes exceptions
to either a 'failed' completion + ack, or a NACK and Pub/Sub redelivery.
"""

from __future__ import annotations

import json
import logging
from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from .completion_builder import JobResult, build_completed, build_failed
from .failure_classification import (
    Disposition,
    PublishTransientError,
    classify,
)
from .gcs import GcsClient
from .publisher import CompletionPublisher
from .puller import PubsubMessage
from .schema import JobMessage, OutputImage

logger = logging.getLogger(__name__)


@runtime_checkable
class ImageGenModel(Protocol):
    """The model's wire surface. The real model lives in model.py and is
    out of scope for this design (../ImageGenWorker/DESIGN.md §7)."""

    def generate(
        self,
        *,
        template_id: str,
        configurable_options: dict[str, object],
        input_images: list[bytes],
        output_count: int,
    ) -> "ModelResult": ...


class ModelResult(Protocol):
    @property
    def images(self) -> list[bytes]: ...
    @property
    def width(self) -> int: ...
    @property
    def height(self) -> int: ...
    @property
    def model_version(self) -> str: ...
    @property
    def processing_seconds(self) -> float: ...


class JobHandler:
    """Stateless per-message handler. Safe to share across threads."""

    def __init__(
        self,
        *,
        gcs: GcsClient,
        model: ImageGenModel,
        publisher: CompletionPublisher,
    ) -> None:
        self._gcs = gcs
        self._model = model
        self._publisher = publisher

    def handle(self, message: PubsubMessage) -> None:
        """Pub/Sub callback. Must always either ack or nack."""
        try:
            job = self._parse(message)
        except ValidationError as exc:
            # Malformed message — never retryable. Ack so we don't redeliver
            # forever; log loudly so we notice the producer-side bug.
            logger.error(
                "job_message_invalid",
                extra={"message_id": message.message_id, "error": str(exc)},
            )
            message.ack()
            return

        try:
            result = self._run(job)
        except BaseException as exc:  # noqa: BLE001 — we route ALL exceptions
            disposition, reason = classify(exc)
            self._handle_failure(message, job, exc, disposition, reason)
            return

        try:
            completion = build_completed(
                story_id=job.story_id,
                user_id=job.user_id,
                request_id=job.request_id,
                result=result,
            )
            self._publisher.publish(completion)
        except PublishTransientError as exc:
            # Don't ack — Pub/Sub will redeliver; new run, new event_id.
            logger.warning(
                "completion_publish_failed_giving_up_attempt",
                extra={"story_id": job.story_id, "error": str(exc)},
            )
            message.nack()
            return

        message.ack()

    def _parse(self, message: PubsubMessage) -> JobMessage:
        payload = json.loads(message.data.decode("utf-8"))
        return JobMessage.model_validate(payload)

    def _run(self, job: JobMessage) -> JobResult:
        # Download inputs in declared order so the model gets them in the
        # position specified by the job. (Worker must not reorder.)
        ordered = sorted(job.input_photos, key=lambda p: p.position)
        input_bytes = [self._gcs.download(p.gcs_uri) for p in ordered]

        result = self._model.generate(
            template_id=job.template_id,
            configurable_options=dict(job.configurable_options),
            input_images=input_bytes,
            output_count=job.output_count,
        )

        if len(result.images) != job.output_count:
            # Treat as a reported failure — model produced wrong shape.
            from .failure_classification import InvalidConfigError

            raise InvalidConfigError(
                f"model produced {len(result.images)} images, expected {job.output_count}"
            )

        outputs: list[OutputImage] = []
        for idx, img in enumerate(result.images):
            uri = GcsClient.output_uri(job.output_prefix, idx, ext="png")
            self._gcs.upload(uri, img, content_type="image/png")
            outputs.append(
                OutputImage(
                    index=idx,
                    gcs_uri=uri,
                    width=result.width,
                    height=result.height,
                    bytes=len(img),
                )
            )

        return JobResult(
            output_images=outputs,
            model_version=result.model_version,
            processing_seconds=result.processing_seconds,
        )

    def _handle_failure(
        self,
        message: PubsubMessage,
        job: JobMessage,
        exc: BaseException,
        disposition: Disposition,
        reason: str | None,
    ) -> None:
        if disposition is Disposition.NACK_RETRY:
            logger.warning(
                "job_failed_will_retry",
                extra={
                    "story_id": job.story_id,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            message.nack()
            return

        # REPORT_FAILED — publish the failed completion and ack.
        assert reason is not None  # invariant of classify()
        try:
            completion = build_failed(
                story_id=job.story_id,
                user_id=job.user_id,
                request_id=job.request_id,
                failure_reason=reason,
            )
            self._publisher.publish(completion)
        except PublishTransientError as pub_exc:
            logger.warning(
                "failed_completion_publish_failed",
                extra={"story_id": job.story_id, "error": str(pub_exc)},
            )
            message.nack()
            return

        logger.info(
            "job_reported_failed",
            extra={"story_id": job.story_id, "failure_reason": reason},
        )
        message.ack()
