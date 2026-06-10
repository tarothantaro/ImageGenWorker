"""Per-message handler — parse → download inputs → run model → upload outputs → publish completion.

DESIGN.md §6.2 + §7. The model is injected via Protocol so unit tests can
substitute a deterministic stub. Failure classification routes exceptions
to either a 'failed' completion + ack, or a NACK and Pub/Sub redelivery.

The model yields one panel at a time (DESIGN.md §7.2). For each panel the
handler uploads the image to GCS and publishes a ``panel_completed`` event, so
the API can stream images to the user as they land; only after every panel does
it publish the terminal ``completed`` and ack the job.
"""

from __future__ import annotations

import json
import logging
from typing import Iterable, Protocol, runtime_checkable

from pydantic import ValidationError

from image_gen_contract import JobMessage, OutputImage

from .completion_builder import (
    JobResult,
    build_completed,
    build_failed,
    build_panel_completed,
)
from .failure_classification import (
    Disposition,
    InvalidConfigError,
    PublishTransientError,
    classify,
)
from .gcs import GcsClient
from .publisher import CompletionPublisher
from .puller import PubsubMessage

logger = logging.getLogger(__name__)


class PanelResult(Protocol):
    """One generated panel, as the model yields it."""

    @property
    def image(self) -> bytes: ...
    @property
    def width(self) -> int: ...
    @property
    def height(self) -> int: ...
    @property
    def model_version(self) -> str: ...
    @property
    def processing_seconds(self) -> float: ...


@runtime_checkable
class ImageGenModel(Protocol):
    """The model's wire surface. The production implementation is the ComfyUI
    client in model.py (DESIGN.md §7.2); unit tests substitute a stub.

    ``generate`` returns an iterable of one :class:`PanelResult` per template
    panel — consumed lazily so the handler can publish each panel as it lands.
    """

    def generate(
        self,
        *,
        story_id: str,
        user_id: str,
        template_id: str,
        configurable_options: dict[str, object],
        input_images: list[bytes],
    ) -> Iterable[PanelResult]: ...


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

        panels = self._model.generate(
            story_id=job.story_id,
            user_id=job.user_id,
            template_id=job.template_id,
            configurable_options=dict(job.configurable_options),
            input_images=input_bytes,
        )

        # One ComfyUI run per panel. Upload + publish each as it lands, then let
        # the model produce the next — the worker never fires all panels at once.
        outputs: list[OutputImage] = []
        total_seconds = 0.0
        model_version = ""
        # Storybook A/B layout (contract / DESIGN.md §4): the flat panels are laid
        # out as story-panel = index // variants_per_panel, variant = index %
        # variants_per_panel. Default 1 keeps one image per panel (legacy).
        variants = job.variants_per_panel
        for index, panel in enumerate(panels):
            if index >= job.output_count:
                # The template definition produced more images than the job
                # promised (template drift between API and worker). The panels
                # iterator is lazy, so this is the earliest the overrun is
                # knowable — and it is deterministic: a retry regenerates the
                # exact same overrun, so report failed instead of nacking
                # (publishing the excess would also violate the contract's
                # panel_index < total_panels and retry forever).
                raise InvalidConfigError(
                    f"model produced more than the expected "
                    f"{job.output_count} images"
                )
            uri = GcsClient.output_uri(job.output_prefix, index, ext="png")
            self._gcs.upload(uri, panel.image, content_type="image/png")
            output = OutputImage(
                index=index,
                gcs_uri=uri,
                width=panel.width,
                height=panel.height,
                bytes=len(panel.image),
                panel_index=index // variants,
                variant=index % variants,
            )
            outputs.append(output)
            total_seconds += panel.processing_seconds
            model_version = panel.model_version
            # Incremental, non-terminal: surfaces this panel to the user now.
            # A failed publish here raises PublishTransientError → nack/redeliver.
            self._publisher.publish(
                build_panel_completed(
                    story_id=job.story_id,
                    user_id=job.user_id,
                    request_id=job.request_id,
                    panel_index=index,
                    total_panels=job.output_count,
                    output_image=output,
                    model_version=panel.model_version,
                    processing_seconds=panel.processing_seconds,
                )
            )

        if len(outputs) != job.output_count:
            # Model produced the wrong number of panels for this job.
            raise InvalidConfigError(
                f"model produced {len(outputs)} images, expected {job.output_count}"
            )

        return JobResult(
            output_images=outputs,
            model_version=model_version,
            processing_seconds=total_seconds,
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
