"""Env-driven worker configuration. See DESIGN.md §10.4."""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(ValueError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class WorkerConfig:
    gcp_project_id: str
    jobs_subscription: str
    completion_topic: str
    max_concurrency: int
    max_processing_seconds: int
    log_level: str
    metrics_port: int
    pubsub_emulator_host: str | None
    storage_emulator_host: str | None

    @property
    def is_emulator(self) -> bool:
        return self.pubsub_emulator_host is not None or self.storage_emulator_host is not None


_REQUIRED = ("GCP_PROJECT_ID", "JOBS_SUBSCRIPTION", "COMPLETION_TOPIC")


def load_config(env: dict[str, str] | None = None) -> WorkerConfig:
    src = env if env is not None else os.environ
    missing = [k for k in _REQUIRED if not src.get(k)]
    if missing:
        raise ConfigError(f"missing required env vars: {', '.join(missing)}")

    return WorkerConfig(
        gcp_project_id=src["GCP_PROJECT_ID"],
        jobs_subscription=_validate_subscription(src["JOBS_SUBSCRIPTION"]),
        completion_topic=_validate_topic(src["COMPLETION_TOPIC"]),
        max_concurrency=_parse_positive_int(src.get("MAX_CONCURRENCY", "4"), "MAX_CONCURRENCY"),
        max_processing_seconds=_parse_max_processing_seconds(
            src.get("MAX_PROCESSING_SECONDS", "540")
        ),
        log_level=src.get("LOG_LEVEL", "info").lower(),
        metrics_port=_parse_positive_int(src.get("METRICS_PORT", "9100"), "METRICS_PORT"),
        pubsub_emulator_host=src.get("PUBSUB_EMULATOR_HOST") or None,
        storage_emulator_host=src.get("STORAGE_EMULATOR_HOST") or None,
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
        raise ConfigError(f"MAX_PROCESSING_SECONDS must be < 600 (Pub/Sub ack deadline), got {n}")
    return n
