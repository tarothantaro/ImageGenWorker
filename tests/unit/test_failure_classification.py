from __future__ import annotations

import pytest

from imagegen.failure_classification import (
    CorruptInputError,
    Disposition,
    GcsTransientError,
    InvalidConfigError,
    ModelTransientError,
    PublishTransientError,
    REPORTED_FAILURE_REASONS,
    TransientError,
    UnsupportedTemplateError,
    classify,
)


@pytest.mark.parametrize(
    ("exc", "expected_reason"),
    [
        (UnsupportedTemplateError("nope"), "unsupported_template"),
        (CorruptInputError("bad bytes"), "corrupt_input"),
        (InvalidConfigError("bad config"), "invalid_config"),
    ],
)
def test_reported_failures_are_acked_with_a_reason(
    exc: Exception, expected_reason: str
) -> None:
    disposition, reason = classify(exc)

    assert disposition is Disposition.REPORT_FAILED
    assert reason == expected_reason


@pytest.mark.parametrize(
    "exc",
    [
        GcsTransientError("503"),
        ModelTransientError("CUDA OOM"),
        PublishTransientError("publish flake"),
    ],
)
def test_transient_errors_are_nacked_for_redelivery(exc: Exception) -> None:
    disposition, reason = classify(exc)

    assert disposition is Disposition.NACK_RETRY
    assert reason is None


def test_unknown_exceptions_are_nacked_conservatively() -> None:
    disposition, reason = classify(RuntimeError("unexpected"))

    assert disposition is Disposition.NACK_RETRY
    assert reason is None


def test_transient_marker_subclass_is_recognised() -> None:
    class MyTransient(TransientError):
        pass

    disposition, _ = classify(MyTransient("x"))

    assert disposition is Disposition.NACK_RETRY


def test_reported_reasons_table_is_in_sync_with_class_hierarchy() -> None:
    # Defensive: every entry has a non-empty failure_reason and a sane class.
    for cls, reason in REPORTED_FAILURE_REASONS.items():
        assert isinstance(cls, type)
        assert issubclass(cls, BaseException)
        assert reason and "_" in reason
