"""Unit tests for the actual-prompt logger (imagegen/prompt_log.py).

The logger persists, per panel, the *real* prompt + rendered workflow the model
submits to ComfyUI — read back by the image-eval skill and by humans debugging a
bad panel. These tests pin down: it is a no-op when disabled, it writes one
JSON-per-panel with the placeholder-substituted prompt text, it preserves the
workflow verbatim, and a write failure never propagates (generation must not
break on a logging error).
"""

from __future__ import annotations

import json
from pathlib import Path

from imagegen.prompt_log import PromptLogger

# A prepared panel: single-key {field: value} dicts, as templates/2 produces. The
# prompt carries an unresolved {INPUT_1_AGE} + USER_ID for the logger to substitute.
_PANEL = [
    {"image": "USER_ID_STORY_ID_INPUT_1.png"},
    {"prompt": "Place the {INPUT_1_AGE} person, by USER_ID"},
    {"seed": 42},
    {"filename_prefix": "USER_ID_STORY_ID_P0_V2"},
]
_PLACEHOLDERS = {
    "USER_ID": "u1",
    "STORY_ID": "s1",
    "{INPUT_1_AGE}": "4-year-old",
}
_WORKFLOW = {"170:151": {"inputs": {"prompt": "Place the 4-year-old person, by u1"}}}


def _log(logger: PromptLogger, **overrides) -> None:
    kwargs = dict(
        story_id="s1",
        user_id="u1",
        story_ref="1_1",
        render_template_id="2",
        model_version="mv-test",
        panel_index=0,
        client_id="s1-0",
        placeholders=_PLACEHOLDERS,
        panel=_PANEL,
        workflow=_WORKFLOW,
    )
    kwargs.update(overrides)
    logger.log_panel(**kwargs)


def test_disabled_logger_is_a_noop(tmp_path: Path) -> None:
    logger = PromptLogger(None)
    assert logger.enabled is False
    _log(logger)  # must not raise or write anything
    assert list(tmp_path.iterdir()) == []


def test_writes_one_record_per_panel_with_resolved_prompt(tmp_path: Path) -> None:
    logger = PromptLogger(tmp_path)
    assert logger.enabled is True

    _log(logger)

    record_path = tmp_path / "s1" / "panel_00.json"
    assert record_path.exists()
    record = json.loads(record_path.read_text())
    # The age word + USER_ID are substituted into the logged prompt text.
    assert record["prompt_text"] == "Place the 4-year-old person, by u1"
    assert record["story_id"] == "s1"
    assert record["story_ref"] == "1_1"
    assert record["panel_index"] == 0
    assert record["panel_number"] == 1
    # The exact workflow submitted is preserved verbatim for debugging.
    assert record["workflow"] == _WORKFLOW
    # panel_fields are the substituted single-key entries (filename/seed too).
    fields = {k: v for entry in record["panel_fields"] for k, v in entry.items()}
    assert fields["filename_prefix"] == "u1_s1_P0_V2"
    assert fields["seed"] == 42


def test_panel_index_namespaces_the_filename(tmp_path: Path) -> None:
    logger = PromptLogger(tmp_path)
    _log(logger, panel_index=0)
    _log(logger, panel_index=3)
    names = sorted(p.name for p in (tmp_path / "s1").iterdir())
    assert names == ["panel_00.json", "panel_03.json"]


def test_status_and_prompt_id_are_recorded(tmp_path: Path) -> None:
    logger = PromptLogger(tmp_path)
    _log(logger, status="completed", comfyui_prompt_id="pid-9", processing_seconds=12.5)
    record = json.loads((tmp_path / "s1" / "panel_00.json").read_text())
    assert record["status"] == "completed"
    assert record["comfyui_prompt_id"] == "pid-9"
    assert record["processing_seconds"] == 12.5


def test_write_failure_is_swallowed(tmp_path: Path) -> None:
    # Point the root at a FILE so mkdir under it fails — the logger must warn and
    # return, never raise, so a logging fault can't abort a generation.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    logger = PromptLogger(blocker)
    _log(logger)  # must not raise
