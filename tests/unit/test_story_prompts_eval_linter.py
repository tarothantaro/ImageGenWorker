"""Unit tests for the story-prompts-eval linter helpers."""

from __future__ import annotations

import importlib.util
import json
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


def test_identity_pos_finds_placeholder() -> None:
    linter = _load_linter()

    assert linter._identity_pos("A prompt. {INPUT_IMAGE_IDENTITY} More.") == len(
        "A prompt. "
    )


def test_identity_pos_finds_expanded_sentence() -> None:
    linter = _load_linter()

    prompt = (
        "A prompt. Preserve the clothes, facial features, skin tone and hairstyle "
        "of the person from the input image. More."
    )
    assert linter._identity_pos(prompt) == len("A prompt. ")


def test_identity_pos_absent_returns_minus_one() -> None:
    linter = _load_linter()

    assert linter._identity_pos("A prompt without the identity pin.") == -1


def test_lint_story_accepts_twelve_panel_adventure(tmp_path, monkeypatch) -> None:
    linter = _load_linter()
    monkeypatch.setattr(linter, "_PROMPTS_DIR", tmp_path)
    prompts = [
        (
            "In a test adventure scene, the {INPUT_1_AGE} person from the input "
            "image points at a map, smiling bravely. {INPUT_IMAGE_IDENTITY} "
            "Eye-level medium shot, {IMAGE_STYLE}. Exactly one child in the frame, "
            "and no other people."
        )
        for _ in range(12)
    ]
    (tmp_path / "2_99.json").write_text(
        json.dumps(
            {
                "title": "Adventure",
                "characters": [],
                "gists": [f"Beat {i}" for i in range(12)],
                "texts": [f"Text {i}" for i in range(12)],
                "prompts": prompts,
            }
        )
    )
    (tmp_path / "character.json").write_text(json.dumps({"characters": {}}))
    findings = linter.Findings()

    linter.lint_story("2_99", findings)

    assert not [
        item for item in findings.items if item[0] == "FAIL" and item[2] == "structure"
    ]


def test_lint_story_rejects_twelve_panel_standard_story(tmp_path, monkeypatch) -> None:
    linter = _load_linter()
    monkeypatch.setattr(linter, "_PROMPTS_DIR", tmp_path)
    prompts = [
        (
            "In a standard story scene, the {INPUT_1_AGE} person from the input "
            "image waves hello, smiling. {INPUT_IMAGE_IDENTITY} Eye-level medium "
            "shot, {IMAGE_STYLE}. Exactly one child in the frame, and no other "
            "people."
        )
        for _ in range(12)
    ]
    (tmp_path / "1_99.json").write_text(
        json.dumps(
            {
                "title": "Standard",
                "characters": [],
                "gists": [f"Beat {i}" for i in range(12)],
                "prompts": prompts,
            }
        )
    )
    (tmp_path / "character.json").write_text(json.dumps({"characters": {}}))
    findings = linter.Findings()

    linter.lint_story("1_99", findings)

    assert (
        "FAIL",
        None,
        "structure",
        "len(prompts) == 12, expected 6",
    ) in findings.items


def test_lint_story_accepts_image_style_placeholder(tmp_path, monkeypatch) -> None:
    linter = _load_linter()
    monkeypatch.setattr(linter, "_PROMPTS_DIR", tmp_path)
    prompt = (
        "In a story scene, the {INPUT_1_AGE} person from the input image stacks "
        "blocks, smiling. {INPUT_IMAGE_IDENTITY} Eye-level medium shot, "
        "{IMAGE_STYLE}. Exactly one child in the frame, and no other people."
    )
    (tmp_path / "1_99.json").write_text(
        json.dumps(
            {
                "title": "Placeholder Style",
                "characters": [],
                "gists": [f"Beat {i}" for i in range(6)],
                "prompts": [prompt for _ in range(6)],
            }
        )
    )
    (tmp_path / "character.json").write_text(json.dumps({"characters": {}}))
    findings = linter.Findings()

    linter.lint_story("1_99", findings)

    assert not [item for item in findings.items if item[2] == "style"]


def _findings_for(
    linter: ModuleType, tmp_path, monkeypatch, prompt: str, category: str
) -> list[tuple]:
    """Lint a 6-panel story whose every panel is ``prompt``; return ``category`` findings."""
    monkeypatch.setattr(linter, "_PROMPTS_DIR", tmp_path)
    (tmp_path / "1_99.json").write_text(
        json.dumps(
            {
                "title": "Linter Fixture",
                "characters": [],
                "gists": [f"Beat {i}" for i in range(6)],
                "prompts": [prompt for _ in range(6)],
            }
        )
    )
    (tmp_path / "character.json").write_text(json.dumps({"characters": {}}))
    findings = linter.Findings()
    linter.lint_story("1_99", findings)
    return [item for item in findings.items if item[2] == category]


def test_count_guard_accepts_matching_headcount(tmp_path, monkeypatch) -> None:
    linter = _load_linter()
    prompt = (
        "In a scene, the {INPUT_1_AGE} person from the input image greets "
        "{GENDER_M_AGE_25}, smiling. {INPUT_IMAGE_IDENTITY} Eye-level medium shot. "
        "Exactly one child and one man in the frame, and no other people."
    )

    assert not _findings_for(linter, tmp_path, monkeypatch, prompt, "count-guard")


def test_count_guard_flags_missing(tmp_path, monkeypatch) -> None:
    linter = _load_linter()
    prompt = (
        "In a scene, the {INPUT_1_AGE} person from the input image waves, smiling. "
        "{INPUT_IMAGE_IDENTITY} Eye-level medium shot."
    )

    findings = _findings_for(linter, tmp_path, monkeypatch, prompt, "count-guard")

    assert findings and all("missing" in item[3] for item in findings)


def test_count_guard_flags_not_last_sentence(tmp_path, monkeypatch) -> None:
    linter = _load_linter()
    prompt = (
        "In a scene, the {INPUT_1_AGE} person from the input image waves. "
        "{INPUT_IMAGE_IDENTITY} Exactly one child in the frame, and no other "
        "people. Eye-level medium shot."
    )

    findings = _findings_for(linter, tmp_path, monkeypatch, prompt, "count-guard")

    assert findings and all("last sentence" in item[3] for item in findings)


def test_count_guard_flags_wrong_count(tmp_path, monkeypatch) -> None:
    linter = _load_linter()
    prompt = (
        "In a scene, the {INPUT_1_AGE} person from the input image greets "
        "{GENDER_M_AGE_25}, smiling. {INPUT_IMAGE_IDENTITY} Eye-level medium shot. "
        "Exactly one child in the frame, and no other people."
    )

    findings = _findings_for(linter, tmp_path, monkeypatch, prompt, "count-guard")

    assert findings and all(
        "states 1 people but the prompt names 2" in item[3] for item in findings
    )


def test_count_guard_counts_named_cast_token(tmp_path, monkeypatch) -> None:
    linter = _load_linter()
    # A non-GENDER named token (adventure cast) still counts as one person.
    prompt = (
        "In a scene, the {INPUT_1_AGE} person from the input image bows to "
        "{ADVENTURE_ELDER_MARA}, smiling. {INPUT_IMAGE_IDENTITY} Eye-level medium "
        "shot. Exactly one child and one elderly woman in the frame, and no other "
        "people."
    )

    assert not _findings_for(linter, tmp_path, monkeypatch, prompt, "count-guard")


def test_identity_pin_accepts_immediately_after_input_image_ref(
    tmp_path, monkeypatch
) -> None:
    linter = _load_linter()
    prompt = (
        "In a scene, the {INPUT_1_AGE} person from the input image "
        "{INPUT_IMAGE_IDENTITY} waves, smiling. Eye-level medium shot. "
        "Exactly one child in the "
        "frame, and no other people."
    )

    assert not _findings_for(linter, tmp_path, monkeypatch, prompt, "identity-pin")


def test_identity_pin_flags_trailing(tmp_path, monkeypatch) -> None:
    linter = _load_linter()
    prompt = (
        "In a scene, the {INPUT_1_AGE} person from the input image waves, smiling. "
        "Eye-level medium shot. Exactly one child in the frame, and no other "
        "people. {INPUT_IMAGE_IDENTITY}"
    )

    findings = _findings_for(linter, tmp_path, monkeypatch, prompt, "identity-pin")

    assert findings and all(
        "immediately after 'person from the input image'" in item[3]
        for item in findings
    )


def test_identity_pin_flags_missing(tmp_path, monkeypatch) -> None:
    linter = _load_linter()
    prompt = (
        "In a scene, the {INPUT_1_AGE} person from the input image waves, smiling. "
        "Eye-level medium shot. Exactly one child in the frame, and no other people."
    )

    findings = _findings_for(linter, tmp_path, monkeypatch, prompt, "identity-pin")

    assert findings and all("missing" in item[3] for item in findings)
