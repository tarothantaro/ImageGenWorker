"""Unit tests for the Qwen-Image-Edit-2511 assets (workflows/2 + templates/2).

These lock the *shipped* asset pair so an accidental edit that breaks the
skeleton ↔ config ↔ template alignment, or leaves a dangling node link after
the subgraph flatten, fails fast without a running ComfyUI. They exercise the
same generic :class:`WorkflowBuilder` path the worker uses, against the real
copied JSON under ``imagegen/``.

Template 2 is the worker's default render (``_RENDER_TEMPLATE_ID = "2"`` in
``imagegen/model.py``): 6 story-bound panels, **one output image each** (the
Qwen edit — no ReActor face-swap, no V1/V2 variants). Template 3 is the
12-panel adventure render. These tests also pin the field-name-agnostic prompt
injection: the prompt input is ``prompt`` (not ``text``), and the ``{PROMPT}``
sentinel still receives the story prompt.
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
_NODE_PROMPT = "170:151"
_NODE_NEGATIVE_PROMPT = "170:149"
_NODE_LORA_STEPS = "170:165"
_NODE_KSAMPLER = "170:169"
_NODE_SAVE = "9"
# The flattened subgraph's VAEDecode — the image the SaveImage node persists.
_NODE_QWEN_OUTPUT = "170:158"


@pytest.fixture
def builder() -> WorkflowBuilder:
    return WorkflowBuilder(_WORKFLOW_ROOT, _TEMPLATE_ROOT)


def test_prepare_loads_template_2_against_workflow_2(builder: WorkflowBuilder) -> None:
    prepared = builder.prepare("2")

    assert prepared.workflow_id == "2"
    assert prepared.panel_count == 6
    # Each panel lines up positionally with the workflow config's node list.
    assert all(len(panel) == len(prepared.config_nodes) for panel in prepared.panels)
    # The single LoadImage node defaults to the one input photo slot.
    assert prepared.image_slots == ["USER_ID_STORY_ID_INPUT_1.png"]
    # One SaveImage node → a single output (one variant) per panel.
    assert builder.output_prefixes(prepared.base_workflow) == ["Qwen_Edit_2511"]


def test_prepare_injects_story_prompt_into_prompt_named_field(
    builder: WorkflowBuilder,
) -> None:
    """The {PROMPT} sentinel receives the story prompt even though this node's
    input is ``prompt`` (not ``text``) — the field name lives in the template,
    not in workflow.py. Reuses the real 6-panel story prompts/1_1.json."""
    prepared = builder.prepare("2", "1_1")

    prompt_text = next(f["prompt"] for f in prepared.panels[0] if "prompt" in f)
    assert "{PROMPT}" not in prompt_text
    assert "the {INPUT_1_AGE} person from the input image" in prompt_text


def test_prepare_defaults_missing_negative_prompts_to_empty(
    builder: WorkflowBuilder,
) -> None:
    prepared = builder.prepare("2", "1_1")

    workflow = builder.render(
        prepared,
        prepared.panels[0],
        placeholders={"USER_ID": "u1", "STORY_ID": "s1"},
    )

    assert workflow[_NODE_NEGATIVE_PROMPT]["inputs"]["prompt"] == ""


def test_prepare_injects_story_negative_prompt(builder: WorkflowBuilder) -> None:
    prepared = builder.prepare("2", "1_8")

    workflow = builder.render(
        prepared,
        prepared.panels[0],
        placeholders={"USER_ID": "u1", "STORY_ID": "s1"},
    )

    assert workflow[_NODE_NEGATIVE_PROMPT]["inputs"]["prompt"] == (
        "training wheels, side wheels, stabilizer wheels, "
        "side-mounted support wheels, extra small wheels beside the rear wheel, "
        "third wheel, fourth wheel"
    )


def test_render_substitutes_panel_values_and_placeholders(
    builder: WorkflowBuilder,
) -> None:
    prepared = builder.prepare("2")

    workflow = builder.render(
        prepared,
        prepared.panels[0],
        placeholders={"USER_ID": "u1", "STORY_ID": "s1"},
    )

    # Image slot resolves to the per-story upload name (no remap step).
    assert workflow[_NODE_EDIT_TARGET]["inputs"]["image"] == "u1_s1_INPUT_1.png"
    # No story bound → the prompt field still holds the sentinel.
    assert workflow[_NODE_PROMPT]["inputs"]["prompt"] == "{PROMPT}"
    assert workflow[_NODE_NEGATIVE_PROMPT]["inputs"]["prompt"] == "{NEGATIVE_PROMPT}"
    assert workflow[_NODE_LORA_STEPS]["inputs"]["value"] == 4
    assert workflow[_NODE_KSAMPLER]["inputs"]["seed"] == 771062815410683
    assert workflow[_NODE_SAVE]["inputs"]["filename_prefix"] == "u1_s1_P0"


def test_render_leaves_template_2_lora_steps_at_four(
    builder: WorkflowBuilder,
) -> None:
    prepared = builder.prepare("2", "1_1")

    workflows = [
        builder.render(
            prepared,
            panel,
            placeholders={"USER_ID": "u", "STORY_ID": "s"},
        )
        for panel in prepared.panels
    ]

    assert [w[_NODE_LORA_STEPS]["inputs"]["value"] for w in workflows] == [4] * 6


def test_render_sets_template_3_lora_steps_to_six_for_adventures(
    builder: WorkflowBuilder,
) -> None:
    prepared = builder.prepare("3", "2_1")

    workflows = [
        builder.render(
            prepared,
            panel,
            placeholders={
                "USER_ID": "u",
                "STORY_ID": "s",
                "{IMAGE_STYLE}": "soft storybook illustration style",
            },
        )
        for panel in prepared.panels
    ]

    assert prepared.panel_count == 12
    assert [w[_NODE_LORA_STEPS]["inputs"]["value"] for w in workflows] == [6] * 12


def test_single_output_persists_the_qwen_edit(builder: WorkflowBuilder) -> None:
    """The lone SaveImage persists the Qwen edit's VAEDecode directly — no
    ReActor face-swap stage stands between them anymore."""
    prepared = builder.prepare("2")
    workflow = builder.render(
        prepared,
        prepared.panels[0],
        placeholders={"USER_ID": "u", "STORY_ID": "s"},
    )

    assert workflow[_NODE_SAVE]["inputs"]["images"] == [_NODE_QWEN_OUTPUT, 0]
    # No ReActor nodes survive the edit.
    assert not [
        n
        for n in workflow.values()
        if str(n.get("class_type", "")).startswith("ReActor")
    ]


def test_render_uses_shared_default_seed(builder: WorkflowBuilder) -> None:
    prepared = builder.prepare("2")
    placeholders = {"USER_ID": "u", "STORY_ID": "s"}

    first = builder.render(prepared, prepared.panels[0], placeholders=placeholders)
    second = builder.render(prepared, prepared.panels[1], placeholders=placeholders)

    assert first[_NODE_KSAMPLER]["inputs"]["seed"] == 771062815410683
    assert second[_NODE_KSAMPLER]["inputs"]["seed"] == 771062815410683
    assert first[_NODE_SAVE]["inputs"]["filename_prefix"] == "u_s_P0"
    assert second[_NODE_SAVE]["inputs"]["filename_prefix"] == "u_s_P1"


def test_rendered_workflow_has_no_dangling_node_links(builder: WorkflowBuilder) -> None:
    """Every ``[node_id, slot]`` input reference must point at a node that exists
    — the invariant the subgraph flatten (``170:<inner>`` naming) must preserve."""
    prepared = builder.prepare("2")
    workflow = builder.render(
        prepared,
        prepared.panels[0],
        placeholders={"USER_ID": "u", "STORY_ID": "s"},
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
