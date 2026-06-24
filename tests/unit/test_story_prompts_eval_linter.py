"""Unit tests for the story-prompts-eval linter helpers."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _load_linter() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[2]
        / ".claude"
        / "skills"
        / "story-prompts-eval"
        / "lint_prompts.py"
    )
    spec = importlib.util.spec_from_file_location("story_prompts_eval_linter", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_identity_tail_accepts_centralized_placeholder() -> None:
    linter = _load_linter()

    assert linter._has_identity_tail("A prompt. {INPUT_IMAGE_IDENTITY}")


def test_identity_tail_accepts_expanded_sentence() -> None:
    linter = _load_linter()

    assert linter._has_identity_tail(
        "A prompt. Preserve the facial features, skin tone and hairstyle "
        "of the person from the input image."
    )


def test_identity_tail_rejects_missing_ending() -> None:
    linter = _load_linter()

    assert not linter._has_identity_tail("A prompt without the identity ending.")
