"""Classify a job-handler exception into the right Pub/Sub posture.

DESIGN.md §6.2 lays out the decision tree:

  - Transient (network blip, GCS 5xx, model OOM that may pass on retry)
      → NACK; let Pub/Sub redeliver per retry_policy.
  - Reported failure (corrupt input, unsupported template, bad config)
      → publish status='failed' completion, then ACK.
  - Unknown / unclassifiable → NACK to be safe; eventually DLQ.

The point of separating this from `job_handler` is to make the policy
exhaustively unit-testable without touching Pub/Sub or the model.
"""

from __future__ import annotations

import enum


class Disposition(enum.Enum):
    """How the handler should resolve a failed job attempt."""

    NACK_RETRY = "nack_retry"
    """Don't ack; Pub/Sub redelivers up to dead_letter_max_attempts."""

    REPORT_FAILED = "report_failed"
    """Publish status='failed' completion (with failure_reason), then ack."""


class TransientError(Exception):
    """Marker base for errors the worker thinks may pass on retry."""


class GcsTransientError(TransientError):
    """5xx/timeout from GCS — retry is worthwhile."""


class ModelTransientError(TransientError):
    """OOM/CUDA glitch — retry is worthwhile."""


class PublishTransientError(TransientError):
    """job-completed publish failed transiently — caller will not ack."""


class UnsupportedTemplateError(Exception):
    """Template the worker doesn't know how to run; no point retrying."""


class CorruptInputError(Exception):
    """Input image rejected by the model decode/preprocess step."""


class InvalidConfigError(Exception):
    """configurable_options invalid in a way only inference can detect."""


# Map each terminal exception class to the failure_reason code that goes into
# the published completion. Keep these short — they end up in user-visible UI.
REPORTED_FAILURE_REASONS: dict[type[BaseException], str] = {
    UnsupportedTemplateError: "unsupported_template",
    CorruptInputError: "corrupt_input",
    InvalidConfigError: "invalid_config",
}


def classify(exc: BaseException) -> tuple[Disposition, str | None]:
    """Decide what to do about a job-handler exception.

    Returns (disposition, failure_reason). failure_reason is None for
    NACK_RETRY (no completion is published in that case).
    """
    for cls, reason in REPORTED_FAILURE_REASONS.items():
        if isinstance(exc, cls):
            return Disposition.REPORT_FAILED, reason
    if isinstance(exc, TransientError):
        return Disposition.NACK_RETRY, None
    # Unknown — be conservative, let Pub/Sub redeliver and (eventually) DLQ.
    return Disposition.NACK_RETRY, None
