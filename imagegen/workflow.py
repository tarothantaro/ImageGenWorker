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
  placeholders (e.g. ``{GENDER_F_AGE_70_RACE_ASIAN}``) which :meth:`prepare`
  resolves against ``prompts/character.json`` so the same generated character
  looks identical across every panel of the story. A character token that has
  *no* enumerated description (only the ``GENDER_<g>_AGE_<a>_RACE_<r>`` config
  baked into the token) instead gets a look composed on the fly — the hair,
  build, wardrobe and features picked at random from ``character.json``'s
  modular tables, once per job so it too stays identical across the panels.
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

# A supporting-character placeholder encodes its *fixed* traits in the token
# itself — ``GENDER_<g>_AGE_<a>_RACE_<r>`` — plus an optional trailing
# ``_<suffix>`` that only disambiguates two otherwise-identical configs (e.g.
# ``..._RACE_ASIAN_PARENT``). Everything past gender/age/race is the character's
# *look* (hair, build, wardrobe, features), filled in at random when the token
# carries no enumerated description in character.json.
_CHARACTER_TOKEN_RE = re.compile(
    r"^GENDER_(?P<gender>[A-Z]+)_AGE_(?P<age>[A-Z0-9]+)_RACE_(?P<race>[A-Z_]+)$"
)
# Any ``{TOKEN}`` in a prompt string. Used to discover which character tokens a
# story references so the un-enumerated ones can be composed.
_PLACEHOLDER_RE = re.compile(r"\{([A-Z0-9_]+)\}")


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
    """Compose a description for a ``GENDER_<g>_AGE_<a>_RACE_<r>`` character token.

    The fixed traits (gender / age / race) are read from the token; the hair,
    build, wardrobe and features are drawn **at random** from ``character.json``'s
    modular tables and assembled with the same recipe the enumerated
    descriptions use::

        {age} {ethnicity} {gender_noun} with {hair}, {build},
            wearing {wardrobe}[, with {features}]

    The draw is **age-aware**: a child-age token never rolls a fragment the
    ``age_restrictions`` table reserves to adults (a business suit, stubble, a
    grey bun …), and an adult-age token never rolls a child-only one. A fragment
    in neither list suits both ages; if every option in a table is reserved to
    the other age group, the restriction is ignored rather than yielding nothing.

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
    race_key = _resolve_race_key(match["race"], race_table)
    race = race_table.get(race_key) if race_key is not None else None
    if not (gender and age and race):
        return None

    is_child = bool(age.get("child"))
    # Fragment keys character.json reserves to the *other* age group — excluded
    # from this character's draw. A key listed under neither group suits both.
    reserved_to_other = data.get("age_restrictions", {}).get(
        "adult_only" if is_child else "child_only", {}
    )

    def pick(table_name: str) -> str | None:
        table = data.get(table_name, {})
        if not table:
            return None
        excluded = set(reserved_to_other.get(table_name, []))
        candidates = sorted(key for key in table if key not in excluded)
        if not candidates:  # every option reserved to the other age → use all
            candidates = sorted(table)
        return str(table[rng.choice(candidates)])

    hair = pick("hair")
    build = pick("build")
    wardrobe = pick("wardrobe")
    features = pick("features")

    noun = gender.get("noun_child") if is_child else gender.get("noun")
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
        """Fill each panel's CLIPTextEncode ``text`` from the bound story's prompts.

        ``prompts/<story_ref>.json`` carries a ``prompts`` array — one prompt per
        panel, in order. Character ``{TOKEN}`` placeholders are resolved here
        against ``prompts/character.json`` (static across a job, so resolve once
        at load), leaving ``USER_ID`` / ``STORY_ID`` for :meth:`render`. A prompt/
        panel count mismatch, a panel with no ``text`` field, or a missing
        prompts/character asset is a deploy bug → :class:`UnsupportedTemplateError`.
        """
        story = self._load_json(self._prompts_root / f"{story_ref}.json")
        prompts = story.get("prompts", [])
        if len(prompts) != len(panels):
            raise UnsupportedTemplateError(
                f"template {template_id!r} story {story_ref!r} has {len(prompts)} "
                f"prompts but the template has {len(panels)} panels"
            )

        characters = self._character_substitutions([str(p) for p in prompts])
        for panel_index, (panel, prompt) in enumerate(zip(panels, prompts)):
            text_field = next((fields for fields in panel if "text" in fields), None)
            if text_field is None:
                raise UnsupportedTemplateError(
                    f"template {template_id!r} story {story_ref!r}: panel "
                    f"{panel_index} has no 'text' field to receive a prompt"
                )
            text_field["text"] = _substitute(str(prompt), characters)

    def _character_substitutions(self, prompts: list[str]) -> dict[str, str]:
        """Map every character ``{TOKEN}`` in ``prompts`` to a description.

        ``prompts/character.json`` is the shared library of generated supporting
        characters (owned by the ``character-config`` skill). Tokens are resolved
        by two paths, in precedence order:

        1. **Enumerated** — ``characters[TOKEN].description`` is used verbatim, so
           an authored character's look stays byte-for-byte identical across every
           panel *and* every job.
        2. **Composed** — a token shaped like ``GENDER_<g>_AGE_<a>_RACE_<r>`` but
           with no enumerated description gets a look composed on the fly: the
           gender / age / race come from the token (the "config in the
           placeholder"); the hair, build, wardrobe and features are picked at
           random from the modular tables (see :func:`_compose_random_character`).
           Each distinct token is composed once here, so its random look is the
           same in every panel of this job.

        Tokens that are neither enumerated nor a valid character config (e.g.
        ``{INPUT_1_AGE}``, ``{USER_ID}``, an unknown name) are left out of the
        map, so the caller's substitution passes them through untouched.
        """
        data = self._load_json(self._prompts_root / "character.json")
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
        return substitutions

    # -- rendering --------------------------------------------------------

    def render(
        self,
        prepared: PreparedTemplate,
        panel: list[dict[str, Any]],
        *,
        placeholders: dict[str, str],
        prompt: str | None,
        steps: int | None,
        seed: int | None,
    ) -> dict[str, Any]:
        """Return a fresh API-format workflow with ``panel``'s values applied.

        Order of operations per node field (mirrors the legacy service):

        1. start from the panel default,
        2. apply the request override if one targets that field name
           (``text``→prompt, ``value``/``steps``→steps, ``noise_seed``→seed),
        3. substitute ``USER_ID`` / ``STORY_ID`` placeholders in string values.

        ``None`` overrides are skipped so the panel default stands — in
        particular a ``None`` ``seed`` keeps each panel's own ``noise_seed``.
        The base workflow is deep-copied, so callers may render every panel
        without runs bleeding into each other.
        """
        overrides: dict[str, Any] = {}
        if prompt is not None:
            overrides["text"] = prompt
        if steps is not None:
            overrides["value"] = steps
            overrides["steps"] = steps
        if seed is not None:
            overrides["noise_seed"] = seed

        workflow = copy.deepcopy(prepared.base_workflow)
        for node_config, fields in zip(prepared.config_nodes, panel):
            node_id = str(node_config["id"])
            node = workflow.get(node_id)
            if node is None:
                raise UnsupportedTemplateError(
                    f"workflow {prepared.workflow_id!r} has no node {node_id!r}"
                )
            inputs = node.setdefault("inputs", {})
            for field, default in fields.items():
                if field not in inputs:
                    raise UnsupportedTemplateError(
                        f"node {node_id!r} ({node.get('class_type')}) has no "
                        f"input {field!r}"
                    )
                value = overrides.get(field, default)
                inputs[field] = _substitute(value, placeholders)

        return workflow

    def output_prefixes(self, workflow: dict[str, Any]) -> list[str]:
        """Every SaveImage ``filename_prefix`` in the rendered workflow, ordered
        by its trailing ``_V<n>`` (V1 before V2 …).

        workflow 2 emits two images per run — ``_V1`` (pre-face-swap) and
        ``_V2`` (face-restored) — and the worker returns *all* of them as a
        panel's A/B variants (variant 0 = V1, 1 = V2 …). Prefixes without a
        ``_V<n>`` suffix sort first, preserving single-output templates.
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
