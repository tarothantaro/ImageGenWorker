"""Render a ComfyUI API-format prompt from a stored workflow + template.

Ports the customization logic from the legacy ImageGenCp ``WorkflowService``
(``../ImageGenCp/src/services/workflow.py``) into the stateless worker. This
module is *pure*: no network, no ComfyUI, no clock — just JSON loading and
field substitution. That keeps it unit-testable without a running container.

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
  looks identical across every panel of the story. ``USER_ID`` / ``STORY_ID``
  are still resolved later, at :meth:`render` time, since they are per-job.

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
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .failure_classification import UnsupportedTemplateError

# SaveImage nodes whose substituted ``filename_prefix`` ends with this marker
# hold the *final* image we return to the caller. workflows/1 emits two:
# ``..._V1`` (pre-face-swap) and ``..._V2`` (face-restored). We collect V2.
FINAL_OUTPUT_SUFFIX = "_V2"


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


class WorkflowBuilder:
    """Loads workflow/template assets and renders submit-ready prompts."""

    def __init__(
        self,
        workflow_root: Path,
        template_root: Path,
        prompts_root: Path | None = None,
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

        characters = self._load_character_map()
        for panel_index, (panel, prompt) in enumerate(zip(panels, prompts)):
            text_field = next((fields for fields in panel if "text" in fields), None)
            if text_field is None:
                raise UnsupportedTemplateError(
                    f"template {template_id!r} story {story_ref!r}: panel "
                    f"{panel_index} has no 'text' field to receive a prompt"
                )
            text_field["text"] = _substitute(str(prompt), characters)

    def _load_character_map(self) -> dict[str, str]:
        """Map each ``{TOKEN}`` placeholder to its description from character.json.

        ``prompts/character.json`` is the shared library of generated supporting
        characters (owned by the ``character-config`` skill). Only the runtime
        ``characters[*].description`` field is consumed. Tokens with no entry are
        simply not in the map, so the caller's substitution leaves them
        untouched (visible in the rendered prompt rather than failing the job).
        """
        data = self._load_json(self._prompts_root / "character.json")
        placeholders: dict[str, str] = {}
        for token, entry in data.get("characters", {}).items():
            description = entry.get("description") if isinstance(entry, dict) else None
            if description:
                placeholders[f"{{{token}}}"] = str(description)
        return placeholders

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
