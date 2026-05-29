"""Unit tests for the pure workflow renderer (imagegen/workflow.py).

These exercise the renderer against the *real* copied assets
(``imagegen/workflows/2`` + ``imagegen/templates/3``) for the happy paths, and
against tiny crafted assets in ``tmp_path`` for the malformed-asset branches.
No ComfyUI, no network.
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
) -> WorkflowBuilder:
    wf_root = tmp_path / "workflows"
    tpl_root = tmp_path / "templates"
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
    return WorkflowBuilder(wf_root, tpl_root)


# --- prepare: happy ----------------------------------------------------------


def test_prepare_loads_template_3_against_workflow_2(
    real_builder: WorkflowBuilder,
) -> None:
    prepared = real_builder.prepare("3")

    assert prepared.workflow_id == "2"
    assert len(prepared.panel) == len(prepared.config_nodes)
    # Both LoadImage nodes default to the same "character.png" → one dedup'd slot.
    assert prepared.image_slots == ["character.png"]


def test_panel_default_returns_template_value_or_none(
    real_builder: WorkflowBuilder,
) -> None:
    prepared = real_builder.prepare("3")

    assert prepared.panel_default("noise_seed") == _TEMPLATE_DEFAULT_SEED
    assert prepared.panel_default("does_not_exist") is None


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
        template={"workflow_id": "w", "panels": [[{"image": "a.png"}]]},
        workflow_config={"nodes": []},
    )

    with pytest.raises(UnsupportedTemplateError, match="panel has 1 entries"):
        builder.prepare("t")


# --- render: happy -----------------------------------------------------------


def test_render_applies_overrides_placeholders_and_image_remap(
    real_builder: WorkflowBuilder,
) -> None:
    prepared = real_builder.prepare("3")

    workflow = real_builder.render(
        prepared,
        placeholders={"USER_ID": "u1", "STORY_ID": "s1"},
        image_remap={"character.png": "u1_s1_src0.png"},
        prompt="a teacher between two kids",
        steps=8,
        seed=42,
    )

    assert workflow[_NODE_PROMPT]["inputs"]["text"] == "a teacher between two kids"
    assert workflow[_NODE_STEPS]["inputs"]["value"] == 8
    assert workflow[_NODE_SEED]["inputs"]["noise_seed"] == 42
    assert workflow[_NODE_INPUT_IMAGE]["inputs"]["image"] == "u1_s1_src0.png"
    assert workflow[_NODE_SOURCE_FACE]["inputs"]["image"] == "u1_s1_src0.png"
    assert workflow[_NODE_SAVE_V1]["inputs"]["filename_prefix"] == "u1_s1_V1"
    assert workflow[_NODE_SAVE_V2]["inputs"]["filename_prefix"] == "u1_s1_V2"


def test_render_without_overrides_keeps_template_defaults(
    real_builder: WorkflowBuilder,
) -> None:
    prepared = real_builder.prepare("3")

    workflow = real_builder.render(
        prepared,
        placeholders={"USER_ID": "u1", "STORY_ID": "s1"},
        image_remap={"character.png": "u1_s1_src0.png"},
        prompt=None,
        steps=None,
        seed=None,
    )

    assert workflow[_NODE_PROMPT]["inputs"]["text"] == ""
    assert workflow[_NODE_STEPS]["inputs"]["value"] == 6
    assert workflow[_NODE_SEED]["inputs"]["noise_seed"] == _TEMPLATE_DEFAULT_SEED


def test_render_does_not_mutate_the_base_workflow(
    real_builder: WorkflowBuilder,
) -> None:
    prepared = real_builder.prepare("3")

    first = real_builder.render(
        prepared,
        placeholders={"USER_ID": "u", "STORY_ID": "s"},
        image_remap={"character.png": "in.png"},
        prompt=None,
        steps=None,
        seed=1,
    )
    second = real_builder.render(
        prepared,
        placeholders={"USER_ID": "u", "STORY_ID": "s"},
        image_remap={"character.png": "in.png"},
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
            placeholders={},
            image_remap={},
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
            placeholders={},
            image_remap={},
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
