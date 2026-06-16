"""Unit tests for the pure workflow renderer (imagegen/workflow.py).

These exercise the renderer against the *real* copied assets
(``imagegen/workflows/2`` + ``imagegen/templates/3``) for the happy paths, and
against tiny crafted assets in ``tmp_path`` for the multi-panel and
malformed-asset branches. No ComfyUI, no network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import imagegen
from imagegen.failure_classification import UnsupportedTemplateError
from imagegen.workflow import FINAL_OUTPUT_SUFFIX, WorkflowBuilder

_PKG = Path(imagegen.__file__).resolve().parent
_WORKFLOW_ROOT = _PKG / "workflows"
_TEMPLATE_ROOT = _PKG / "templates"

# Template 3 / workflow 2 node ids that templates/3 customizes.
_NODE_INPUT_IMAGE = "46"
_NODE_SOURCE_FACE = "122"
_NODE_PROMPT = "68:6"
_NODE_STEPS = "68:90"
_NODE_SEED = "68:25"
_NODE_SAVE_V1 = "123"
_NODE_SAVE_V2 = "119"

_TEMPLATE_DEFAULT_SEED = 771062815410683
# templates/3's input image filename carries USER_ID / STORY_ID placeholders.
_INPUT_SLOT = "USER_ID_STORY_ID_INPUT_1.png"


@pytest.fixture
def real_builder() -> WorkflowBuilder:
    return WorkflowBuilder(_WORKFLOW_ROOT, _TEMPLATE_ROOT)


def _write_assets(
    tmp_path: Path,
    *,
    template: dict[str, Any],
    workflow_config: dict[str, Any] | None = None,
    workflow: dict[str, Any] | None = None,
    template_id: str = "t",
    workflow_id: str = "w",
    template_text: str | None = None,
    story: dict[str, Any] | None = None,
    story_id: str = "s",
    characters: dict[str, Any] | None = None,
) -> WorkflowBuilder:
    wf_root = tmp_path / "workflows"
    tpl_root = tmp_path / "templates"
    prompts_root = tmp_path / "prompts"
    (tpl_root / template_id).mkdir(parents=True)
    if template_text is not None:
        (tpl_root / template_id / "config.json").write_text(template_text)
    else:
        (tpl_root / template_id / "config.json").write_text(json.dumps(template))
    if workflow_config is not None or workflow is not None:
        (wf_root / workflow_id).mkdir(parents=True)
    if workflow_config is not None:
        (wf_root / workflow_id / "config.json").write_text(json.dumps(workflow_config))
    if workflow is not None:
        (wf_root / workflow_id / "workflow.json").write_text(json.dumps(workflow))
    if story is not None or characters is not None:
        prompts_root.mkdir(parents=True, exist_ok=True)
    if story is not None:
        (prompts_root / f"{story_id}.json").write_text(json.dumps(story))
    if characters is not None:
        (prompts_root / "character.json").write_text(
            json.dumps({"characters": characters})
        )
    return WorkflowBuilder(wf_root, tpl_root, prompts_root)


# --- prepare: happy ----------------------------------------------------------


def test_prepare_loads_template_3_against_workflow_2(
    real_builder: WorkflowBuilder,
) -> None:
    prepared = real_builder.prepare("3")

    assert prepared.workflow_id == "2"
    assert prepared.panel_count == 1
    assert len(prepared.panels[0]) == len(prepared.config_nodes)
    # Both LoadImage nodes default to the same input filename → one dedup'd slot.
    assert prepared.image_slots == [_INPUT_SLOT]


def test_prepare_dedups_image_slots_across_panels(tmp_path: Path) -> None:
    builder = _write_assets(
        tmp_path,
        template={
            "workflow_id": "w",
            "panels": [
                [{"image": "USER_ID_a.png"}, {"text": "scene one"}],
                [{"image": "USER_ID_a.png"}, {"text": "scene two"}],
                [{"image": "USER_ID_b.png"}, {"text": "scene three"}],
            ],
        },
        workflow_config={
            "nodes": [
                {"id": 1, "type": "LoadImage"},
                {"id": 2, "type": "CLIPTextEncode"},
            ]
        },
        workflow={
            "1": {"class_type": "LoadImage", "inputs": {"image": "x.png"}},
            "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "x"}},
        },
    )

    prepared = builder.prepare("t")

    assert prepared.panel_count == 3
    assert prepared.image_slots == ["USER_ID_a.png", "USER_ID_b.png"]


# --- prepare: malformed assets → UnsupportedTemplateError --------------------


def test_prepare_missing_template_raises(real_builder: WorkflowBuilder) -> None:
    with pytest.raises(UnsupportedTemplateError, match="missing asset"):
        real_builder.prepare("does-not-exist")


def test_prepare_invalid_json_raises(tmp_path: Path) -> None:
    builder = _write_assets(tmp_path, template={}, template_text="{not json")

    with pytest.raises(UnsupportedTemplateError, match="invalid JSON"):
        builder.prepare("t")


def test_prepare_without_workflow_id_raises(tmp_path: Path) -> None:
    builder = _write_assets(tmp_path, template={"panels": [[]]})

    with pytest.raises(UnsupportedTemplateError, match="no workflow_id"):
        builder.prepare("t")


def test_prepare_without_panels_raises(tmp_path: Path) -> None:
    builder = _write_assets(
        tmp_path,
        template={"workflow_id": "w", "panels": []},
        workflow_config={"nodes": []},
    )

    with pytest.raises(UnsupportedTemplateError, match="no panels"):
        builder.prepare("t")


def test_prepare_panel_node_count_mismatch_raises(tmp_path: Path) -> None:
    builder = _write_assets(
        tmp_path,
        template={
            "workflow_id": "w",
            "panels": [[{"image": "a.png"}], [{"image": "a.png"}, {"text": "x"}]],
        },
        workflow_config={"nodes": [{"id": 1, "type": "LoadImage"}]},
    )

    with pytest.raises(UnsupportedTemplateError, match="panel 1 has 2 entries"):
        builder.prepare("t")


# --- prepare: story-bound templates (per-panel prompts + character tokens) ----


def test_prepare_template_4_injects_story_prompts_and_resolves_characters(
    real_builder: WorkflowBuilder,
) -> None:
    """Template 4 declares ``"story": "1_1"`` — prepare sources each panel's text
    from prompts/1_1.json and resolves {TOKEN}s against prompts/character.json."""
    prepared = real_builder.prepare("4")

    assert prepared.panel_count == 6
    # Panel 0 is solo (no token): the story prompt is injected verbatim.
    panel0_text = next(f["text"] for f in prepared.panels[0] if "text" in f)
    assert panel0_text.startswith("Place the person from the input image")
    # Panel 1 references {GENDER_F_AGE_70_RACE_ASIAN}: resolved to its
    # description, with no leftover placeholder braces.
    panel1_text = next(f["text"] for f in prepared.panels[1] if "text" in f)
    assert "{" not in panel1_text and "}" not in panel1_text
    assert "elderly East Asian woman" in panel1_text
    assert "the far left" in panel1_text


def test_prepare_story_prompt_count_mismatch_raises(tmp_path: Path) -> None:
    builder = _write_assets(
        tmp_path,
        template={
            "workflow_id": "w",
            "story": "s",
            "panels": [[{"text": ""}], [{"text": ""}]],
        },
        workflow_config={"nodes": [{"id": 1, "type": "CLIPTextEncode"}]},
        workflow={"1": {"class_type": "CLIPTextEncode", "inputs": {"text": "x"}}},
        story={"prompts": ["only one prompt"]},
        characters={},
    )

    with pytest.raises(
        UnsupportedTemplateError,
        match="has 1 prompts but the template has 2 panels",
    ):
        builder.prepare("t")


def test_prepare_story_panel_without_text_field_raises(tmp_path: Path) -> None:
    builder = _write_assets(
        tmp_path,
        template={"workflow_id": "w", "story": "s", "panels": [[{"image": "a.png"}]]},
        workflow_config={"nodes": [{"id": 1, "type": "LoadImage"}]},
        workflow={"1": {"class_type": "LoadImage", "inputs": {"image": "x"}}},
        story={"prompts": ["a prompt with nowhere to go"]},
        characters={},
    )

    with pytest.raises(UnsupportedTemplateError, match="no 'text' field"):
        builder.prepare("t")


def test_prepare_story_resolves_known_tokens_and_leaves_unknown(tmp_path: Path) -> None:
    builder = _write_assets(
        tmp_path,
        template={"workflow_id": "w", "story": "s", "panels": [[{"text": ""}]]},
        workflow_config={"nodes": [{"id": 1, "type": "CLIPTextEncode"}]},
        workflow={"1": {"class_type": "CLIPTextEncode", "inputs": {"text": "x"}}},
        story={"prompts": ["{HERO} meets {UNKNOWN}"]},
        characters={
            "HERO": {"description": "a brave knight"},
            "NO_DESC": {},  # entry without a description → skipped
            "BAD": "not-a-dict",  # non-dict entry → skipped
        },
    )

    prepared = builder.prepare("t")

    text = next(f["text"] for f in prepared.panels[0] if "text" in f)
    assert text == "a brave knight meets {UNKNOWN}"


# --- render: happy -----------------------------------------------------------


def test_render_applies_overrides_and_placeholders(
    real_builder: WorkflowBuilder,
) -> None:
    prepared = real_builder.prepare("3")

    workflow = real_builder.render(
        prepared,
        prepared.panels[0],
        placeholders={"USER_ID": "u1", "STORY_ID": "s1"},
        prompt="a teacher between two kids",
        steps=8,
        seed=42,
    )

    assert workflow[_NODE_PROMPT]["inputs"]["text"] == "a teacher between two kids"
    assert workflow[_NODE_STEPS]["inputs"]["value"] == 8
    assert workflow[_NODE_SEED]["inputs"]["noise_seed"] == 42
    # Image filename is substituted to the per-story upload name (no remap).
    assert workflow[_NODE_INPUT_IMAGE]["inputs"]["image"] == "u1_s1_INPUT_1.png"
    assert workflow[_NODE_SOURCE_FACE]["inputs"]["image"] == "u1_s1_INPUT_1.png"
    assert workflow[_NODE_SAVE_V1]["inputs"]["filename_prefix"] == "u1_s1_V1"
    assert workflow[_NODE_SAVE_V2]["inputs"]["filename_prefix"] == "u1_s1_V2"


def test_render_without_overrides_keeps_template_defaults(
    real_builder: WorkflowBuilder,
) -> None:
    prepared = real_builder.prepare("3")

    workflow = real_builder.render(
        prepared,
        prepared.panels[0],
        placeholders={"USER_ID": "u1", "STORY_ID": "s1"},
        prompt=None,
        steps=None,
        seed=None,
    )

    assert workflow[_NODE_PROMPT]["inputs"]["text"] == ""
    assert workflow[_NODE_STEPS]["inputs"]["value"] == 6
    assert workflow[_NODE_SEED]["inputs"]["noise_seed"] == _TEMPLATE_DEFAULT_SEED


def test_render_uses_each_panels_own_values(tmp_path: Path) -> None:
    builder = _write_assets(
        tmp_path,
        template={
            "workflow_id": "w",
            "panels": [
                [{"text": "scene one"}, {"noise_seed": 11}],
                [{"text": "scene two"}, {"noise_seed": 22}],
            ],
        },
        workflow_config={
            "nodes": [
                {"id": 1, "type": "CLIPTextEncode"},
                {"id": 2, "type": "RandomNoise"},
            ]
        },
        workflow={
            "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "x"}},
            "2": {"class_type": "RandomNoise", "inputs": {"noise_seed": 0}},
        },
    )
    prepared = builder.prepare("t")

    first = builder.render(
        prepared,
        prepared.panels[0],
        placeholders={},
        prompt=None,
        steps=None,
        seed=None,
    )
    second = builder.render(
        prepared,
        prepared.panels[1],
        placeholders={},
        prompt=None,
        steps=None,
        seed=None,
    )

    assert first["1"]["inputs"]["text"] == "scene one"
    assert first["2"]["inputs"]["noise_seed"] == 11
    assert second["1"]["inputs"]["text"] == "scene two"
    assert second["2"]["inputs"]["noise_seed"] == 22


def test_render_does_not_mutate_the_base_workflow(
    real_builder: WorkflowBuilder,
) -> None:
    prepared = real_builder.prepare("3")

    first = real_builder.render(
        prepared,
        prepared.panels[0],
        placeholders={"USER_ID": "u", "STORY_ID": "s"},
        prompt=None,
        steps=None,
        seed=1,
    )
    second = real_builder.render(
        prepared,
        prepared.panels[0],
        placeholders={"USER_ID": "u", "STORY_ID": "s"},
        prompt=None,
        steps=None,
        seed=2,
    )

    assert first[_NODE_SEED]["inputs"]["noise_seed"] == 1
    assert second[_NODE_SEED]["inputs"]["noise_seed"] == 2


# --- render: malformed assets → UnsupportedTemplateError ---------------------


def test_render_raises_when_workflow_missing_a_config_node(tmp_path: Path) -> None:
    builder = _write_assets(
        tmp_path,
        template={"workflow_id": "w", "panels": [[{"text": "hi"}]]},
        workflow_config={"nodes": [{"id": 999, "type": "X"}]},
        workflow={},
    )
    prepared = builder.prepare("t")

    with pytest.raises(UnsupportedTemplateError, match="no node '999'"):
        builder.render(
            prepared,
            prepared.panels[0],
            placeholders={},
            prompt=None,
            steps=None,
            seed=None,
        )


def test_render_raises_when_node_lacks_the_paneled_input(tmp_path: Path) -> None:
    builder = _write_assets(
        tmp_path,
        template={"workflow_id": "w", "panels": [[{"missing_field": 1}]]},
        workflow_config={"nodes": [{"id": 1, "type": "X"}]},
        workflow={"1": {"class_type": "X", "inputs": {}}},
    )
    prepared = builder.prepare("t")

    with pytest.raises(UnsupportedTemplateError, match="no input 'missing_field'"):
        builder.render(
            prepared,
            prepared.panels[0],
            placeholders={},
            prompt=None,
            steps=None,
            seed=None,
        )


# --- final_output_prefix -----------------------------------------------------


def test_final_output_prefix_selects_the_v2_saveimage(
    real_builder: WorkflowBuilder,
) -> None:
    workflow = {
        "a": {"class_type": "SaveImage", "inputs": {"filename_prefix": "x_V1"}},
        "b": {"class_type": "SaveImage", "inputs": {"filename_prefix": "x_V2"}},
        "c": {"class_type": "LoadImage", "inputs": {"image": "x.png"}},
    }

    assert real_builder.final_output_prefix(workflow) == "x" + FINAL_OUTPUT_SUFFIX


def test_final_output_prefix_raises_when_no_v2_node(
    real_builder: WorkflowBuilder,
) -> None:
    workflow = {
        "a": {"class_type": "SaveImage", "inputs": {"filename_prefix": "x_V1"}},
    }

    with pytest.raises(UnsupportedTemplateError, match="filename_prefix"):
        real_builder.final_output_prefix(workflow)
