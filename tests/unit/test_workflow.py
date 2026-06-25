"""Unit tests for the pure workflow renderer (imagegen/workflow.py).

These exercise the renderer against the *real* copied assets
(``imagegen/workflows/1`` + ``imagegen/templates/1``, the single render
template) for the happy paths, and against tiny crafted assets in ``tmp_path``
for the multi-panel and malformed-asset branches. No ComfyUI, no network.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

import imagegen
from imagegen.failure_classification import UnsupportedTemplateError
from imagegen.workflow import (
    FINAL_OUTPUT_SUFFIX,
    WorkflowBuilder,
    _compose_random_character,
    _resolve_race_key,
)

_PKG = Path(imagegen.__file__).resolve().parent
_WORKFLOW_ROOT = _PKG / "workflows"
_TEMPLATE_ROOT = _PKG / "templates"

# workflow 1 node ids that templates/1's panels customize.
_NODE_INPUT_IMAGE = "46"
_NODE_SOURCE_FACE = "122"
_NODE_PROMPT = "68:6"
_NODE_STEPS = "68:90"
_NODE_SEED = "68:25"
_NODE_SAVE_V1 = "123"
_NODE_SAVE_V2 = "119"

# templates/1 panel 0 seed; its input image filename carries USER_ID / STORY_ID.
_TEMPLATE_DEFAULT_SEED = 771062815410683
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
    character_json: dict[str, Any] | None = None,
    rng: random.Random | None = None,
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
    if story is not None or characters is not None or character_json is not None:
        prompts_root.mkdir(parents=True, exist_ok=True)
    if story is not None:
        (prompts_root / f"{story_id}.json").write_text(json.dumps(story))
    # ``character_json`` writes the full file (dimensions + look tables) verbatim;
    # ``characters`` is the shorthand for just the enumerated ``characters`` map.
    if character_json is not None:
        (prompts_root / "character.json").write_text(json.dumps(character_json))
    elif characters is not None:
        (prompts_root / "character.json").write_text(
            json.dumps({"characters": characters})
        )
    return WorkflowBuilder(wf_root, tpl_root, prompts_root, rng=rng)


# --- prepare: happy ----------------------------------------------------------


def test_prepare_loads_template_1_against_workflow_1(
    real_builder: WorkflowBuilder,
) -> None:
    prepared = real_builder.prepare("1")

    assert prepared.workflow_id == "1"
    assert prepared.panel_count == 6
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


def test_prepare_injects_story_prompts_and_resolves_characters(
    real_builder: WorkflowBuilder,
) -> None:
    """``prepare("1", "1_1")`` sources each panel's text from prompts/1_1.json
    and resolves {TOKEN}s against prompts/character.json (the template no longer
    binds a story inline; the job's type/id select it)."""
    prepared = real_builder.prepare("1", "1_1")

    assert prepared.panel_count == 6
    # Panel 0 is solo (no character token): the story prompt is injected as-is,
    # still carrying the per-job {INPUT_1_AGE} token (resolved at render time,
    # like USER_ID/STORY_ID — not at prepare time).
    panel0_text = next(f["text"] for f in prepared.panels[0] if "text" in f)
    assert panel0_text.startswith(
        "On a tree-lined suburban pavement in the morning sunlight, "
        "the {INPUT_1_AGE} person from the input image"
    )
    # The render-time token survives prepare (resolved later, like USER_ID).
    assert "{INPUT_1_AGE} person from the input image" in panel0_text
    # Panel 1 references the race-free {GENDER_F_AGE_70}: composed to a look here
    # (a random race per job). The only braces left are the render-time
    # {INPUT_1_AGE}.
    panel1_text = next(f["text"] for f in prepared.panels[1] if "text" in f)
    assert "{GENDER" not in panel1_text
    assert "elderly" in panel1_text and "woman" in panel1_text
    assert "in the mid-ground" in panel1_text
    assert "{INPUT_1_AGE}" in panel1_text


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


def test_prepare_story_panel_without_prompt_placeholder_raises(tmp_path: Path) -> None:
    builder = _write_assets(
        tmp_path,
        template={"workflow_id": "w", "story": "s", "panels": [[{"image": "a.png"}]]},
        workflow_config={"nodes": [{"id": 1, "type": "LoadImage"}]},
        workflow={"1": {"class_type": "LoadImage", "inputs": {"image": "x"}}},
        story={"prompts": ["a prompt with nowhere to go"]},
        characters={},
    )

    with pytest.raises(UnsupportedTemplateError, match=r"no \{PROMPT\} placeholder"):
        builder.prepare("t")


def test_prepare_story_resolves_known_tokens_and_leaves_unknown(tmp_path: Path) -> None:
    builder = _write_assets(
        tmp_path,
        template={"workflow_id": "w", "story": "s", "panels": [[{"text": "{PROMPT}"}]]},
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


def test_prepare_story_resolves_extra_character_file(tmp_path: Path) -> None:
    builder = _write_assets(
        tmp_path,
        template={"workflow_id": "w", "story": "s", "panels": [[{"text": "{PROMPT}"}]]},
        workflow_config={"nodes": [{"id": 1, "type": "CLIPTextEncode"}]},
        workflow={"1": {"class_type": "CLIPTextEncode", "inputs": {"text": "x"}}},
        story={
            "character_file": "adventure_character.json",
            "prompts": ["{ADVENTURE_HEALER_ALDEN} helps {HERO}"],
        },
        characters={"HERO": {"description": "a brave child"}},
    )
    extra_file = tmp_path / "prompts" / "adventure_character.json"
    extra_file.write_text(
        json.dumps(
            {
                "characters": {
                    "ADVENTURE_HEALER_ALDEN": {
                        "description": "a medieval healer in a green robe"
                    }
                }
            }
        )
    )

    prepared = builder.prepare("t")

    text = next(f["text"] for f in prepared.panels[0] if "text" in f)
    assert text == "a medieval healer in a green robe helps a brave child"


# --- prepare: random look for un-enumerated character tokens -----------------

# A character.json whose look tables each hold exactly one option, so a composed
# look is fully determined (independent of the rng) and can be asserted exactly.
# ``GENDER_F_AGE_30_RACE_ASIAN`` is enumerated; every other config is composed.
_SINGLE_LOOK_CHARACTER_JSON: dict[str, Any] = {
    "dimensions": {
        "gender": {
            "M": {"noun": "man", "noun_child": "boy"},
            "F": {"noun": "woman", "noun_child": "girl"},
        },
        "age": {
            "30": {"phrase": "a 30-year-old", "child": False},
            "08": {"phrase": "an 8-year-old", "child": True},
        },
        "race": {
            "ASIAN": {"adj": "East Asian"},
            "SOUTH_ASIAN": {"adj": "South Asian"},
        },
    },
    "hair": {"H": "short black hair"},
    "build": {"B": "an average build"},
    "wardrobe": {"W": "a blue shirt"},
    "features": {"F": "a kind smile"},
    "characters": {
        "GENDER_F_AGE_30_RACE_ASIAN": {"description": "the enumerated woman"},
    },
}

# Multi-option look tables for exercising the *randomness* of the draw.
_MULTI_LOOK_CHARACTER_JSON: dict[str, Any] = {
    "dimensions": {
        "gender": {"M": {"noun": "man", "noun_child": "boy"}},
        "age": {"30": {"phrase": "a 30-year-old", "child": False}},
        "race": {"ASIAN": {"adj": "East Asian"}},
    },
    "hair": {f"H{i}": f"hair{i}" for i in range(8)},
    "build": {f"B{i}": f"build{i}" for i in range(8)},
    "wardrobe": {f"W{i}": f"wear{i}" for i in range(8)},
    "features": {f"F{i}": f"feat{i}" for i in range(8)},
}

_COMPOSED_SINGLE_LOOK = (
    "a 30-year-old East Asian man with short black hair, "
    "an average build, wearing a blue shirt, with a kind smile"
)


def test_prepare_composes_random_look_for_unenumerated_token(tmp_path: Path) -> None:
    """A character token with no enumerated description gets a look composed from
    the modular tables — once per job, so it is identical in every panel."""
    builder = _write_assets(
        tmp_path,
        template={
            "workflow_id": "w",
            "story": "s",
            "panels": [
                [{"text": "{PROMPT}"}],
                [{"text": "{PROMPT}"}],
                [{"text": "{PROMPT}"}],
            ],
        },
        workflow_config={"nodes": [{"id": 1, "type": "CLIPTextEncode"}]},
        workflow={"1": {"class_type": "CLIPTextEncode", "inputs": {"text": "x"}}},
        story={
            "prompts": [
                "Panel A with {GENDER_M_AGE_30_RACE_ASIAN}.",
                "Panel B near {GENDER_M_AGE_30_RACE_ASIAN}.",
                "Panel C beside {GENDER_M_AGE_30_RACE_ASIAN}.",
            ]
        },
        character_json=_SINGLE_LOOK_CHARACTER_JSON,
        rng=random.Random(0),
    )

    prepared = builder.prepare("t")

    texts = [next(f["text"] for f in panel if "text" in f) for panel in prepared.panels]
    assert all("{GENDER" not in text for text in texts)
    assert texts == [
        f"Panel A with {_COMPOSED_SINGLE_LOOK}.",
        f"Panel B near {_COMPOSED_SINGLE_LOOK}.",
        f"Panel C beside {_COMPOSED_SINGLE_LOOK}.",
    ]


def test_prepare_enumerated_description_wins_over_random_composition(
    tmp_path: Path,
) -> None:
    """An enumerated description is used verbatim even though the token *could*
    be composed — the authored look stays byte-for-byte stable."""
    builder = _write_assets(
        tmp_path,
        template={"workflow_id": "w", "story": "s", "panels": [[{"text": "{PROMPT}"}]]},
        workflow_config={"nodes": [{"id": 1, "type": "CLIPTextEncode"}]},
        workflow={"1": {"class_type": "CLIPTextEncode", "inputs": {"text": "x"}}},
        story={"prompts": ["Here is {GENDER_F_AGE_30_RACE_ASIAN}."]},
        character_json=_SINGLE_LOOK_CHARACTER_JSON,
        rng=random.Random(0),
    )

    prepared = builder.prepare("t")

    text = next(f["text"] for f in prepared.panels[0] if "text" in f)
    assert text == "Here is the enumerated woman."


def test_prepare_raises_on_character_token_that_resolves_to_nothing(
    tmp_path: Path,
) -> None:
    """A ``GENDER_…_AGE_…``-shaped token naming an unknown dimension (here an age
    the table doesn't define) must fail the job terminally rather than ship the
    literal ``{TOKEN}`` to the image model — which would otherwise paint a
    different stranger in every panel."""
    builder = _write_assets(
        tmp_path,
        template={"workflow_id": "w", "story": "s", "panels": [[{"text": "{PROMPT}"}]]},
        workflow_config={"nodes": [{"id": 1, "type": "CLIPTextEncode"}]},
        workflow={"1": {"class_type": "CLIPTextEncode", "inputs": {"text": "x"}}},
        story={"prompts": ["A stranger {GENDER_M_AGE_99} walks by."]},
        character_json=_SINGLE_LOOK_CHARACTER_JSON,
        rng=random.Random(0),
    )

    with pytest.raises(UnsupportedTemplateError, match="GENDER_M_AGE_99"):
        builder.prepare("t")


def test_compose_uses_child_noun_and_multiword_race() -> None:
    text = _compose_random_character(
        "GENDER_F_AGE_08_RACE_SOUTH_ASIAN",
        _SINGLE_LOOK_CHARACTER_JSON,
        random.Random(0),
    )
    assert text == (
        "an 8-year-old South Asian girl with short black hair, "
        "an average build, wearing a blue shirt, with a kind smile"
    )


def test_compose_resolves_race_with_trailing_disambiguator() -> None:
    # ``..._RACE_ASIAN_PARENT`` resolves to the ASIAN race adjective; the
    # ``_PARENT`` suffix only keeps the token distinct from the plain config.
    text = _compose_random_character(
        "GENDER_M_AGE_30_RACE_ASIAN_PARENT",
        _SINGLE_LOOK_CHARACTER_JSON,
        random.Random(0),
    )
    assert text == _COMPOSED_SINGLE_LOOK


# A composed look from ``_SINGLE_LOOK_CHARACTER_JSON`` for either of its two
# defined races — the only thing that varies when the token omits ``_RACE_``.
_COMPOSED_RACELESS = {
    f"a 30-year-old {adj} man with short black hair, "
    "an average build, wearing a blue shirt, with a kind smile"
    for adj in ("East Asian", "South Asian")
}


def test_compose_picks_a_random_race_when_token_omits_race() -> None:
    # ``GENDER_M_AGE_30`` has no ``_RACE_`` segment, so a race is drawn from the
    # dimensions table (one of the two defined here); the rest is the fixed look.
    text = _compose_random_character(
        "GENDER_M_AGE_30", _SINGLE_LOOK_CHARACTER_JSON, random.Random(0)
    )
    assert text in _COMPOSED_RACELESS


def test_compose_random_race_varies_across_seeds() -> None:
    # Both defined races turn up across seeds → the race really is drawn at random.
    looks = {
        _compose_random_character(
            "GENDER_M_AGE_30", _SINGLE_LOOK_CHARACTER_JSON, random.Random(seed)
        )
        for seed in range(12)
    }
    assert looks == _COMPOSED_RACELESS


def test_compose_raceless_token_with_disambiguator_suffix() -> None:
    # A trailing suffix that is *not* ``_RACE_...`` (e.g. ``_FRIEND2``) only keeps
    # the token distinct; the race is still drawn at random and composition works.
    text = _compose_random_character(
        "GENDER_M_AGE_30_FRIEND2", _SINGLE_LOOK_CHARACTER_JSON, random.Random(0)
    )
    assert text in _COMPOSED_RACELESS


def test_prepare_composes_one_random_race_held_across_panels(tmp_path: Path) -> None:
    """A race-free token draws its race once per job, so every panel of the story
    shows the *same* composed character (race included)."""
    builder = _write_assets(
        tmp_path,
        template={
            "workflow_id": "w",
            "story": "s",
            "panels": [
                [{"text": "{PROMPT}"}],
                [{"text": "{PROMPT}"}],
                [{"text": "{PROMPT}"}],
            ],
        },
        workflow_config={"nodes": [{"id": 1, "type": "CLIPTextEncode"}]},
        workflow={"1": {"class_type": "CLIPTextEncode", "inputs": {"text": "x"}}},
        story={
            "prompts": [
                "A {GENDER_M_AGE_30}.",
                "B {GENDER_M_AGE_30}.",
                "C {GENDER_M_AGE_30}.",
            ]
        },
        character_json=_SINGLE_LOOK_CHARACTER_JSON,
        rng=random.Random(0),
    )

    prepared = builder.prepare("t")

    texts = [next(f["text"] for f in panel if "text" in f) for panel in prepared.panels]
    assert all("{GENDER" not in text for text in texts)
    # Identical description (same randomly-picked race) in every panel.
    descriptions = {text[2:] for text in texts}  # drop the "A "/"B "/"C " prefix
    assert len(descriptions) == 1
    assert descriptions.pop() in {f"{look}." for look in _COMPOSED_RACELESS}


@pytest.mark.parametrize(
    "token",
    [
        "INPUT_1_AGE",  # not a character config at all
        "USER_ID",
        "GENDER_X_AGE_30_RACE_ASIAN",  # gender not in dimensions
        "GENDER_M_AGE_99_RACE_ASIAN",  # age not in dimensions
        "GENDER_M_AGE_30_RACE_MARTIAN",  # race matches no key
    ],
)
def test_compose_returns_none_for_non_character_or_unknown_dims(token: str) -> None:
    assert (
        _compose_random_character(token, _SINGLE_LOOK_CHARACTER_JSON, random.Random(0))
        is None
    )


def test_compose_omits_look_segments_when_tables_absent() -> None:
    """With no hair/build/wardrobe/features tables, the look + features clauses
    are dropped — the description is still grammatical."""
    data = {
        "dimensions": {
            "gender": {"M": {"noun": "man", "noun_child": "boy"}},
            "age": {"30": {"phrase": "a 30-year-old", "child": False}},
            "race": {"ASIAN": {"adj": "East Asian"}},
        }
    }
    text = _compose_random_character(
        "GENDER_M_AGE_30_RACE_ASIAN", data, random.Random(0)
    )
    assert text == "a 30-year-old East Asian man"


def test_resolve_race_key_longest_match_or_none() -> None:
    table = {"ASIAN": {}, "SOUTH_ASIAN": {}}
    assert _resolve_race_key("SOUTH_ASIAN", table) == "SOUTH_ASIAN"
    assert _resolve_race_key("ASIAN_PARENT", table) == "ASIAN"
    assert _resolve_race_key("MARTIAN", table) is None


def test_compose_is_reproducible_under_a_fixed_seed() -> None:
    token = "GENDER_M_AGE_30_RACE_ASIAN"
    first = _compose_random_character(
        token, _MULTI_LOOK_CHARACTER_JSON, random.Random(7)
    )
    second = _compose_random_character(
        token, _MULTI_LOOK_CHARACTER_JSON, random.Random(7)
    )
    assert first == second


def test_compose_varies_across_seeds() -> None:
    token = "GENDER_M_AGE_30_RACE_ASIAN"
    looks = {
        _compose_random_character(
            token, _MULTI_LOOK_CHARACTER_JSON, random.Random(seed)
        )
        for seed in range(12)
    }
    assert len(looks) > 1


# A character.json whose look tables mix child/adult and masc/fem/neutral
# fragments. Each gender/age value carries an ``avoid`` list; the keys reserved
# to each group live under ``restrictions`` — the schema workflow.py filters on.
_TAGGED_CHARACTER_JSON: dict[str, Any] = {
    "dimensions": {
        "gender": {
            "M": {"noun": "man", "noun_child": "boy", "avoid": ["fem_only"]},
            "F": {"noun": "woman", "noun_child": "girl", "avoid": ["masc_only"]},
            "NB": {
                "noun": "person",
                "noun_child": "child",
                "avoid": ["masc_only", "fem_only"],
            },
        },
        "age": {
            # Three-way age band: child / middle adult / elderly. Only the
            # elderly age may draw ``elderly_only`` fragments (grey hair); a
            # child and a middle adult both avoid them.
            "30": {
                "phrase": "a 30-year-old",
                "child": False,
                "avoid": ["child_only", "elderly_only"],
            },
            "06": {
                "phrase": "a 6-year-old",
                "child": True,
                "avoid": ["adult_only", "elderly_only"],
            },
            "70": {"phrase": "an elderly", "child": False, "avoid": ["child_only"]},
        },
        "race": {"ASIAN": {"adj": "East Asian"}},
    },
    "hair": {
        "PLAIN_HAIR": "plain hair",
        "A_BUN": "a neat bun",
        "A_BUZZ": "a buzz cut",
        "A_GREY": "short grey hair",
    },
    "build": {"CHILD_SMALL": "a small frame", "ADULT_TALL": "a tall build"},
    "wardrobe": {
        "SCHOOL": "a school uniform",
        "SUIT": "a business suit",
        "A_DRESS": "a floral dress",
        "A_FLANNEL": "a plaid flannel shirt",
        "A_TEE": "a plain tee",
    },
    "features": {"A_SMILE": "a warm smile", "A_BEARD": "a full beard"},
    "restrictions": {
        "child_only": {"build": ["CHILD_SMALL"], "wardrobe": ["SCHOOL"]},
        "adult_only": {
            "build": ["ADULT_TALL"],
            "wardrobe": ["SUIT"],
            "features": ["A_BEARD"],
        },
        "masc_only": {
            "hair": ["A_BUZZ"],
            "wardrobe": ["A_FLANNEL"],
            "features": ["A_BEARD"],
        },
        "fem_only": {"hair": ["A_BUN"], "wardrobe": ["A_DRESS"]},
        "elderly_only": {"hair": ["A_GREY"]},
    },
}


_GENDERED_HAIR_CHARACTER_JSON: dict[str, Any] = {
    "dimensions": {
        "gender": {
            "M": {"noun": "man", "noun_child": "boy", "avoid": ["fem_only"]},
            "F": {"noun": "woman", "noun_child": "girl", "avoid": ["masc_only"]},
            "NB": {
                "noun": "person",
                "noun_child": "child",
                "avoid": ["masc_only", "fem_only"],
            },
        },
        "age": {
            "30": {"phrase": "a 30-year-old", "child": False, "avoid": []},
            "70": {"phrase": "an elderly", "child": False, "avoid": []},
        },
        "race": {"ASIAN": {"adj": "East Asian"}},
    },
    "hair": {
        "SHORT_NEUTRAL": "short neutral hair",
        "LONG_FEM": "long wavy hair",
        "MAN_BUN": "a long man bun",
        "BUZZ": "a buzz cut",
        "GREY_BUN": "a grey bun",
        "SHORT_GREY": "short grey hair",
    },
    "hair_by_gender": {
        "M": ["SHORT_NEUTRAL", "BUZZ", "SHORT_GREY"],
        "F": ["LONG_FEM", "GREY_BUN"],
        "NB": ["SHORT_NEUTRAL"],
    },
    "restrictions": {
        "masc_only": {"hair": ["BUZZ", "SHORT_GREY"]},
        "fem_only": {"hair": ["LONG_FEM", "GREY_BUN"]},
        "elderly_only": {"hair": ["GREY_BUN", "SHORT_GREY"]},
    },
}


def _looks(token: str, n: int = 40) -> list[str]:
    return [
        _compose_random_character(token, _TAGGED_CHARACTER_JSON, random.Random(seed))
        for seed in range(n)
    ]


def _gendered_hair_looks(token: str, n: int = 40) -> list[str]:
    return [
        _compose_random_character(
            token, _GENDERED_HAIR_CHARACTER_JSON, random.Random(seed)
        )
        for seed in range(n)
    ]


def test_compose_limits_male_hair_to_gender_classification() -> None:
    looks = _gendered_hair_looks("GENDER_M_AGE_30_RACE_ASIAN")

    assert all("long wavy hair" not in look for look in looks)
    assert all("a grey bun" not in look for look in looks)
    assert all("a long man bun" not in look for look in looks)
    assert any("short neutral hair" in look for look in looks)
    assert any("a buzz cut" in look for look in looks)


def test_compose_limits_female_hair_to_gender_classification() -> None:
    looks = _gendered_hair_looks("GENDER_F_AGE_30_RACE_ASIAN")

    assert all("short neutral hair" not in look for look in looks)
    assert all("a buzz cut" not in look for look in looks)
    assert all("short grey hair" not in look for look in looks)
    assert any("long wavy hair" in look for look in looks)
    assert any("a grey bun" in look for look in looks)


def test_compose_limits_nonbinary_hair_to_gender_classification() -> None:
    looks = _gendered_hair_looks("GENDER_NB_AGE_30_RACE_ASIAN")

    assert all("short neutral hair" in look for look in looks)
    assert all("long wavy hair" not in look for look in looks)
    assert all("a buzz cut" not in look for look in looks)


def test_compose_omits_hair_when_gender_classification_is_stale() -> None:
    data = {
        **_GENDERED_HAIR_CHARACTER_JSON,
        "hair_by_gender": {"M": ["MISSING_HAIR"]},
    }

    text = _compose_random_character(
        "GENDER_M_AGE_30_RACE_ASIAN", data, random.Random(0)
    )

    assert text == "a 30-year-old East Asian man"


def test_compose_child_token_never_draws_adult_only_fragments() -> None:
    looks = _looks("GENDER_M_AGE_06_RACE_ASIAN")
    # CHILD_SMALL is the only non-adult build → always chosen for a child.
    assert all("a small frame" in look for look in looks)
    assert all("a tall build" not in look for look in looks)
    assert all("a business suit" not in look for look in looks)  # adult-only
    assert any("a school uniform" in look for look in looks)


def test_compose_adult_token_never_draws_child_only_fragments() -> None:
    looks = _looks("GENDER_M_AGE_30_RACE_ASIAN")
    assert all("a tall build" in look for look in looks)
    assert all("a small frame" not in look for look in looks)
    assert all("a school uniform" not in look for look in looks)  # child-only
    assert any("a business suit" in look for look in looks)


def test_compose_middle_adult_token_never_draws_elderly_only_fragments() -> None:
    # Age is a three-way band, not binary: a 30-year-old is an adult but not
    # elderly, so it draws ``adult_only`` looks yet still avoids ``elderly_only``
    # ones (grey hair). This is the case the old child/adult split got wrong.
    looks = _looks("GENDER_F_AGE_30_RACE_ASIAN")
    assert all("short grey hair" not in look for look in looks)
    assert any("a business suit" in look for look in looks)  # adult-only is fine


def test_compose_child_token_never_draws_elderly_only_fragments() -> None:
    looks = _looks("GENDER_M_AGE_06_RACE_ASIAN")
    assert all("short grey hair" not in look for look in looks)


def test_compose_elderly_token_may_draw_elderly_only_fragments() -> None:
    # Only the elderly band unlocks ``elderly_only`` looks; the neutral grey
    # hair is reserved to no gender group, so any elderly character can roll it.
    looks = _looks("GENDER_F_AGE_70_RACE_ASIAN")
    assert any("short grey hair" in look for look in looks)


def test_compose_woman_token_never_draws_masc_only_fragments() -> None:
    looks = _looks("GENDER_F_AGE_30_RACE_ASIAN")
    assert all("a buzz cut" not in look for look in looks)  # masc-only hair
    assert all("a plaid flannel shirt" not in look for look in looks)  # masc wardrobe
    assert all("a full beard" not in look for look in looks)  # masc feature
    assert any("a neat bun" in look for look in looks)  # fem hair is fine
    assert any("a floral dress" in look for look in looks)  # fem wardrobe is fine


def test_compose_man_token_never_draws_fem_only_fragments() -> None:
    looks = _looks("GENDER_M_AGE_30_RACE_ASIAN")
    assert all("a neat bun" not in look for look in looks)  # fem-only hair
    assert all("a floral dress" not in look for look in looks)  # fem-only wardrobe
    assert any("a buzz cut" in look for look in looks)  # masc hair is fine
    assert any("a full beard" in look for look in looks)  # masc feature is fine


def test_compose_nonbinary_token_draws_only_unisex_fragments() -> None:
    looks = _looks("GENDER_NB_AGE_30_RACE_ASIAN")
    # NB avoids both gendered groups → only the neutral hair survives.
    assert all("plain hair" in look for look in looks)
    assert all("a buzz cut" not in look for look in looks)  # masc
    assert all("a neat bun" not in look for look in looks)  # fem
    assert all("a floral dress" not in look for look in looks)  # fem
    assert all("a plaid flannel shirt" not in look for look in looks)  # masc
    assert all("a full beard" not in look for look in looks)  # masc


def test_compose_ignores_restriction_when_it_empties_a_table() -> None:
    """If every option in a table is reserved away from the character, the
    restriction is dropped for that table rather than yielding nothing."""
    data = {
        "dimensions": {
            "gender": {
                "M": {"noun": "man", "noun_child": "boy", "avoid": ["fem_only"]}
            },
            "age": {
                "06": {"phrase": "a 6-year-old", "child": True, "avoid": ["adult_only"]}
            },
            "race": {"ASIAN": {"adj": "East Asian"}},
        },
        # Both builds are adult-only, but the subject is a child → fall back.
        "build": {"ADULT_A": "build a", "ADULT_B": "build b"},
        "restrictions": {"adult_only": {"build": ["ADULT_A", "ADULT_B"]}},
    }

    text = _compose_random_character(
        "GENDER_M_AGE_06_RACE_ASIAN", data, random.Random(0)
    )

    assert text is not None
    assert "build a" in text or "build b" in text


# --- render: happy -----------------------------------------------------------


def test_render_applies_panel_values_and_placeholders(
    real_builder: WorkflowBuilder,
) -> None:
    prepared = real_builder.prepare("1")

    workflow = real_builder.render(
        prepared,
        prepared.panels[0],
        placeholders={"USER_ID": "u1", "STORY_ID": "s1"},
    )

    # Image filename is substituted to the per-story upload name (no remap).
    assert workflow[_NODE_INPUT_IMAGE]["inputs"]["image"] == "u1_s1_INPUT_1.png"
    assert workflow[_NODE_SOURCE_FACE]["inputs"]["image"] == "u1_s1_INPUT_1.png"
    # Panel 0's filename prefixes carry the per-panel P0 marker.
    assert workflow[_NODE_SAVE_V1]["inputs"]["filename_prefix"] == "u1_s1_P0_V1"
    assert workflow[_NODE_SAVE_V2]["inputs"]["filename_prefix"] == "u1_s1_P0_V2"


def test_render_keeps_template_defaults_and_unbound_prompt_sentinel(
    real_builder: WorkflowBuilder,
) -> None:
    # No story bound → the prompt field still carries the {PROMPT} sentinel
    # (it is only replaced when prepare() is given a story_ref).
    prepared = real_builder.prepare("1")

    workflow = real_builder.render(
        prepared,
        prepared.panels[0],
        placeholders={"USER_ID": "u1", "STORY_ID": "s1"},
    )

    assert workflow[_NODE_PROMPT]["inputs"]["text"] == "{PROMPT}"
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

    first = builder.render(prepared, prepared.panels[0], placeholders={})
    second = builder.render(prepared, prepared.panels[1], placeholders={})

    assert first["1"]["inputs"]["text"] == "scene one"
    assert first["2"]["inputs"]["noise_seed"] == 11
    assert second["1"]["inputs"]["text"] == "scene two"
    assert second["2"]["inputs"]["noise_seed"] == 22


def test_render_does_not_mutate_the_base_workflow(
    real_builder: WorkflowBuilder,
) -> None:
    prepared = real_builder.prepare("1")
    placeholders = {"USER_ID": "u", "STORY_ID": "s"}

    first = real_builder.render(prepared, prepared.panels[0], placeholders=placeholders)
    second = real_builder.render(
        prepared, prepared.panels[1], placeholders=placeholders
    )

    # Each rendered panel carries its own template seed …
    assert first[_NODE_SEED]["inputs"]["noise_seed"] == 771062815410683
    assert second[_NODE_SEED]["inputs"]["noise_seed"] == 771062815410684
    # … and the shared base workflow is never mutated (deep-copied per render).
    assert (
        prepared.base_workflow[_NODE_SEED]["inputs"]["noise_seed"]
        == _TEMPLATE_DEFAULT_SEED
    )


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
        builder.render(prepared, prepared.panels[0], placeholders={})


def test_render_raises_when_node_lacks_the_paneled_input(tmp_path: Path) -> None:
    builder = _write_assets(
        tmp_path,
        template={"workflow_id": "w", "panels": [[{"missing_field": 1}]]},
        workflow_config={"nodes": [{"id": 1, "type": "X"}]},
        workflow={"1": {"class_type": "X", "inputs": {}}},
    )
    prepared = builder.prepare("t")

    with pytest.raises(UnsupportedTemplateError, match="no input 'missing_field'"):
        builder.render(prepared, prepared.panels[0], placeholders={})


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
