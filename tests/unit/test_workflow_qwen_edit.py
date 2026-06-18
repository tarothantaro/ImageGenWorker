"""Unit tests for the Qwen-Image-Edit-2511 assets (workflows/2 + templates/2).

These lock the *shipped* asset pair so an accidental edit that breaks the
skeleton ↔ config ↔ template alignment (or leaves a dangling node link after
the subgraph flatten) fails fast, without a running ComfyUI. They exercise the
same generic :class:`WorkflowBuilder` path the worker uses, against the real
copied JSON under ``imagegen/`` — mirroring ``test_workflow.py``'s use of
workflow/template 1.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import imagegen
from imagegen.workflow import WorkflowBuilder

_PKG = Path(imagegen.__file__).resolve().parent
_WORKFLOW_ROOT = _PKG / "workflows"
_TEMPLATE_ROOT = _PKG / "templates"

# workflow 2 node ids that templates/2's panels customize (positional, in the
# same order as workflows/2/config.json's node list).
_NODE_EDIT_TARGET = "41"
_NODE_REFERENCE = "83"
_NODE_PROMPT = "170:151"
_NODE_KSAMPLER = "170:169"
_NODE_SAVE = "9"


@pytest.fixture
def builder() -> WorkflowBuilder:
    return WorkflowBuilder(_WORKFLOW_ROOT, _TEMPLATE_ROOT)


def test_prepare_loads_template_2_against_workflow_2(builder: WorkflowBuilder) -> None:
    prepared = builder.prepare("2")

    assert prepared.workflow_id == "2"
    assert prepared.panel_count == 2
    # Each panel lines up positionally with the workflow config's node list.
    assert all(len(panel) == len(prepared.config_nodes) for panel in prepared.panels)
    # The two LoadImage slots take distinct per-story input filenames.
    assert prepared.image_slots == [
        "USER_ID_STORY_ID_INPUT_1.png",
        "USER_ID_STORY_ID_INPUT_2.png",
    ]
    # One SaveImage → one output per run (no face-swap V1/V2 variants here).
    assert builder.output_prefixes(prepared.base_workflow) == ["Qwen_Edit_2511"]


def test_render_substitutes_panel_values_and_placeholders(
    builder: WorkflowBuilder,
) -> None:
    prepared = builder.prepare("2")

    workflow = builder.render(
        prepared,
        prepared.panels[0],
        placeholders={"USER_ID": "u1", "STORY_ID": "s1"},
        prompt=None,
        steps=None,
        seed=None,
    )

    # Image slots resolve to the per-story upload names (no remap step).
    assert workflow[_NODE_EDIT_TARGET]["inputs"]["image"] == "u1_s1_INPUT_1.png"
    assert workflow[_NODE_REFERENCE]["inputs"]["image"] == "u1_s1_INPUT_2.png"
    # The TextEncodeQwenImageEditPlus prompt field is named "prompt", not "text".
    assert workflow[_NODE_PROMPT]["inputs"]["prompt"].startswith("Using the person")
    assert workflow[_NODE_KSAMPLER]["inputs"]["seed"] == 771062815410683
    assert workflow[_NODE_SAVE]["inputs"]["filename_prefix"] == "u1_s1_P0"


def test_render_uses_each_panels_own_seed_and_prompt(builder: WorkflowBuilder) -> None:
    prepared = builder.prepare("2")
    placeholders = {"USER_ID": "u", "STORY_ID": "s"}

    first = builder.render(
        prepared, prepared.panels[0], placeholders=placeholders,
        prompt=None, steps=None, seed=None,
    )
    second = builder.render(
        prepared, prepared.panels[1], placeholders=placeholders,
        prompt=None, steps=None, seed=None,
    )

    assert first[_NODE_KSAMPLER]["inputs"]["seed"] == 771062815410683
    assert second[_NODE_KSAMPLER]["inputs"]["seed"] == 771062815410684
    assert first[_NODE_SAVE]["inputs"]["filename_prefix"] == "u_s_P0"
    assert second[_NODE_SAVE]["inputs"]["filename_prefix"] == "u_s_P1"
    assert (
        first[_NODE_PROMPT]["inputs"]["prompt"]
        != second[_NODE_PROMPT]["inputs"]["prompt"]
    )


def test_rendered_workflow_has_no_dangling_node_links(builder: WorkflowBuilder) -> None:
    """Every ``[node_id, slot]`` input reference must point at a node that exists
    — the invariant the subgraph flatten (``170:<inner>`` naming) must preserve."""
    prepared = builder.prepare("2")
    workflow = builder.render(
        prepared, prepared.panels[0],
        placeholders={"USER_ID": "u", "STORY_ID": "s"},
        prompt=None, steps=None, seed=None,
    )

    node_ids = set(workflow)
    dangling = [
        (node_id, field, value[0])
        for node_id, node in workflow.items()
        for field, value in node.get("inputs", {}).items()
        if isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and value[0] not in node_ids
    ]

    assert dangling == []
    # The single SaveImage is fed by the flattened subgraph's VAEDecode.
    assert workflow[_NODE_SAVE]["inputs"]["images"] == ["170:158", 0]
