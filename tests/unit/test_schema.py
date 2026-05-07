from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from imagegen.schema import (
    CURRENT_SCHEMA_VERSION,
    CompletionMessage,
    JobInputPhoto,
    JobMessage,
    OutputImage,
)


def _valid_job() -> dict[str, object]:
    return {
        "schema_version": 1,
        "story_id": "01HX_story",
        "user_id": "uid_abc",
        "request_id": "req_xyz",
        "template_id": "tpl_v1",
        "configurable_options": {"style": "warm"},
        "input_photos": [
            {
                "photo_id": "ph_1",
                "position": 0,
                "gcs_uri": "gs://growstory-prod-uploads/uid_abc/photos/ph_1.jpg",
            }
        ],
        "output_count": 4,
        "output_prefix": "gs://growstory-prod-outputs/uid_abc/01HX_story/outputs/",
        "callback_topic": "projects/growstory-prod/topics/job-completed",
        "enqueued_at": "2026-05-05T12:34:56Z",
    }


def _valid_completed() -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_id": "evt_1",
        "story_id": "01HX_story",
        "user_id": "uid_abc",
        "request_id": "req_xyz",
        "status": "completed",
        "output_images": [
            {
                "index": 0,
                "gcs_uri": "gs://b/uid_abc/01HX_story/outputs/0.png",
                "width": 1024,
                "height": 1024,
                "bytes": 873421,
            }
        ],
        "model_version": "growstory-img-2026-04",
        "processing_seconds": 27.4,
        "completed_at": "2026-05-05T12:35:24Z",
    }


def _valid_failed() -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_id": "evt_2",
        "story_id": "01HX_story",
        "user_id": "uid_abc",
        "request_id": "req_xyz",
        "status": "failed",
        "completed_at": "2026-05-05T12:35:24Z",
        "failure_reason": "unsupported_template",
    }


# --- JobMessage ----------------------------------------------------------------


def test_job_message_round_trips_canonical_payload() -> None:
    job = JobMessage.model_validate(_valid_job())

    assert job.schema_version == CURRENT_SCHEMA_VERSION
    assert job.story_id == "01HX_story"
    assert job.input_photos[0].photo_id == "ph_1"
    assert isinstance(job.enqueued_at, datetime)


def test_job_message_rejects_unknown_schema_version() -> None:
    payload = _valid_job() | {"schema_version": 99}

    with pytest.raises(ValidationError):
        JobMessage.model_validate(payload)


def test_job_message_rejects_extra_fields() -> None:
    payload = _valid_job() | {"surprise": "extra"}

    with pytest.raises(ValidationError):
        JobMessage.model_validate(payload)


def test_job_message_rejects_missing_required_fields() -> None:
    payload = _valid_job()
    del payload["template_id"]

    with pytest.raises(ValidationError):
        JobMessage.model_validate(payload)


def test_job_message_rejects_zero_input_photos() -> None:
    payload = _valid_job() | {"input_photos": []}

    with pytest.raises(ValidationError):
        JobMessage.model_validate(payload)


def test_job_message_rejects_more_than_ten_input_photos() -> None:
    base = _valid_job()
    photo = base["input_photos"][0]  # type: ignore[index]
    photos = [{**photo, "photo_id": f"ph_{i}", "position": i} for i in range(11)]  # type: ignore[dict-item]
    payload = base | {"input_photos": photos}

    with pytest.raises(ValidationError):
        JobMessage.model_validate(payload)


def test_job_message_rejects_output_count_out_of_range() -> None:
    with pytest.raises(ValidationError):
        JobMessage.model_validate(_valid_job() | {"output_count": 0})
    with pytest.raises(ValidationError):
        JobMessage.model_validate(_valid_job() | {"output_count": 17})


def test_job_message_rejects_negative_position() -> None:
    bad = _valid_job()
    bad["input_photos"] = [{**bad["input_photos"][0], "position": -1}]  # type: ignore[index]

    with pytest.raises(ValidationError):
        JobMessage.model_validate(bad)


def test_job_message_rejects_non_gs_input_uri() -> None:
    bad = _valid_job()
    bad["input_photos"] = [{**bad["input_photos"][0], "gcs_uri": "https://example.com/x"}]  # type: ignore[index]

    with pytest.raises(ValidationError, match="gcs_uri"):
        JobMessage.model_validate(bad)


def test_job_message_rejects_input_uri_without_object_part() -> None:
    bad = _valid_job()
    bad["input_photos"] = [{**bad["input_photos"][0], "gcs_uri": "gs://only-bucket"}]  # type: ignore[index]

    with pytest.raises(ValidationError, match="gcs_uri"):
        JobMessage.model_validate(bad)


def test_job_message_rejects_output_prefix_without_trailing_slash() -> None:
    payload = _valid_job() | {"output_prefix": "gs://b/path/no-slash"}

    with pytest.raises(ValidationError, match="output_prefix"):
        JobMessage.model_validate(payload)


def test_job_message_rejects_output_prefix_not_in_gcs_form() -> None:
    payload = _valid_job() | {"output_prefix": "/local/path/"}

    with pytest.raises(ValidationError, match="output_prefix"):
        JobMessage.model_validate(payload)


def test_job_message_rejects_callback_topic_not_in_canonical_form() -> None:
    payload = _valid_job() | {"callback_topic": "topic-name"}

    with pytest.raises(ValidationError, match="callback_topic"):
        JobMessage.model_validate(payload)


def test_job_input_photo_can_be_constructed_directly() -> None:
    photo = JobInputPhoto(photo_id="ph_1", position=0, gcs_uri="gs://b/o.jpg")
    assert photo.photo_id == "ph_1"


def test_job_message_is_frozen() -> None:
    job = JobMessage.model_validate(_valid_job())
    with pytest.raises(ValidationError):
        job.story_id = "different"  # type: ignore[misc]


# --- CompletionMessage --------------------------------------------------------


def test_completion_message_completed_round_trips() -> None:
    msg = CompletionMessage.model_validate(_valid_completed())

    assert msg.status == "completed"
    assert msg.output_images is not None
    assert msg.output_images[0].gcs_uri.endswith("/0.png")
    assert msg.failure_reason is None


def test_completion_message_failed_round_trips() -> None:
    msg = CompletionMessage.model_validate(_valid_failed())

    assert msg.status == "failed"
    assert msg.failure_reason == "unsupported_template"
    assert msg.output_images is None


def test_completion_message_completed_requires_output_images() -> None:
    bad = _valid_completed()
    del bad["output_images"]

    with pytest.raises(ValidationError, match="output_images"):
        CompletionMessage.model_validate(bad)


def test_completion_message_completed_requires_model_version() -> None:
    bad = _valid_completed()
    del bad["model_version"]

    with pytest.raises(ValidationError, match="model_version"):
        CompletionMessage.model_validate(bad)


def test_completion_message_completed_requires_processing_seconds() -> None:
    bad = _valid_completed()
    del bad["processing_seconds"]

    with pytest.raises(ValidationError, match="processing_seconds"):
        CompletionMessage.model_validate(bad)


def test_completion_message_completed_rejects_failure_reason() -> None:
    bad = _valid_completed() | {"failure_reason": "should not be here"}

    with pytest.raises(ValidationError, match="failure_reason"):
        CompletionMessage.model_validate(bad)


def test_completion_message_failed_requires_failure_reason() -> None:
    bad = _valid_failed()
    del bad["failure_reason"]

    with pytest.raises(ValidationError, match="failure_reason"):
        CompletionMessage.model_validate(bad)


def test_completion_message_failed_rejects_output_images() -> None:
    bad = _valid_failed() | {
        "output_images": [
            {
                "index": 0,
                "gcs_uri": "gs://b/x.png",
                "width": 1,
                "height": 1,
                "bytes": 1,
            }
        ]
    }

    with pytest.raises(ValidationError, match="output_images"):
        CompletionMessage.model_validate(bad)


def test_completion_message_rejects_unknown_status() -> None:
    bad = _valid_completed() | {"status": "weird"}

    with pytest.raises(ValidationError):
        CompletionMessage.model_validate(bad)


def test_output_image_rejects_non_gs_uri() -> None:
    with pytest.raises(ValidationError, match="gcs_uri"):
        OutputImage(index=0, gcs_uri="http://x", width=1, height=1, bytes=1)


def test_output_image_rejects_uri_without_object() -> None:
    with pytest.raises(ValidationError, match="gcs_uri"):
        OutputImage(index=0, gcs_uri="gs://only-bucket", width=1, height=1, bytes=1)


def test_completion_message_rejects_extra_fields() -> None:
    bad = _valid_completed() | {"hidden": True}

    with pytest.raises(ValidationError):
        CompletionMessage.model_validate(bad)


def test_completion_message_completed_at_must_parse_to_datetime() -> None:
    msg = CompletionMessage.model_validate(_valid_completed())
    assert msg.completed_at.tzinfo is not None
    assert msg.completed_at.astimezone(timezone.utc).year == 2026
