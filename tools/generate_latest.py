#!/usr/bin/env python3
"""Generate local story renders into eval_runs/latest without running eval.

By default this generates every story. Pass one story id, such as ``1_8``, to
generate only that prompt set.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parents[1]
_GENERATOR_PATH = (
    _REPO_ROOT / ".claude" / "skills" / "local-batch-eval" / "generate_stories.py"
)

_INPUT = "tests/assets/leo.jpg"
_AGE = "4-year-old"
_RUN_DIR = "eval_runs/latest"


def _load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("local_generate_stories", _GENERATOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load generator script: {_GENERATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _generator_args(
    *,
    story_id: str | None,
    url: str,
    timeout: float,
    model_version: str,
    user_id: str | None,
) -> list[str]:
    args = [
        "--input",
        _INPUT,
        "--age",
        _AGE,
        "--run-dir",
        _RUN_DIR,
        "--url",
        url,
        "--timeout",
        str(timeout),
        "--model-version",
        model_version,
    ]
    if user_id:
        args.extend(["--user-id", user_id])
    if story_id:
        args.extend(["--stories", story_id])
    return args


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "story_id",
        nargs="?",
        help="optional story id to generate, e.g. 1_8; omitted means all stories",
    )
    parser.add_argument(
        "--url", default="http://localhost:8188", help="live ComfyUI base URL"
    )
    parser.add_argument(
        "--timeout", type=float, default=300.0, help="per-panel ComfyUI timeout seconds"
    )
    parser.add_argument("--model-version", default="comfyui-qwen-edit-2511")
    parser.add_argument(
        "--user-id",
        default=None,
        help="GCS-path user component passed through to the generator",
    )
    args = parser.parse_args(argv)

    if args.story_id and "_" not in args.story_id:
        parser.error("story_id must look like <type>_<id>, for example 1_8")

    generator = _load_generator()
    return generator.main(
        _generator_args(
            story_id=args.story_id,
            url=args.url,
            timeout=args.timeout,
            model_version=args.model_version,
            user_id=args.user_id,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
