"""Persist the *actual* prompt the model submits to ComfyUI, per panel.

The render path resolves a story's per-panel prompt through several layers —
character ``{TOKEN}`` expansion, ``{INPUT_<n>_AGE}`` substitution, ``USER_ID`` /
``STORY_ID`` — before it ever reaches ComfyUI. By the time it's on the wire the
*effective sentence the model rendered* lives only inside the API-format
workflow JSON. This module captures that, so two consumers can read it back:

* **debug** — when a panel looks wrong, the exact prompt + the full workflow that
  produced it sit on disk next to the story id, no re-derivation needed.
* **the ``image-eval`` skill** — it judges generated panels against the prompt
  that actually drove them, instead of reconstructing the substitution itself.

One JSON file per panel run (one ComfyUI ``/prompt`` submission), under
``<root>/<story_id>/panel_<NN>.json``. Writing is **best-effort**: a logging
failure must never break generation, so every public call swallows its own IO
errors after a warning. Disabled (``root is None``) it is a cheap no-op, which is
the production default — only the dev stack mounts a host volume and sets
``PROMPT_LOG_DIR`` (see ``deploy/stages/dev``).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .workflow import _substitute

logger = logging.getLogger(__name__)

# Panel field keys whose (substituted) value is the human-readable prompt text.
# The render template binds the story prompt to one of these (templates/2 uses
# ``prompt``); we surface their values as ``prompt_text`` for the eval/debug
# reader without it having to walk the workflow graph.
_PROMPT_FIELD_KEYS = ("prompt", "text", "positive")


class PromptLogger:
    """Writes one ``actual prompt + workflow`` record per panel run.

    Construct with ``root=None`` to disable (no-op). The model holds one of these
    for the whole worker lifetime and calls :meth:`log_panel` for each panel it
    submits — there is no per-job state, the story id namespaces the files.
    """

    def __init__(self, root: Path | None) -> None:
        self._root = Path(root) if root is not None else None

    @property
    def enabled(self) -> bool:
        return self._root is not None

    def log_panel(
        self,
        *,
        story_id: str,
        user_id: str,
        story_ref: str,
        render_template_id: str,
        model_version: str,
        panel_index: int,
        client_id: str,
        placeholders: dict[str, str],
        panel: list[dict[str, Any]],
        workflow: dict[str, Any],
        comfyui_prompt_id: str | None = None,
        status: str = "submitted",
        error: str | None = None,
        processing_seconds: float | None = None,
    ) -> None:
        """Write (or overwrite) the record for one panel run.

        Called more than once for the same panel as state advances —
        ``submitted`` right after the workflow is rendered (so a hung/failed run
        is still captured for debug), then ``completed`` / ``error`` once the run
        resolves. The last write wins; the file is small.
        """
        if self._root is None:
            return
        try:
            fields = _substitute_panel(panel, placeholders)
            record = {
                "logged_at": datetime.now(tz=timezone.utc).isoformat(),
                "story_id": story_id,
                "user_id": user_id,
                "story_ref": story_ref,
                "render_template_id": render_template_id,
                "model_version": model_version,
                "panel_index": panel_index,
                "panel_number": panel_index + 1,
                "client_id": client_id,
                "status": status,
                "error": error,
                "comfyui_prompt_id": comfyui_prompt_id,
                "processing_seconds": processing_seconds,
                "placeholders": dict(placeholders),
                "prompt_text": _prompt_text(fields),
                "panel_fields": fields,
                # The exact API-format graph submitted to ComfyUI's /prompt.
                "workflow": workflow,
            }
            out_dir = self._root / story_id
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"panel_{panel_index:02d}.json"
            path.write_text(json.dumps(record, indent=2, default=str))
        except Exception as exc:  # noqa: BLE001 - never break generation on a log
            logger.warning(
                "prompt_log_write_failed",
                extra={"story_id": story_id, "panel_index": panel_index, "error": str(exc)},
            )


def _substitute_panel(
    panel: list[dict[str, Any]], placeholders: dict[str, str]
) -> list[dict[str, Any]]:
    """Apply the render-time placeholder substitution to each panel field.

    ``panel`` is the prepared panel — a list of single-key ``{field: value}``
    dicts whose ``{PROMPT}`` was already filled in at ``prepare`` time. Running
    the same ``_substitute`` the renderer runs yields the literal field values
    that landed in the workflow (prompt text, seed, filename prefixes).
    """
    out: list[dict[str, Any]] = []
    for entry in panel:
        out.append({key: _substitute(value, placeholders) for key, value in entry.items()})
    return out


def _prompt_text(fields: list[dict[str, Any]]) -> str | None:
    """The resolved prompt sentence(s) from the substituted panel fields."""
    texts = [
        str(value)
        for entry in fields
        for key, value in entry.items()
        if key in _PROMPT_FIELD_KEYS and isinstance(value, str)
    ]
    return "\n".join(texts) if texts else None
