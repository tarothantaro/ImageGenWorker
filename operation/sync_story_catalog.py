"""Sync bound-story metadata into the API server's Firestore catalog.

The worker owns story *content* (``imagegen/prompts/<story>.json``) and the
template→story binding (``imagegen/templates/<id>/config.json`` → ``"story"``).
The API server reads ``templates/{id}`` and serves the story's title/lesson to
the client (``GET /api/v1/templates/{id}``). This script closes that loop: for
every worker template that binds a story, it writes that story's

    story_type, story_type_name, story_number, title, lesson, story_version

onto ``templates/{template_id}`` with ``merge=True`` — so it augments the
seed-owned half (required_credits/output_count) instead of clobbering it.

Run it through a per-stage wrapper (``operation/stages/<stage>/...``) which
points it at that stage's Firestore via the canonical ``deploy/stages/<stage>/
env.sh`` (dev → the Application local emulator; preprod/prod → real Firestore
over the *operator's* ADC — the worker SA has no Firestore access, §4.3).
Idempotent.

    python operation/sync_story_catalog.py            # sync every bound template
    python operation/sync_story_catalog.py --template 4
    python operation/sync_story_catalog.py --dry-run  # print, write nothing
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# Repo root = the parent of operation/. Templates + prompts live under imagegen/.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATES_DIR = _REPO_ROOT / "imagegen" / "templates"
_PROMPTS_DIR = _REPO_ROOT / "imagegen" / "prompts"

_TEMPLATES_COLLECTION = "templates"


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _story_doc(prompt: dict[str, Any]) -> dict[str, Any]:
    """The subset of a prompt JSON the catalog exposes (TemplateView fields)."""
    return {
        "story_type": prompt.get("story_type"),
        "story_type_name": prompt.get("story_type_name", ""),
        "story_number": prompt.get("story_number"),
        "title": prompt.get("title", ""),
        "lesson": prompt.get("lesson", ""),
        # The API stores the prompt's ``version`` as ``story_version`` so it
        # never collides with a future template-schema version.
        "story_version": prompt.get("version"),
    }


def _bindings(only: str | None) -> list[tuple[str, dict[str, Any]]]:
    """(template_id, story_doc) for each template config that binds a story."""
    if not _TEMPLATES_DIR.is_dir():
        raise SystemExit(f"templates dir not found: {_TEMPLATES_DIR}")
    out: list[tuple[str, dict[str, Any]]] = []
    for config_path in sorted(_TEMPLATES_DIR.glob("*/config.json")):
        config = _load_json(config_path)
        template_id = str(config.get("id") or config_path.parent.name)
        if only is not None and template_id != only:
            continue
        story = config.get("story")
        if not story:
            print(
                f"[sync] template {template_id}: no bound story — skipped",
                file=sys.stderr,
            )
            continue
        prompt_path = _PROMPTS_DIR / f"{story}.json"
        if not prompt_path.is_file():
            raise SystemExit(
                f"template {template_id} binds story '{story}' but "
                f"{prompt_path} is missing"
            )
        out.append((template_id, _story_doc(_load_json(prompt_path))))
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Sync bound-story metadata to Firestore.")
    p.add_argument("--template", default=None, help="only sync this template id")
    p.add_argument(
        "--project", default=None, help="GCP project (else GOOGLE_CLOUD_PROJECT)"
    )
    p.add_argument("--dry-run", action="store_true", help="print, write nothing")
    args = p.parse_args(argv)

    bindings = _bindings(args.template)
    if not bindings:
        print("[sync] nothing to sync (no bound templates matched)", file=sys.stderr)
        return 0

    project = args.project or os.environ.get("GOOGLE_CLOUD_PROJECT")
    target = os.environ.get(
        "FIRESTORE_EMULATOR_HOST", f"real Firestore (project={project})"
    )
    print(f"[sync] target={target} project={project}", file=sys.stderr)

    if args.dry_run:
        for template_id, doc in bindings:
            print(f"[dry-run] templates/{template_id} <- {json.dumps(doc)}")
        return 0

    # Imported lazily so --dry-run works without the client installed
    # (pip install -e .[catalog]).
    from google.cloud import firestore

    db = firestore.Client(project=project) if project else firestore.Client()
    for template_id, doc in bindings:
        db.collection(_TEMPLATES_COLLECTION).document(template_id).set(doc, merge=True)
        print(
            f"[sync] templates/{template_id} title={doc['title']!r} "
            f"version={doc['story_version']}"
        )
    print(f"[sync] done: {len(bindings)} template(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
