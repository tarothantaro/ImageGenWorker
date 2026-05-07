from __future__ import annotations

from datetime import datetime, timezone

from imagegen.completion_builder import JobResult, build_completed, build_failed
from imagegen.schema import OutputImage


def _outputs() -> list[OutputImage]:
    return [
        OutputImage(index=0, gcs_uri="gs://b/o/0.png", width=10, height=10, bytes=100),
        OutputImage(index=1, gcs_uri="gs://b/o/1.png", width=10, height=10, bytes=110),
    ]


def _now() -> datetime:
    return datetime(2026, 5, 5, 12, 35, 24, tzinfo=timezone.utc)


def test_build_completed_populates_every_field() -> None:
    msg = build_completed(
        story_id="s1",
        user_id="u1",
        request_id="r1",
        result=JobResult(output_images=_outputs(), model_version="m-1", processing_seconds=12.5),
        now=_now,
        new_event_id=lambda: "evt_fixed",
    )

    assert msg.event_id == "evt_fixed"
    assert msg.status == "completed"
    assert msg.story_id == "s1"
    assert msg.completed_at == _now()
    assert msg.model_version == "m-1"
    assert msg.processing_seconds == 12.5
    assert msg.output_images is not None
    assert [o.index for o in msg.output_images] == [0, 1]
    assert msg.failure_reason is None


def test_build_failed_populates_every_field() -> None:
    msg = build_failed(
        story_id="s1",
        user_id="u1",
        request_id="r1",
        failure_reason="unsupported_template",
        now=_now,
        new_event_id=lambda: "evt_failed",
    )

    assert msg.event_id == "evt_failed"
    assert msg.status == "failed"
    assert msg.failure_reason == "unsupported_template"
    assert msg.output_images is None
    assert msg.model_version is None
    assert msg.processing_seconds is None


def test_build_completed_uses_default_event_id_factory_each_call() -> None:
    """DESIGN.md §5.2: every retry generates a fresh event_id."""
    msg_a = build_completed(
        story_id="s",
        user_id="u",
        request_id="r",
        result=JobResult(output_images=_outputs(), model_version="m", processing_seconds=1.0),
    )
    msg_b = build_completed(
        story_id="s",
        user_id="u",
        request_id="r",
        result=JobResult(output_images=_outputs(), model_version="m", processing_seconds=1.0),
    )

    assert msg_a.event_id != msg_b.event_id
    assert msg_a.event_id.startswith("evt_")


def test_build_failed_uses_default_event_id_factory_each_call() -> None:
    msg_a = build_failed(
        story_id="s", user_id="u", request_id="r", failure_reason="corrupt_input"
    )
    msg_b = build_failed(
        story_id="s", user_id="u", request_id="r", failure_reason="corrupt_input"
    )

    assert msg_a.event_id != msg_b.event_id


def test_build_completed_uses_default_now_when_not_overridden() -> None:
    before = datetime.now(tz=timezone.utc)
    msg = build_completed(
        story_id="s",
        user_id="u",
        request_id="r",
        result=JobResult(output_images=_outputs(), model_version="m", processing_seconds=1.0),
    )
    after = datetime.now(tz=timezone.utc)

    assert before <= msg.completed_at <= after


def test_build_failed_uses_default_now_when_not_overridden() -> None:
    before = datetime.now(tz=timezone.utc)
    msg = build_failed(story_id="s", user_id="u", request_id="r", failure_reason="invalid_config")
    after = datetime.now(tz=timezone.utc)

    assert before <= msg.completed_at <= after
