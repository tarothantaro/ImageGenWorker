"""Unit tests for the Qwen-Image-Edit-2511 assets (workflows/2 + templates/2).

These lock the *shipped* asset pair so an accidental edit that breaks the
skeleton ↔ config ↔ template alignment, drops the ReActor face-swap stage, or
leaves a dangling node link after the subgraph flatten fails fast, without a
running ComfyUI. They exercise the same generic :class:`WorkflowBuilder` path
the worker uses, against the real copied JSON under ``imagegen/``.

Template 2 is built to full parity with template 1 (6 story-bound panels, two
``_V1``/``_V2`` outputs) but is **not** the worker's active render — the model
still hardcodes template 1 (see ``imagegen/model.py``). It also pins the
field-name-agnostic prompt injection: template 2's prompt input is ``prompt``
(not ``text``), and the ``{PROMPT}`` sentinel still receives the story prompt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import imagegen
from imagegen.workflow import FINAL_OUTPUT_SUFFIX, WorkflowBuilder

_PKG = Path(imagegen.__file__).resolve().parent
_WORKFLOW_ROOT = _PKG / "workflows"
_TEMPLATE_ROOT = _PKG / "templates"

# workflow 2 node ids that templates/2's panels customize (positional, in the
# same order as workflows/2/config.json's node list).
_NODE_EDIT_TARGET = "41"
_NODE_PROMPT = "170:151"
_NODE_KSAMPLER = "170:169"
_NODE_SAVE_V1 = "9"
_NODE_SAVE_V2 = "119"
# The flattened subgraph's VAEDecode (the pre-face-swap image) + ReActor stage.
_NODE_QWEN_OUTPUT = "170:158"
_NODE_REACTOR_SWAP = "121"
_NODE_REACTOR_RESTORE = "120"


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
    # Two SaveImage nodes → two variants per panel (V1 pre-swap, V2 restored).
    assert builder.output_prefixes(prepared.base_workflow) == [
        "Qwen_Edit_2511_V1",
        "Qwen_Edit_2511_V2",
    ]


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
    assert workflow[_NODE_KSAMPLER]["inputs"]["seed"] == 771062815410683
    assert workflow[_NODE_SAVE_V1]["inputs"]["filename_prefix"] == "u1_s1_P0_V1"
    assert workflow[_NODE_SAVE_V2]["inputs"]["filename_prefix"] == "u1_s1_P0_V2"


def test_reactor_face_swap_stage_is_wired(builder: WorkflowBuilder) -> None:
    """The ReActor stage mirrors workflow 1: V1 is the raw Qwen edit, V2 is the
    face-restored image whose face-swap source is the input photo (node 41)."""
    prepared = builder.prepare("2")
    workflow = builder.render(
        prepared, prepared.panels[0],
        placeholders={"USER_ID": "u", "STORY_ID": "s"},
    )

    # V1 = the flattened subgraph's decode; V2 = the ReActor restore output.
    assert workflow[_NODE_SAVE_V1]["inputs"]["images"] == [_NODE_QWEN_OUTPUT, 0]
    assert workflow[_NODE_SAVE_V2]["inputs"]["images"] == [_NODE_REACTOR_RESTORE, 0]
    # Swap target is the Qwen output; swap source is the user's input photo.
    assert workflow[_NODE_REACTOR_SWAP]["inputs"]["input_image"] == [_NODE_QWEN_OUTPUT, 0]
    assert workflow[_NODE_REACTOR_SWAP]["inputs"]["source_image"] == [_NODE_EDIT_TARGET, 0]
    assert workflow[_NODE_REACTOR_RESTORE]["inputs"]["image"] == [_NODE_REACTOR_SWAP, 0]
    # The model selects the face-restored V2 as the final image.
    assert builder.final_output_prefix(workflow) == "u_s_P0" + FINAL_OUTPUT_SUFFIX


def test_render_uses_each_panels_own_seed(builder: WorkflowBuilder) -> None:
    prepared = builder.prepare("2")
    placeholders = {"USER_ID": "u", "STORY_ID": "s"}

    first = builder.render(prepared, prepared.panels[0], placeholders=placeholders)
    second = builder.render(prepared, prepared.panels[1], placeholders=placeholders)

    assert first[_NODE_KSAMPLER]["inputs"]["seed"] == 771062815410683
    assert second[_NODE_KSAMPLER]["inputs"]["seed"] == 771062815410684
    assert first[_NODE_SAVE_V1]["inputs"]["filename_prefix"] == "u_s_P0_V1"
    assert second[_NODE_SAVE_V1]["inputs"]["filename_prefix"] == "u_s_P1_V1"


def test_rendered_workflow_has_no_dangling_node_links(builder: WorkflowBuilder) -> None:
    """Every ``[node_id, slot]`` input reference must point at a node that exists
    — the invariant the subgraph flatten (``170:<inner>`` naming) must preserve."""
    prepared = builder.prepare("2")
    workflow = builder.render(
        prepared, prepared.panels[0],
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
