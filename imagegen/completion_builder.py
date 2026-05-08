"""Build CompletionMessage payloads from a finished JobResult.

DESIGN.md §5.2 + §6 — every retry generates a *fresh* event_id. That's the
whole reason this lives in its own module: making event_id-freshness an
unmissable invariant of the call site.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from image_gen_contract import CURRENT_SCHEMA_VERSION, CompletionMessage, OutputImage


@dataclass(frozen=True)
class JobResult:
    """Outcome of one model run, as job_handler hands it off."""

    output_images: list[OutputImage]
    model_version: str
    processing_seconds: float


def _default_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _default_event_id() -> str:
    # ULID-shaped via uuid7 substitute: a uuid4 is fine for dedup; Redis
    # SET NX EX 86400 only needs uniqueness with high probability.
    return f"evt_{uuid.uuid4().hex}"


def build_completed(
    *,
    story_id: str,
    user_id: str,
    request_id: str,
    result: JobResult,
    now: Callable[[], datetime] = _default_now,
    new_event_id: Callable[[], str] = _default_event_id,
) -> CompletionMessage:
    return CompletionMessage(
        schema_version=CURRENT_SCHEMA_VERSION,
        event_id=new_event_id(),
        story_id=story_id,
        user_id=user_id,
        request_id=request_id,
        status="completed",
        output_images=result.output_images,
        model_version=result.model_version,
        processing_seconds=result.processing_seconds,
        completed_at=now(),
    )


def build_failed(
    *,
    story_id: str,
    user_id: str,
    request_id: str,
    failure_reason: str,
    now: Callable[[], datetime] = _default_now,
    new_event_id: Callable[[], str] = _default_event_id,
) -> CompletionMessage:
    return CompletionMessage(
        schema_version=CURRENT_SCHEMA_VERSION,
        event_id=new_event_id(),
        story_id=story_id,
        user_id=user_id,
        request_id=request_id,
        status="failed",
        completed_at=now(),
        failure_reason=failure_reason,
    )
