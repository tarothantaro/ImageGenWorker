from __future__ import annotations

import os

import pytest

from imagegen.config import ConfigError, WorkerConfig, load_config

_VALID_ENV: dict[str, str] = {
    "GCP_PROJECT_ID": "growstory-prod",
    "JOBS_SUBSCRIPTION": "projects/growstory-prod/subscriptions/image-gen-jobs-worker-sub",
    "COMPLETION_TOPIC": "projects/growstory-prod/topics/job-completed",
}


def test_load_config_with_defaults_yields_expected_dataclass() -> None:
    cfg = load_config(_VALID_ENV.copy())

    assert cfg.gcp_project_id == "growstory-prod"
    assert cfg.jobs_subscription.endswith("/image-gen-jobs-worker-sub")
    assert cfg.completion_topic.endswith("/job-completed")
    assert cfg.max_concurrency == 4
    assert cfg.max_processing_seconds == 540
    assert cfg.log_level == "info"
    assert cfg.metrics_port == 9100
    assert cfg.pubsub_emulator_host is None
    assert cfg.storage_emulator_host is None
    assert cfg.is_emulator is False
    assert cfg.comfyui_url == "http://host.docker.internal:8188"
    assert cfg.model_version == "comfyui-flux2"


def test_load_config_picks_up_emulator_hosts_and_flips_is_emulator() -> None:
    env = _VALID_ENV | {
        "PUBSUB_EMULATOR_HOST": "pubsub-emulator:8085",
        "STORAGE_EMULATOR_HOST": "http://fake-gcs-server:4443",
        "MAX_CONCURRENCY": "2",
        "MAX_PROCESSING_SECONDS": "60",
        "LOG_LEVEL": "DEBUG",
        "METRICS_PORT": "9200",
    }

    cfg = load_config(env)

    assert cfg.pubsub_emulator_host == "pubsub-emulator:8085"
    assert cfg.storage_emulator_host == "http://fake-gcs-server:4443"
    assert cfg.max_concurrency == 2
    assert cfg.max_processing_seconds == 60
    assert cfg.log_level == "debug"
    assert cfg.metrics_port == 9200
    assert cfg.is_emulator is True


def test_load_config_picks_up_comfyui_overrides() -> None:
    env = _VALID_ENV | {
        "COMFYUI_URL": "http://gpu-box:8188",
        "MODEL_VERSION": "flux2-2026-05",
    }

    cfg = load_config(env)

    assert cfg.comfyui_url == "http://gpu-box:8188"
    assert cfg.model_version == "flux2-2026-05"


def test_load_config_uses_real_os_environ_when_no_dict_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for k, v in _VALID_ENV.items():
        monkeypatch.setenv(k, v)
    for k in ("MAX_CONCURRENCY", "PUBSUB_EMULATOR_HOST", "STORAGE_EMULATOR_HOST"):
        monkeypatch.delenv(k, raising=False)

    cfg = load_config()

    assert cfg.gcp_project_id == _VALID_ENV["GCP_PROJECT_ID"]


@pytest.mark.parametrize(
    "missing", ["GCP_PROJECT_ID", "JOBS_SUBSCRIPTION", "COMPLETION_TOPIC"]
)
def test_load_config_rejects_missing_required(missing: str) -> None:
    env = _VALID_ENV.copy()
    env.pop(missing)

    with pytest.raises(ConfigError, match=missing):
        load_config(env)


def test_load_config_treats_empty_string_as_missing() -> None:
    env = _VALID_ENV | {"GCP_PROJECT_ID": ""}

    with pytest.raises(ConfigError, match="GCP_PROJECT_ID"):
        load_config(env)


def test_jobs_subscription_must_be_canonical_form() -> None:
    env = _VALID_ENV | {"JOBS_SUBSCRIPTION": "not-a-real-subscription"}

    with pytest.raises(ConfigError, match="JOBS_SUBSCRIPTION"):
        load_config(env)


def test_completion_topic_must_be_canonical_form() -> None:
    env = _VALID_ENV | {"COMPLETION_TOPIC": "projects/p/subscriptions/oops"}

    with pytest.raises(ConfigError, match="COMPLETION_TOPIC"):
        load_config(env)


def test_max_concurrency_rejects_non_integer() -> None:
    env = _VALID_ENV | {"MAX_CONCURRENCY": "lots"}

    with pytest.raises(ConfigError, match="MAX_CONCURRENCY"):
        load_config(env)


def test_max_concurrency_rejects_zero_or_negative() -> None:
    env = _VALID_ENV | {"MAX_CONCURRENCY": "0"}

    with pytest.raises(ConfigError, match="MAX_CONCURRENCY"):
        load_config(env)


def test_max_processing_seconds_must_be_under_pubsub_ack_deadline() -> None:
    env = _VALID_ENV | {"MAX_PROCESSING_SECONDS": "600"}

    with pytest.raises(ConfigError, match="600"):
        load_config(env)


def test_metrics_port_rejects_non_integer() -> None:
    env = _VALID_ENV | {"METRICS_PORT": "abc"}

    with pytest.raises(ConfigError, match="METRICS_PORT"):
        load_config(env)


def test_worker_config_is_immutable() -> None:
    cfg = load_config(_VALID_ENV.copy())

    with pytest.raises(Exception):  # frozen dataclass raises FrozenInstanceError
        cfg.max_concurrency = 99  # type: ignore[misc]


def test_load_config_does_not_leak_real_env_when_dict_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GCP_PROJECT_ID", "leaked-from-os-env")

    cfg = load_config(_VALID_ENV.copy())

    assert cfg.gcp_project_id == "growstory-prod"
    assert os.environ["GCP_PROJECT_ID"] == "leaked-from-os-env"
