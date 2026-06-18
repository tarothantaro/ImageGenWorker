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
    """One generated image, as the model yields it.

    The model owns the storybook layout: ``index`` is the flat ordinal,
    ``panel_index`` / ``variant`` decompose it into page + A/B variant, and
    ``total`` is the total image count for the job (so panel_completed can carry
    ``total_panels`` without the job declaring an ``output_count``).
    """

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
    @property
    def index(self) -> int: ...
    @property
    def panel_index(self) -> int: ...
    @property
    def variant(self) -> int: ...
    @property
    def total(self) -> int: ...


@runtime_checkable
class ImageGenModel(Protocol):
    """The model's wire surface. The production implementation is the ComfyUI
    client in model.py (DESIGN.md §7.2); unit tests substitute a stub.

    ``generate`` returns an iterable of one :class:`PanelResult` per generated
    image — consumed lazily so the handler can publish each as it lands. The
    job's ``type``/``id`` select the prompt set; the render template is fixed.
    """

    def generate(
        self,
        *,
        story_id: str,
        user_id: str,
        prompt_type: int,
        prompt_id: int,
        input_images: list[bytes],
        input_ages: list[str | None],
    ) -> Iterable[PanelResult]: ...


class JobHandler:
    """Stateless per-message handler. Safe to share across threads."""

    def __init__(
        self,
        *,
        gcs: GcsClient,
        gcs_bucket: str,
        model: ImageGenModel,
        publisher: CompletionPublisher,
        max_delivery_attempts: int = 5,
    ) -> None:
        self._gcs = gcs
        self._gcs_bucket = gcs_bucket
        self._model = model
        self._publisher = publisher
        # Mirror the subscription's dead_letter_policy.max_delivery_attempts so
        # we can spot the final delivery and report a terminal failure instead of
        # letting Pub/Sub dead-letter it silently (see ``_handle_failure``).
        self._max_delivery_attempts = max_delivery_attempts

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
        # position specified by the job. (Worker must not reorder.) The objects
        # are at the deterministic per-story name the API wrote them under —
        # gs://<bucket>/<user>_<story>_input_<position>.png (DESIGN.md §5.1).
        ordered = sorted(job.input_images, key=lambda p: p.position)
        input_bytes = [
            self._gcs.download(
                GcsClient.input_uri(
                    self._gcs_bucket, job.user_id, job.story_id, p.position
                )
            )
            for p in ordered
        ]
        # Per-input age strings, same order as input_bytes — fills the prompt
        # set's {INPUT_<n>_AGE} tokens (DESIGN.md §5.1, contract JobInputImage).
        input_ages = [p.age for p in ordered]

        panels = self._model.generate(
            story_id=job.story_id,
            user_id=job.user_id,
            prompt_type=job.type,
            prompt_id=job.id,
            input_images=input_bytes,
            input_ages=input_ages,
        )

        # One ComfyUI run per panel. Upload + publish each image as it lands, then
        # let the model produce the next — the worker never fires all panels at
        # once. The model owns the storybook layout (index / panel_index / variant
        # / total), so the handler just records and relays each image.
        outputs: list[OutputImage] = []
        total_seconds = 0.0
        model_version = ""
        for panel in panels:
            uri = GcsClient.output_uri(
                self._gcs_bucket, job.user_id, job.story_id, panel.index
            )
            self._gcs.upload(uri, panel.image, content_type="image/png")
            output = OutputImage(
                index=panel.index,
                gcs_uri=uri,
                width=panel.width,
                height=panel.height,
                bytes=len(panel.image),
                panel_index=panel.panel_index,
                variant=panel.variant,
            )
            outputs.append(output)
            total_seconds += panel.processing_seconds
            model_version = panel.model_version
            # Incremental, non-terminal: surfaces this image to the user now.
            # A failed publish here raises PublishTransientError → nack/redeliver.
            self._publisher.publish(
                build_panel_completed(
                    story_id=job.story_id,
                    user_id=job.user_id,
                    request_id=job.request_id,
                    panel_index=panel.index,
                    total_panels=panel.total,
                    output_image=output,
                    model_version=panel.model_version,
                    processing_seconds=panel.processing_seconds,
                )
            )

        if not outputs:
            # A story always has at least one panel; zero images means a broken
            # template/prompt set — deterministic, so report failed (not nack).
            raise InvalidConfigError("model produced no images")

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
            if self._is_final_attempt(message):
                # Retries are exhausted: nacking now would let Pub/Sub move the
                # job to the DLQ silently, leaving the story stuck non-terminal
                # forever (the client then spins on its GEN-TIMEOUT path). Give
                # the user a terminal 'failed' instead so the API refunds + flips
                # status. The DLQ stays a backstop for true crashes (worker dies
                # before ack/nack), not for handled transient failures.
                logger.warning(
                    "job_failed_retries_exhausted",
                    extra={
                        "story_id": job.story_id,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "delivery_attempt": message.delivery_attempt,
                    },
                )
                self._report_failed(message, job, reason="generation_failed")
                return
            logger.warning(
                "job_failed_will_retry",
                extra={
                    "story_id": job.story_id,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "delivery_attempt": message.delivery_attempt,
                },
            )
            message.nack()
            return

        # REPORT_FAILED — publish the failed completion and ack.
        assert reason is not None  # invariant of classify()
        self._report_failed(message, job, reason=reason)

    def _is_final_attempt(self, message: PubsubMessage) -> bool:
        """True when this is the last delivery before Pub/Sub dead-letters it.

        ``delivery_attempt`` is ``None`` when the subscription has no
        dead_letter_policy — then we never short-circuit (legacy: nack forever).
        """
        attempt = message.delivery_attempt
        return attempt is not None and attempt >= self._max_delivery_attempts

    def _report_failed(
        self, message: PubsubMessage, job: JobMessage, *, reason: str
    ) -> None:
        """Publish a terminal 'failed' completion and ack.

        If the publish itself fails transiently we nack — better to risk a
        redelivery (or, on the final attempt, a DLQ landing) than to ack a job
        the API never heard failed.
        """
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
