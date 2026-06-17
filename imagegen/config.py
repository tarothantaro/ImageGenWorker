"""Env-driven worker configuration. See DESIGN.md §10.4."""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(ValueError):
    """Raised when required configuration is missing or invalid."""


_DEFAULT_COMFYUI_URL = "http://host.docker.internal:8188"
_DEFAULT_MODEL_VERSION = "comfyui-flux2"
# How long the worker waits for ComfyUI to finish ONE panel (one /prompt run)
# before treating it as a transient failure. Per-request, not per-job: a job
# with N panels can take up to N × this. DESIGN.md §7.2.
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 180
# Must mirror the jobs subscription's dead_letter_policy.max_delivery_attempts
# (DESIGN.md §4.1 — Pub/Sub's floor is 5). The worker uses it to recognise the
# FINAL delivery: on that attempt a would-be transient nack instead publishes a
# terminal 'failed' completion + acks, so a job that's about to be silently
# dead-lettered still surfaces to the user (see job_handler) rather than leaving
# the story stuck non-terminal forever.
_DEFAULT_MAX_DELIVERY_ATTEMPTS = 5


@dataclass(frozen=True)
class WorkerConfig:
    gcp_project_id: str
    jobs_subscription: str
    completion_topic: str
    # The single GCS bucket the worker reads inputs from and writes outputs to.
    # Inputs:  gs://<bucket>/<user_id>_<story_id>_input_<position>.png
    # Outputs: gs://<bucket>/<user_id>/<story_id>/outputs/<index>.png
    # The job message no longer carries a gcs_uri / output_prefix (DESIGN.md §5.1);
    # the worker derives both from this bucket + the message's ids.
    gcs_bucket: str
    max_concurrency: int
    max_processing_seconds: int
    log_level: str
    metrics_port: int
    pubsub_emulator_host: str | None
    storage_emulator_host: str | None
    # The ComfyUI container the model talks to, and the model id stamped onto
    # completions. Both have sane defaults so existing deployments need no new
    # env vars (DESIGN.md §"Worker Layout").
    comfyui_url: str = _DEFAULT_COMFYUI_URL
    model_version: str = _DEFAULT_MODEL_VERSION
    comfyui_request_timeout_seconds: int = _DEFAULT_REQUEST_TIMEOUT_SECONDS
    max_delivery_attempts: int = _DEFAULT_MAX_DELIVERY_ATTEMPTS

    @property
    def is_emulator(self) -> bool:
        return (
            self.pubsub_emulator_host is not None
            or self.storage_emulator_host is not None
        )


_REQUIRED = ("GCP_PROJECT_ID", "JOBS_SUBSCRIPTION", "COMPLETION_TOPIC", "GCS_BUCKET")


def load_config(env: dict[str, str] | None = None) -> WorkerConfig:
    src = env if env is not None else os.environ
    missing = [k for k in _REQUIRED if not src.get(k)]
    if missing:
        raise ConfigError(f"missing required env vars: {', '.join(missing)}")

    return WorkerConfig(
        gcp_project_id=src["GCP_PROJECT_ID"],
        jobs_subscription=_validate_subscription(src["JOBS_SUBSCRIPTION"]),
        completion_topic=_validate_topic(src["COMPLETION_TOPIC"]),
        gcs_bucket=src["GCS_BUCKET"],
        max_concurrency=_parse_positive_int(
            src.get("MAX_CONCURRENCY", "4"), "MAX_CONCURRENCY"
        ),
        max_processing_seconds=_parse_max_processing_seconds(
            src.get("MAX_PROCESSING_SECONDS", "540")
        ),
        log_level=src.get("LOG_LEVEL", "info").lower(),
        metrics_port=_parse_positive_int(
            src.get("METRICS_PORT", "9100"), "METRICS_PORT"
        ),
        pubsub_emulator_host=src.get("PUBSUB_EMULATOR_HOST") or None,
        storage_emulator_host=src.get("STORAGE_EMULATOR_HOST") or None,
        comfyui_url=src.get("COMFYUI_URL", _DEFAULT_COMFYUI_URL),
        model_version=src.get("MODEL_VERSION", _DEFAULT_MODEL_VERSION),
        comfyui_request_timeout_seconds=_parse_positive_int(
            src.get(
                "COMFYUI_REQUEST_TIMEOUT_SECONDS",
                str(_DEFAULT_REQUEST_TIMEOUT_SECONDS),
            ),
            "COMFYUI_REQUEST_TIMEOUT_SECONDS",
        ),
        max_delivery_attempts=_parse_positive_int(
            src.get("MAX_DELIVERY_ATTEMPTS", str(_DEFAULT_MAX_DELIVERY_ATTEMPTS)),
            "MAX_DELIVERY_ATTEMPTS",
        ),
    )


def _validate_subscription(value: str) -> str:
    parts = value.split("/")
    if len(parts) != 4 or parts[0] != "projects" or parts[2] != "subscriptions":
        raise ConfigError(
            f"JOBS_SUBSCRIPTION must be 'projects/<project>/subscriptions/<name>', got {value!r}"
        )
    return value


def _validate_topic(value: str) -> str:
    parts = value.split("/")
    if len(parts) != 4 or parts[0] != "projects" or parts[2] != "topics":
        raise ConfigError(
            f"COMPLETION_TOPIC must be 'projects/<project>/topics/<name>', got {value!r}"
        )
    return value


def _parse_positive_int(value: str, name: str) -> int:
    try:
        n = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc
    if n <= 0:
        raise ConfigError(f"{name} must be > 0, got {n}")
    return n


def _parse_max_processing_seconds(value: str) -> int:
    n = _parse_positive_int(value, "MAX_PROCESSING_SECONDS")
    # Pub/Sub max ack deadline is 600s; we must publish completion + ack before then.
    if n >= 600:
        raise ConfigError(
            f"MAX_PROCESSING_SECONDS must be < 600 (Pub/Sub ack deadline), got {n}"
        )
    return n
