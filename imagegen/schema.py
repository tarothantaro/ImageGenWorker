"""Pydantic models for the wire-format messages on image-gen-jobs and job-completed.

Mirrors schemas/job.json and schemas/completion.json. Both repos parse these
shapes; structural drift between worker and API server is the class of bug
this module exists to make impossible.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CURRENT_SCHEMA_VERSION = 1


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class JobInputPhoto(_StrictModel):
    photo_id: str = Field(min_length=1)
    position: int = Field(ge=0)
    gcs_uri: str

    @field_validator("gcs_uri")
    @classmethod
    def _check_gcs_uri(cls, v: str) -> str:
        if not v.startswith("gs://") or "/" not in v[5:]:
            raise ValueError("gcs_uri must be gs://<bucket>/<object>")
        return v


class JobMessage(_StrictModel):
    schema_version: Literal[1]
    story_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    template_id: str = Field(min_length=1)
    configurable_options: dict[str, object] = Field(default_factory=dict)
    input_photos: list[JobInputPhoto] = Field(min_length=1, max_length=10)
    output_count: int = Field(ge=1, le=16)
    output_prefix: str
    callback_topic: str
    enqueued_at: datetime

    @field_validator("output_prefix")
    @classmethod
    def _check_output_prefix(cls, v: str) -> str:
        if not v.startswith("gs://") or not v.endswith("/"):
            raise ValueError("output_prefix must be gs://<bucket>/<dir>/ (trailing slash)")
        return v

    @field_validator("callback_topic")
    @classmethod
    def _check_callback_topic(cls, v: str) -> str:
        parts = v.split("/")
        if len(parts) != 4 or parts[0] != "projects" or parts[2] != "topics":
            raise ValueError("callback_topic must be projects/<project>/topics/<name>")
        return v


class OutputImage(_StrictModel):
    index: int = Field(ge=0)
    gcs_uri: str
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    bytes: int = Field(ge=1)

    @field_validator("gcs_uri")
    @classmethod
    def _check_gcs_uri(cls, v: str) -> str:
        if not v.startswith("gs://") or "/" not in v[5:]:
            raise ValueError("gcs_uri must be gs://<bucket>/<object>")
        return v


class CompletionMessage(_StrictModel):
    schema_version: Literal[1]
    event_id: str = Field(min_length=1)
    story_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    status: Literal["completed", "failed"]
    output_images: list[OutputImage] | None = None
    model_version: str | None = Field(default=None, min_length=1)
    processing_seconds: float | None = Field(default=None, ge=0)
    completed_at: datetime
    failure_reason: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def _check_status_fields(self) -> "CompletionMessage":
        if self.status == "completed":
            if self.output_images is None:
                raise ValueError("output_images required when status='completed'")
            if self.model_version is None:
                raise ValueError("model_version required when status='completed'")
            if self.processing_seconds is None:
                raise ValueError("processing_seconds required when status='completed'")
            if self.failure_reason is not None:
                raise ValueError("failure_reason must be omitted when status='completed'")
        else:  # status == 'failed'
            if self.failure_reason is None:
                raise ValueError("failure_reason required when status='failed'")
            if self.output_images:
                raise ValueError("output_images must be empty/omitted when status='failed'")
        return self
