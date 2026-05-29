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
  ``inputs``. We only use the *first* panel: in the new worker a single job maps
  to one template panel, and ``output_count`` variations are produced by varying
  the seed (DESIGN.md §"Job→workflow"), not by walking template panels.

A missing or malformed workflow/template is treated as
:class:`~imagegen.failure_classification.UnsupportedTemplateError` — a corrupt
asset is a deploy bug the worker can't run, never a transient condition.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .failure_classification import UnsupportedTemplateError

# SaveImage nodes whose substituted ``filename_prefix`` ends with this marker
# hold the *final* image we return to the caller. workflows/2 emits two:
# ``..._V1`` (pre-face-swap) and ``..._V2`` (face-restored). We collect V2.
FINAL_OUTPUT_SUFFIX = "_V2"


@dataclass(frozen=True)
class PreparedTemplate:
    """Everything needed to render a template's workflow, loaded once.

    Reused across the per-output runs of a single job so the JSON files are
    read (and validated) exactly once per job, not once per output image.
    """

    template_id: str
    workflow_id: str
    config_nodes: list[dict[str, Any]]
    panel: list[dict[str, Any]]
    base_workflow: dict[str, Any]
    image_slots: list[str]
    """Distinct default ``image`` filenames in the panel, in node order. Each is
    a slot the caller fills with one uploaded input image (see
    :meth:`WorkflowBuilder.render`)."""

    def panel_default(self, field: str) -> Any | None:
        """Return the template's default value for ``field`` (first match) or None."""
        for fields in self.panel:
            if field in fields:
                return fields[field]
        return None


def _substitute(value: Any, placeholders: dict[str, str]) -> Any:
    """Replace every ``placeholder`` substring in a string value; pass others through."""
    if not isinstance(value, str):
        return value
    for placeholder, replacement in placeholders.items():
        value = value.replace(placeholder, replacement)
    return value


class WorkflowBuilder:
    """Loads workflow/template assets and renders submit-ready prompts."""

    def __init__(self, workflow_root: Path, template_root: Path) -> None:
        self._workflow_root = workflow_root
        self._template_root = template_root

    # -- loading ----------------------------------------------------------

    def _load_json(self, path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text())
        except FileNotFoundError as exc:
            raise UnsupportedTemplateError(f"missing asset: {path}") from exc
        except json.JSONDecodeError as exc:
            raise UnsupportedTemplateError(f"invalid JSON in {path}: {exc}") from exc

    def prepare(self, template_id: str) -> PreparedTemplate:
        """Load + validate a template and its workflow, ready for :meth:`render`.

        Raises :class:`UnsupportedTemplateError` if any asset is missing, if the
        template names no workflow, has no panel, or its panel doesn't line up
        positionally with the workflow config's node list.
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
        panel = panels[0]

        if len(panel) != len(config_nodes):
            raise UnsupportedTemplateError(
                f"template {template_id!r} panel has {len(panel)} entries but "
                f"workflow {workflow_id!r} config declares {len(config_nodes)} nodes"
            )

        base_workflow = self._load_json(
            self._workflow_root / workflow_id / "workflow.json"
        )

        image_slots: list[str] = []
        for fields in panel:
            if "image" in fields:
                name = fields["image"]
                if name not in image_slots:
                    image_slots.append(name)

        return PreparedTemplate(
            template_id=template_id,
            workflow_id=str(workflow_id),
            config_nodes=config_nodes,
            panel=panel,
            base_workflow=base_workflow,
            image_slots=image_slots,
        )

    # -- rendering --------------------------------------------------------

    def render(
        self,
        prepared: PreparedTemplate,
        *,
        placeholders: dict[str, str],
        image_remap: dict[str, str],
        prompt: str | None,
        steps: int | None,
        seed: int | None,
    ) -> dict[str, Any]:
        """Return a fresh API-format workflow with this run's values applied.

        Order of operations per node field (mirrors the legacy service):

        1. start from the template panel default,
        2. apply the request override if one targets that field name
           (``text``→prompt, ``value``/``steps``→steps, ``noise_seed``→seed),
        3. substitute ``USER_ID`` / ``STORY_ID`` placeholders in string values,
        4. for an ``image`` field, remap the (substituted) filename to the
           uploaded input name.

        ``None`` overrides are skipped so the template default stands. The base
        workflow is deep-copied, so callers may render many times (one per
        output image) without runs bleeding into each other.
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
        for node_config, fields in zip(prepared.config_nodes, prepared.panel):
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
                value = _substitute(value, placeholders)
                if field == "image":
                    value = image_remap.get(value, value)
                inputs[field] = value

        return workflow

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
