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
        "tests/assets/leo.jpg",
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
        user_id="leo",
    )

    assert args[-4:] == ["--user-id", "leo", "--stories", "1_8"]
    assert args[args.index("--run-dir") + 1] == "eval_runs/latest"


def test_main_delegates_to_generator(monkeypatch) -> None:
    mod = _load()
    calls: list[list[str]] = []

    def fake_main(args: list[str]) -> int:
        calls.append(args)
        return 7

    monkeypatch.setattr(mod, "_load_generator", lambda: SimpleNamespace(main=fake_main))

    result = mod.main(["1_8", "--url", "http://comfy:8188", "--timeout", "120"])

    assert result == 7
    assert calls == [
        [
            "--input",
            "tests/assets/leo.jpg",
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
