from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODULE_PATH = _REPO_ROOT / "tools" / "generate_latest.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate_latest", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generator_args_use_latest_run_defaults_for_all_stories() -> None:
    mod = _load()

    args = mod._generator_args(
        story_id=None,
        url="http://localhost:8188",
        timeout=300.0,
        model_version="comfyui-qwen-edit-2511",
        user_id=None,
    )

    assert args == [
        "--input",
        "tests/assets/liam.png",
        "--age",
        "4-year-old",
        "--run-dir",
        "eval_runs/latest",
        "--url",
        "http://localhost:8188",
        "--timeout",
        "300.0",
        "--model-version",
        "comfyui-qwen-edit-2511",
    ]


def test_generator_args_add_story_subset_and_user_id() -> None:
    mod = _load()

    args = mod._generator_args(
        story_id="1_8",
        url="http://comfy:8188",
        timeout=120.0,
        model_version="model-test",
        user_id="liam",
    )

    assert args[-4:] == ["--user-id", "liam", "--stories", "1_8"]
    assert args[args.index("--run-dir") + 1] == "eval_runs/latest"


def test_fetch_args_build_batch_mode_for_all_stories() -> None:
    mod = _load()

    args = mod._fetch_args(story_id=None, user_id=None)

    assert args == [
        "--local-root",
        "eval_runs/latest/outputs",
        "--log-dir",
        "eval_runs/latest/prompt_logs",
        "--out",
        "eval_runs/latest/eval",
    ]


def test_fetch_args_build_single_story_mode_with_default_user() -> None:
    mod = _load()

    args = mod._fetch_args(story_id="1_14", user_id=None)

    assert args == [
        "--local-root",
        "eval_runs/latest/outputs",
        "--log-dir",
        "eval_runs/latest/prompt_logs",
        "--out",
        "eval_runs/latest/eval/1_14__1_14",
        "--story",
        "1_14",
        "--story-id",
        "1_14",
        "--user-id",
        "liam",
    ]


def test_main_delegates_to_generator_then_refreshes_eval(monkeypatch) -> None:
    mod = _load()
    generator_calls: list[list[str]] = []
    fetch_calls: list[list[str]] = []

    def fake_main(args: list[str]) -> int:
        generator_calls.append(args)
        return 0

    def fake_fetch(args: list[str]) -> int:
        fetch_calls.append(args)
        return 0

    monkeypatch.setattr(mod, "_load_generator", lambda: SimpleNamespace(main=fake_main))
    monkeypatch.setattr(
        mod, "_load_fetch_outputs", lambda: SimpleNamespace(main=fake_fetch)
    )

    result = mod.main(["1_8", "--url", "http://comfy:8188", "--timeout", "120"])

    assert result == 0
    assert generator_calls == [
        [
            "--input",
            "tests/assets/liam.png",
            "--age",
            "4-year-old",
            "--run-dir",
            "eval_runs/latest",
            "--url",
            "http://comfy:8188",
            "--timeout",
            "120.0",
            "--model-version",
            "comfyui-qwen-edit-2511",
            "--stories",
            "1_8",
        ]
    ]
    assert fetch_calls == [
        [
            "--local-root",
            "eval_runs/latest/outputs",
            "--log-dir",
            "eval_runs/latest/prompt_logs",
            "--out",
            "eval_runs/latest/eval/1_8__1_8",
            "--story",
            "1_8",
            "--story-id",
            "1_8",
            "--user-id",
            "liam",
        ]
    ]


def test_main_returns_generation_failure_after_refreshing_eval(monkeypatch) -> None:
    mod = _load()

    monkeypatch.setattr(
        mod,
        "_load_generator",
        lambda: SimpleNamespace(main=lambda args: 7),
    )
    monkeypatch.setattr(
        mod,
        "_load_fetch_outputs",
        lambda: SimpleNamespace(main=lambda args: 0),
    )

    assert mod.main([]) == 7
