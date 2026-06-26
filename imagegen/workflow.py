"""Render a ComfyUI API-format prompt from a stored workflow + template.

Ports the customization logic from the legacy ImageGenCp ``WorkflowService``
(``../ImageGenCp/src/services/workflow.py``) into the stateless worker. This
module does no network / ComfyUI / clock I/O — just JSON loading and field
substitution — which keeps it unit-testable without a running container. Its
one source of nondeterminism is the random *look* it rolls for un-enumerated
character tokens (see :func:`_compose_random_character`); that draw goes
through an injectable ``rng`` so tests can pin it.

Vocabulary (unchanged from the legacy service):

* A **workflow** (``workflows/<id>/workflow.json``) is a ComfyUI graph exported
  in *API format* — a dict keyed by node id, each value ``{class_type, inputs,
  _meta}``.
* Its companion ``workflows/<id>/config.json`` lists, *positionally*, which
  nodes a template may customize: ``{"nodes": [{"id": .., "type": ..}, ..]}``.
* A **template** (``templates/<id>/config.json``) carries one or more
  **panels**. Each panel is a list parallel to the workflow config's node list;
  every entry is a ``{field_name: value}`` dict written into that node's
  ``inputs``. **One panel == one ComfyUI run == one output image** (DESIGN.md
  §7.2). A story with N scenes is N panels; the worker renders + submits each
  panel in turn.
* :meth:`prepare` sources each panel's prompt from ``prompts/<story_ref>.json``
  (an ordered array, one prompt per panel). ``story_ref`` is passed in — for the
  single render template (``templates/1``) it comes from the job's ``type``/
  ``id`` (e.g. ``"1_1"``); a legacy template may instead carry its own
  ``"story"`` field. Those prompts may contain character ``{TOKEN}``
  placeholders (e.g. ``{GENDER_F_AGE_70}`` or ``{GENDER_F_AGE_70_RACE_ASIAN}``)
  which :meth:`prepare` resolves against ``prompts/character.json`` so the same
  generated character looks identical across every panel of the story. A
  character token that has *no* enumerated description (only the
  ``GENDER_<g>_AGE_<a>`` config baked into the token, with an optional
  ``_RACE_<r>``) instead gets a look composed on the fly — the race (when the
  token omits ``_RACE_``) plus the hair, build, wardrobe and features all picked
  at random from ``character.json``'s modular tables, once per job so it too
  stays identical across the panels.
  ``USER_ID`` / ``STORY_ID`` are still resolved later, at :meth:`render` time,
  since they are per-job.

Image filenames in a panel carry ``USER_ID`` / ``STORY_ID`` placeholders
(e.g. ``USER_ID_STORY_ID_INPUT_1.png``). After substitution they become the
*per-story* filenames the worker uploads the input photos under — so the
rendered ``LoadImage`` node references exactly the name ComfyUI stored, with no
separate remap step (the model uploads under the substituted slot name).

A missing or malformed workflow/template is treated as
:class:`~imagegen.failure_classification.UnsupportedTemplateError` — a corrupt
asset is a deploy bug the worker can't run, never a transient condition.
"""

from __future__ import annotations

import copy
import json
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .failure_classification import UnsupportedTemplateError

# SaveImage nodes whose substituted ``filename_prefix`` ends with this marker
# hold the *final* image we return to the caller. workflows/1 emits two:
# ``..._V1`` (pre-face-swap) and ``..._V2`` (face-restored). We collect V2.
FINAL_OUTPUT_SUFFIX = "_V2"

# Sentinel a story-bound template's prompt field carries to mark *where* the
# per-panel story prompt is injected. ``prepare`` replaces it (per panel) with
# the resolved prompt. Keying off this placeholder — rather than a hard-coded
# field name like ``text`` — lets the prompt land in whatever input the node
# actually exposes (``text`` on ``CLIPTextEncode``, ``prompt`` on
# ``TextEncodeQwenImageEditPlus``, …); the field name lives in the template, not
# in this module.
PROMPT_PLACEHOLDER = "{PROMPT}"
NEGATIVE_PROMPT_PLACEHOLDER = "{NEGATIVE_PROMPT}"

# A supporting-character placeholder encodes its *fixed* traits in the token
# itself — ``GENDER_<g>_AGE_<a>`` with an **optional** ``_RACE_<r>`` segment —
# plus an optional trailing ``_<suffix>`` that only disambiguates two otherwise
# identical configs (e.g. ``..._RACE_ASIAN_PARENT``, or race-free
# ``GENDER_F_AGE_06_FRIEND2``). Everything after gender/age is captured as
# ``rest`` and split into race vs. disambiguator in code: when ``rest`` carries
# no ``_RACE_`` segment the race is picked at random per job, and when the token
# has no enumerated description in character.json the rest of the *look* (hair,
# build, wardrobe, features) is filled in at random too.
_RACE_PREFIX = "_RACE_"
_CHARACTER_TOKEN_RE = re.compile(
    r"^GENDER_(?P<gender>[A-Z]+)_AGE_(?P<age>[A-Z0-9]+)(?P<rest>_[A-Z0-9_]+)?$"
)
# Any ``{TOKEN}`` in a prompt string. Used to discover which character tokens a
# story references so the un-enumerated ones can be composed.
_PLACEHOLDER_RE = re.compile(r"\{([A-Z0-9_]+)\}")

# Facial-hair feature keys. Adult men are forced to a *definite* beard state
# (see :func:`_compose_random_character`), so these never enter their random
# feature draw; the other demographics already exclude them via ``restrictions``.
_FACIAL_HAIR_KEYS = ("STUBBLE", "FULL_BEARD", "NEAT_MUSTACHE", "NO_BEARD")
# The two states an adult man is forced into, 50/50, so his facial hair stays
# identical across a story's panels instead of being improvised per panel.
_BEARD_STATE_KEYS = ("FULL_BEARD", "NO_BEARD")


@dataclass(frozen=True)
class PreparedTemplate:
    """Everything needed to render a template's workflow, loaded once.

    Reused across the per-panel runs of a single job so the JSON files are
    read (and validated) exactly once per job, not once per panel.
    """

    template_id: str
    workflow_id: str
    config_nodes: list[dict[str, Any]]
    panels: list[list[dict[str, Any]]]
    base_workflow: dict[str, Any]
    image_slots: list[str]
    """Distinct ``image`` placeholder filenames across all panels, in first-seen
    order. Each is one input slot the caller fills with an uploaded photo (the
    filename is the *pre-substitution* template value, e.g.
    ``USER_ID_STORY_ID_INPUT_1.png``)."""

    @property
    def panel_count(self) -> int:
        return len(self.panels)


def _substitute(value: Any, placeholders: dict[str, str]) -> Any:
    """Replace every ``placeholder`` substring in a string value; pass others through."""
    if not isinstance(value, str):
        return value
    for placeholder, replacement in placeholders.items():
        value = value.replace(placeholder, replacement)
    return value


def _distinct_tokens(prompts: list[str]) -> list[str]:
    """Every ``{TOKEN}`` across ``prompts``, deduped in first-seen order."""
    seen: list[str] = []
    for prompt in prompts:
        for token in _PLACEHOLDER_RE.findall(prompt):
            if token not in seen:
                seen.append(token)
    return seen


def _resolve_race_key(race: str, race_table: dict[str, Any]) -> str | None:
    """Longest key in ``race_table`` that the token's race segment names.

    Race keys carry underscores (``SOUTH_ASIAN``) and a token may append a
    disambiguator (``..._RACE_ASIAN_PARENT`` → race segment ``ASIAN_PARENT``),
    so the segment is matched as a key prefix rather than an exact key. Returns
    ``None`` when nothing matches.
    """
    candidates = [
        key for key in race_table if race == key or race.startswith(f"{key}_")
    ]
    return max(candidates, key=len) if candidates else None


def _compose_random_character(
    token: str, data: dict[str, Any], rng: random.Random
) -> str | None:
    """Compose a description for a ``GENDER_<g>_AGE_<a>`` character token.

    The fixed traits (gender / age / race) are read from the token; the hair,
    build, wardrobe and features are drawn **at random** from ``character.json``'s
    modular tables and assembled with the same recipe the enumerated
    descriptions use::

        {age} {ethnicity} {gender_noun} with {hair}, {build},
            wearing {wardrobe}[, with {features}]

    The draw is **age- and gender-aware**: hair is first limited to the
    ``hair_by_gender`` list for the token's gender, then each fragment table is
    filtered before the random pick to the keys this character may use. Every
    dimension value (the matched age and gender entry) lists under ``avoid`` the
    ``restrictions`` groups whose fragments it must skip. Age is a three-way
    band, not binary — a child avoids ``adult_only`` *and* ``elderly_only`` (a
    business suit, a grey bun …); a middle adult avoids ``child_only`` *and*
    ``elderly_only`` (so a 25-year-old never rolls grey/thinning hair); only the
    elderly avoid ``child_only`` alone, unlocking ``elderly_only`` looks. A man
    avoids ``fem_only`` (a dress, pigtails …), a non-binary character avoids both
    gendered groups, and so on. A fragment in no avoided group suits everyone;
    if a table is emptied entirely, its filter is dropped rather than yielding
    nothing.

    Adult men get one extra rule on top of the random draw: their **beard state
    is forced** to a definite value -- ``FULL_BEARD`` or ``NO_BEARD`` (clean-
    shaven), 50/50 -- because the model otherwise improvises facial hair
    differently in each panel. Stubble/mustache are excluded from their regular
    feature draw so the forced beard line never contradicts them; any non-facial
    feature still drawn is kept and the beard appended to it.

    The ``_RACE_<r>`` segment is **optional**: a token that omits it (e.g.
    ``GENDER_F_AGE_70``) draws a race at random from the ``dimensions.race``
    table for this job — picked once here, so it stays identical across the
    panels — while a token that names a race (``GENDER_F_AGE_70_RACE_ASIAN``)
    pins that exact one.

    Returns ``None`` when ``token`` isn't shaped like a character config, or
    names a gender / age / race the ``dimensions`` table doesn't define — the
    caller then leaves the placeholder untouched rather than failing the job.
    """
    match = _CHARACTER_TOKEN_RE.match(token)
    if match is None:
        return None

    dimensions = data.get("dimensions", {})
    gender = dimensions.get("gender", {}).get(match["gender"])
    age = dimensions.get("age", {}).get(match["age"])
    race_table = dimensions.get("race", {})
    if not (gender and age and race_table):
        return None

    rest = match["rest"]
    if rest and rest.startswith(_RACE_PREFIX):
        race_key = _resolve_race_key(rest[len(_RACE_PREFIX) :], race_table)
        if race_key is None:
            return None
    else:
        # No ``_RACE_`` segment in the token → pick a race at random for this job
        # (any trailing ``rest`` is a bare disambiguator, not a race).
        race_key = rng.choice(sorted(race_table))
    race = race_table[race_key]

    # The restriction groups this character must skip, gathered from its age and
    # gender (e.g. a child woman avoids ``adult_only`` + ``elderly_only`` +
    # ``masc_only``).
    restrictions = data.get("restrictions", {})
    avoid_groups = list(gender.get("avoid", [])) + list(age.get("avoid", []))

    def pick(table_name: str, allowed_keys: list[str] | None = None) -> str | None:
        table = data.get(table_name, {})
        if not table:
            return None
        if allowed_keys is not None:
            allowed = {key for key in allowed_keys if key in table}
            table = {key: table[key] for key in allowed}
            if not table:
                return None
        excluded = {
            key
            for group in avoid_groups
            for key in restrictions.get(group, {}).get(table_name, [])
        }
        candidates = sorted(key for key in table if key not in excluded)
        if not candidates:  # whole table filtered away → ignore the filter
            candidates = sorted(table)
        return str(table[rng.choice(candidates)])

    hair_by_gender = data.get("hair_by_gender", {})
    hair_keys = (
        hair_by_gender.get(match["gender"])
        if isinstance(hair_by_gender, dict)
        else None
    )
    hair = pick("hair", hair_keys if isinstance(hair_keys, list) else None)
    build = pick("build")
    wardrobe = pick("wardrobe")
    if match["gender"] == "M" and not age.get("child"):
        # Adult men render inconsistent facial hair across a story's panels
        # unless the beard state is stated outright, so force every adult man to
        # one definite state -- a full beard or clean-shaven (50/50) -- instead
        # of leaving it to the random draw (which might never mention facial hair
        # at all). Stubble/mustache are dropped from the regular feature draw for
        # them so the forced line can't contradict it; any non-facial feature
        # drawn (glasses, freckles, ...) is kept and the beard appended to it.
        feature_table = data.get("features", {})
        non_beard = pick(
            "features", [k for k in feature_table if k not in _FACIAL_HAIR_KEYS]
        )
        states = [k for k in _BEARD_STATE_KEYS if k in feature_table]
        beard = str(feature_table[rng.choice(states)]) if states else None
        features = (
            f"{non_beard} and {beard}" if non_beard and beard else (beard or non_beard)
        )
    else:
        features = pick("features")

    noun = gender.get("noun_child") if age.get("child") else gender.get("noun")
    text = f"{age['phrase']} {race['adj']} {noun}"
    look: list[str] = []
    if hair:
        look.append(f"with {hair}")
    if build:
        look.append(build)
    if wardrobe:
        look.append(f"wearing {wardrobe}")
    if look:
        text += " " + ", ".join(look)
    if features:
        text += f", with {features}"
    return text


class WorkflowBuilder:
    """Loads workflow/template assets and renders submit-ready prompts."""

    def __init__(
        self,
        workflow_root: Path,
        template_root: Path,
        prompts_root: Path | None = None,
        *,
        rng: random.Random | None = None,
    ) -> None:
        self._workflow_root = workflow_root
        self._template_root = template_root
        # Story prompt sets + the character placeholder library live in
        # ``imagegen/prompts/`` (a sibling of templates/). Defaulting from the
        # template root keeps existing two-arg callers (and tests) working.
        self._prompts_root = (
            prompts_root
            if prompts_root is not None
            else template_root.parent / "prompts"
        )
        # Rolls the look of un-enumerated character tokens. A single instance is
        # reused across the builder's lifetime (one worker process) so each job
        # gets a fresh look while every panel of one job stays consistent.
        # Tests inject a seeded ``Random`` to pin the draw.
        self._rng = rng if rng is not None else random.Random()

    # -- loading ----------------------------------------------------------

    def _load_json(self, path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text())
        except FileNotFoundError as exc:
            raise UnsupportedTemplateError(f"missing asset: {path}") from exc
        except json.JSONDecodeError as exc:
            raise UnsupportedTemplateError(f"invalid JSON in {path}: {exc}") from exc

    def prepare(
        self, template_id: str, story_ref: str | None = None
    ) -> PreparedTemplate:
        """Load + validate a template and its workflow, ready for :meth:`render`.

        ``story_ref`` selects which ``prompts/<story_ref>.json`` set fills the
        panels' ``text`` fields. The single render template (``templates/1``) no
        longer binds a story inline, so the caller passes it from the job's
        ``type``/``id`` (e.g. ``"1_1"``); if omitted, a legacy template's own
        ``"story"`` field is used.

        Raises :class:`UnsupportedTemplateError` if any asset is missing, if the
        template names no workflow, has no panels, or *any* panel doesn't line
        up positionally with the workflow config's node list.
        """
        template = self._load_json(self._template_root / template_id / "config.json")
        workflow_id = template.get("workflow_id")
        if not workflow_id:
            raise UnsupportedTemplateError(
                f"template {template_id!r} has no workflow_id"
            )

        config = self._load_json(self._workflow_root / workflow_id / "config.json")
        config_nodes = config.get("nodes", [])

        panels = template.get("panels", [])
        if not panels:
            raise UnsupportedTemplateError(f"template {template_id!r} has no panels")

        for panel_index, panel in enumerate(panels):
            if len(panel) != len(config_nodes):
                raise UnsupportedTemplateError(
                    f"template {template_id!r} panel {panel_index} has {len(panel)} "
                    f"entries but workflow {workflow_id!r} config declares "
                    f"{len(config_nodes)} nodes"
                )

        story_ref = story_ref or template.get("story")
        if story_ref:
            self._apply_story_prompts(template_id, str(story_ref), panels)

        base_workflow = self._load_json(
            self._workflow_root / workflow_id / "workflow.json"
        )

        image_slots: list[str] = []
        for panel in panels:
            for fields in panel:
                if "image" in fields:
                    name = fields["image"]
                    if name not in image_slots:
                        image_slots.append(name)

        return PreparedTemplate(
            template_id=template_id,
            workflow_id=str(workflow_id),
            config_nodes=config_nodes,
            panels=panels,
            base_workflow=base_workflow,
            image_slots=image_slots,
        )

    def _apply_story_prompts(
        self,
        template_id: str,
        story_ref: str,
        panels: list[list[dict[str, Any]]],
    ) -> None:
        """Inject the bound story's per-panel prompts at each panel's ``{PROMPT}``.

        ``prompts/<story_ref>.json`` carries a ``prompts`` array — one prompt per
        panel, in order. Character ``{TOKEN}`` placeholders are resolved here
        against ``prompts/character.json`` (static across a job, so resolve once
        at load), leaving ``USER_ID`` / ``STORY_ID`` for :meth:`render`.

        The resolved prompt replaces the :data:`PROMPT_PLACEHOLDER` sentinel
        wherever it appears in the panel's field *values* — so which node input
        receives it (``text``, ``prompt``, …) is decided by the template, not by
        a field name baked into this module. A prompt/panel count mismatch, a
        panel that carries no ``{PROMPT}`` placeholder, or a missing prompts/
        character asset is a deploy bug → :class:`UnsupportedTemplateError`.
        """
        story = self._load_json(self._prompts_root / f"{story_ref}.json")
        prompts = story.get("prompts", [])
        negative_prompts = story.get("negative_prompts") or []
        if len(prompts) != len(panels):
            raise UnsupportedTemplateError(
                f"template {template_id!r} story {story_ref!r} has {len(prompts)} "
                f"prompts but the template has {len(panels)} panels"
            )
        if len(negative_prompts) not in (0, len(panels)):
            raise UnsupportedTemplateError(
                f"template {template_id!r} story {story_ref!r} has "
                f"{len(negative_prompts)} negative_prompts but the template has "
                f"{len(panels)} panels"
            )
        if not negative_prompts:
            negative_prompts = ["" for _ in panels]

        characters = self._character_substitutions([str(p) for p in prompts], story)
        for panel_index, (panel, prompt, negative_prompt) in enumerate(
            zip(panels, prompts, negative_prompts)
        ):
            resolved = _substitute(str(prompt), characters)
            injected = False
            for fields in panel:
                for key, value in fields.items():
                    if isinstance(value, str) and PROMPT_PLACEHOLDER in value:
                        fields[key] = value.replace(PROMPT_PLACEHOLDER, resolved)
                        injected = True
                    if (
                        isinstance(fields[key], str)
                        and NEGATIVE_PROMPT_PLACEHOLDER in fields[key]
                    ):
                        fields[key] = fields[key].replace(
                            NEGATIVE_PROMPT_PLACEHOLDER, str(negative_prompt)
                        )
            if not injected:
                raise UnsupportedTemplateError(
                    f"template {template_id!r} story {story_ref!r}: panel "
                    f"{panel_index} has no {PROMPT_PLACEHOLDER} placeholder to "
                    "receive a prompt"
                )

    def _character_substitutions(
        self, prompts: list[str], story: dict[str, Any] | None = None
    ) -> dict[str, str]:
        """Map every character ``{TOKEN}`` in ``prompts`` to a description.

        ``prompts/character.json`` is the shared library of generated supporting
        characters (owned by the ``character-config`` skill). Tokens are resolved
        by two paths, in precedence order:

        1. **Enumerated** — ``characters[TOKEN].description`` is used verbatim, so
           an authored character's look stays byte-for-byte identical across every
           panel *and* every job.
        2. **Composed** — a token shaped like ``GENDER_<g>_AGE_<a>`` (with an
           optional ``_RACE_<r>``) but with no enumerated description gets a look
           composed on the fly: the gender / age come from the token (the "config
           in the placeholder"), the race too when ``_RACE_`` is present
           (otherwise one is picked at random); the hair, build, wardrobe and
           features are picked at random from the modular tables (see
           :func:`_compose_random_character`). Each distinct token is composed
           once here, so its random look is the same in every panel of this job.

        Tokens that aren't character-shaped at all (e.g. ``{INPUT_1_AGE}``,
        ``{USER_ID}``, an unknown name) are left out of the map, so the caller's
        substitution passes them through untouched. A token that *does* match the
        ``GENDER_<g>_AGE_<a>`` character shape but resolves to nothing (unknown
        gender/age/race, or assets out of sync with this code) raises
        :class:`UnsupportedTemplateError` — never silently passed through, since
        a literal ``{GENDER_…}`` reaching the image model breaks the supporting
        character's consistency across the story's panels.
        """
        data = self._load_json(self._prompts_root / "character.json")
        if story is not None and story.get("character_file"):
            extra_path = self._prompts_root / str(story["character_file"])
            extra = self._load_json(extra_path)
            merged_characters = {
                **data.get("characters", {}),
                **extra.get("characters", {}),
            }
            data = {**data, "characters": merged_characters}
        substitutions: dict[str, str] = {}
        for token, entry in data.get("characters", {}).items():
            description = entry.get("description") if isinstance(entry, dict) else None
            if description:
                substitutions[f"{{{token}}}"] = str(description)
        for token in _distinct_tokens(prompts):
            braced = f"{{{token}}}"
            if braced in substitutions:
                continue  # enumerated description already won
            composed = _compose_random_character(token, data, self._rng)
            if composed is not None:
                substitutions[braced] = composed
            elif _CHARACTER_TOKEN_RE.match(token):
                # A character-shaped token (``GENDER_<g>_AGE_<a>`` …) that is
                # neither enumerated nor composable names a gender / age / race
                # ``character.json`` doesn't define (a typo, or assets out of sync
                # with this code). Leaving it unresolved would ship the literal
                # ``{TOKEN}`` to the image model, which then paints a *different*
                # stranger in every panel — the supporting cast loses all
                # cross-panel consistency. Fail the job terminally instead of
                # silently producing broken art.
                raise UnsupportedTemplateError(
                    f"character token {token!r} matches the GENDER_…_AGE_… shape "
                    "but resolves to no description in character.json "
                    "(unknown gender/age/race, or stale assets)"
                )
        return substitutions

    # -- rendering --------------------------------------------------------

    def render(
        self,
        prepared: PreparedTemplate,
        panel: list[dict[str, Any]],
        *,
        placeholders: dict[str, str],
    ) -> dict[str, Any]:
        """Return a fresh API-format workflow with ``panel``'s values applied.

        For each ``(config node, panel entry)`` pair the entry's ``{field:
        value}`` items are written into that node's ``inputs`` — the field name
        comes straight from the template, so nothing here is keyed to a specific
        input (``text`` / ``noise_seed`` / …). ``USER_ID`` / ``STORY_ID`` (and any
        other ``placeholders``) are then substituted into the string values; the
        per-panel prompt was already injected at :meth:`prepare` (see
        :meth:`_apply_story_prompts`). Each panel keeps its own ``noise_seed`` /
        ``filename_prefix`` — the base workflow is deep-copied, so rendering one
        panel never bleeds into another.

        Raises :class:`UnsupportedTemplateError` if the workflow lacks a config
        node, or a node lacks an input the panel names.
        """
        workflow = copy.deepcopy(prepared.base_workflow)
        for node_config, fields in zip(prepared.config_nodes, panel):
            node_id = str(node_config["id"])
            node = workflow.get(node_id)
            if node is None:
                raise UnsupportedTemplateError(
                    f"workflow {prepared.workflow_id!r} has no node {node_id!r}"
                )
            inputs = node.setdefault("inputs", {})
            for field, value in fields.items():
                if field not in inputs:
                    raise UnsupportedTemplateError(
                        f"node {node_id!r} ({node.get('class_type')}) has no "
                        f"input {field!r}"
                    )
                inputs[field] = _substitute(value, placeholders)

        return workflow

    def output_prefixes(self, workflow: dict[str, Any]) -> list[str]:
        """Every SaveImage ``filename_prefix`` in the rendered workflow, ordered
        by its trailing ``_V<n>`` (V1 before V2 …).

        A workflow may save more than one image per run as a panel's A/B variants
        (workflow 1 emits ``_V1`` pre-face-swap + ``_V2`` face-restored → variant
        0 = V1, 1 = V2 …); the worker returns *all* of them. The live workflow 2
        saves a single image per panel, so it yields one prefix (one variant).
        Prefixes without a ``_V<n>`` suffix sort first, preserving single-output
        templates.
        """
        prefixes = [
            node.get("inputs", {}).get("filename_prefix", "")
            for node in workflow.values()
            if node.get("class_type") == "SaveImage"
        ]
        prefixes = [p for p in prefixes if p]

        def _variant_num(prefix: str) -> int:
            match = re.search(r"_V(\d+)$", prefix)
            return int(match.group(1)) if match else 0

        return sorted(prefixes, key=_variant_num)

    def final_output_prefix(self, workflow: dict[str, Any]) -> str:
        """Return the substituted ``filename_prefix`` of the final SaveImage node.

        Scans the rendered workflow for SaveImage nodes and returns the prefix
        ending in :data:`FINAL_OUTPUT_SUFFIX`. The worker filters ComfyUI's
        output history on this prefix so only the final (face-restored) image is
        fetched. Raises :class:`UnsupportedTemplateError` if none qualifies.
        """
        prefixes = [
            node.get("inputs", {}).get("filename_prefix", "")
            for node in workflow.values()
            if node.get("class_type") == "SaveImage"
        ]
        finals = [p for p in prefixes if p.endswith(FINAL_OUTPUT_SUFFIX)]
        if not finals:
            raise UnsupportedTemplateError(
                f"no SaveImage node with a {FINAL_OUTPUT_SUFFIX!r} filename_prefix"
            )
        return finals[0]
