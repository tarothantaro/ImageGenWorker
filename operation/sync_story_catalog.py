"""Sync story metadata into the API server's Firestore catalog.

The worker owns story *content* — the prompt sets ``imagegen/prompts/<type>_<id>
.json``. The API server keeps one catalog doc per story (``templates/<type>_<id>``,
e.g. ``templates/1_1``) and serves its title/lesson/story text to the client
(``GET /api/v1/templates/{id}``). This script closes that loop: for every prompt
set it writes

    story_type, story_type_name, story_number, title, lesson, story_version,
    story_text, example_image_urls

onto ``templates/<type>_<id>`` with ``merge=True`` — augmenting the seed-owned
half (required_credits/output_count/active) instead of clobbering it.

``story_text`` is the per-panel storybook narration (read-aloud scene + dialog,
one entry per panel, parallel to the prompt array) authored by the
``story-text`` skill and stored as the prompt JSON's ``texts`` field.
``example_image_urls`` points at the latest Liam-generated catalog examples
uploaded to Cloud Storage by this sync.

The prompt JSON carries ``type``/``id`` (not the legacy ``story_type``/
``story_number``) and no ``story_type_name``; this script maps ``type`` → its
display name via :data:`_TYPE_NAMES` and writes the Firestore field names the
API still reads (``story_type``/``story_number``/``story_type_name``).

Run it through a per-stage wrapper (``operation/stages/<stage>/...``) which
points it at that stage's Firestore via the canonical ``deploy/stages/<stage>/
env.sh`` (dev → the Application local emulator; preprod/prod → real Firestore
over the *operator's* ADC — the worker SA has no Firestore access, §4.3).
Idempotent.

    python operation/sync_story_catalog.py            # sync every story
    python operation/sync_story_catalog.py --template 1_1
    python operation/sync_story_catalog.py --dry-run  # print, write nothing
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# Repo root = the parent of operation/. Prompts live under imagegen/prompts/.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_PROMPTS_DIR = _REPO_ROOT / "imagegen" / "prompts"
_DEFAULT_EXAMPLE_ROOT = _REPO_ROOT / "eval_runs" / "latest" / "outputs" / "liam"

_TEMPLATES_COLLECTION = "templates"
_EXAMPLE_PREFIX = "catalog/examples/liam"

# Display name per prompt ``type`` (the prompt JSON dropped ``story_type_name``;
# the mapping lives in code now). Extend as new story types are added.
_TYPE_NAMES = {1: "life_lesson", 2: "adventure"}


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _story_doc(
    prompt: dict[str, Any], example_image_urls: list[str] | None = None
) -> dict[str, Any]:
    """The subset of a prompt JSON the catalog exposes (TemplateView fields)."""
    story_type = prompt.get("type")
    return {
        "story_type": story_type,
        "story_type_name": _TYPE_NAMES.get(story_type, ""),
        "story_number": prompt.get("id"),
        "title": prompt.get("title", ""),
        "lesson": prompt.get("lesson", ""),
        # The API stores the prompt's ``version`` as ``story_version`` so it
        # never collides with a future template-schema version.
        "story_version": prompt.get("version"),
        # Per-panel storybook narration (one string per panel, parallel to the
        # prompts). Authored by the ``story-text`` skill as the JSON ``texts``
        # field; absent stories sync an empty list rather than a missing field.
        "story_text": prompt.get("texts", []),
        "example_image_urls": example_image_urls or [],
    }


def _example_paths(template_id: str, example_root: Path) -> list[Path]:
    outputs_dir = example_root / template_id / "outputs"
    if not outputs_dir.is_dir():
        return []

    def order(path: Path) -> tuple[int, str]:
        try:
            return (int(path.stem), path.name)
        except ValueError:
            return (10_000, path.name)

    return sorted(outputs_dir.glob("*.png"), key=order)


def _object_name(template_id: str, path: Path) -> str:
    return f"{_EXAMPLE_PREFIX}/{template_id}/{path.name}"


def _public_url(bucket: str, object_name: str) -> str:
    storage_emulator = os.environ.get("STORAGE_EMULATOR_HOST")
    if storage_emulator:
        return (
            f"{storage_emulator.rstrip('/')}/storage/v1/b/{bucket}/o/"
            f"{object_name.replace('/', '%2F')}?alt=media"
        )
    return f"https://storage.googleapis.com/{bucket}/{object_name}"


def _upload_examples(
    *,
    bucket_name: str | None,
    template_id: str,
    paths: list[Path],
    dry_run: bool,
) -> list[str]:
    if not bucket_name or not paths:
        return []

    urls = [_public_url(bucket_name, _object_name(template_id, path)) for path in paths]
    if dry_run:
        return urls

    from google.cloud import storage  # type: ignore[import-untyped]

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    for path in paths:
        bucket.blob(_object_name(template_id, path)).upload_from_filename(
            str(path), content_type="image/png"
        )
    return urls


def _bindings(
    only: str | None,
    *,
    example_root: Path = _DEFAULT_EXAMPLE_ROOT,
    bucket_name: str | None = None,
    dry_run: bool = False,
) -> list[tuple[str, dict[str, Any]]]:
    """(template_id, story_doc) for each prompt set.

    ``template_id`` is ``<type>_<id>`` (the prompt file stem), which is also the
    catalog doc id the API serves.
    """
    if not _PROMPTS_DIR.is_dir():
        raise SystemExit(f"prompts dir not found: {_PROMPTS_DIR}")
    out: list[tuple[str, dict[str, Any]]] = []
    for prompt_path in sorted(_PROMPTS_DIR.glob("[0-9]*_[0-9]*.json")):
        template_id = prompt_path.stem  # "<type>_<id>"
        if only is not None and template_id != only:
            continue
        example_urls = _upload_examples(
            bucket_name=bucket_name,
            template_id=template_id,
            paths=_example_paths(template_id, example_root),
            dry_run=dry_run,
        )
        out.append((template_id, _story_doc(_load_json(prompt_path), example_urls)))
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Sync story metadata to Firestore.")
    p.add_argument(
        "--template", default=None, help="only sync this catalog id (<type>_<id>)"
    )
    p.add_argument(
        "--project", default=None, help="GCP project (else GOOGLE_CLOUD_PROJECT)"
    )
    p.add_argument(
        "--examples-root",
        type=Path,
        default=_DEFAULT_EXAMPLE_ROOT,
        help="local example output root (<root>/<template>/outputs/*.png)",
    )
    p.add_argument(
        "--examples-bucket",
        default=None,
        help="bucket for catalog example images (else GCS_BUCKET/GCS_INPUT_BUCKET)",
    )
    p.add_argument("--dry-run", action="store_true", help="print, write nothing")
    args = p.parse_args(argv)

    examples_bucket = (
        args.examples_bucket
        or os.environ.get("GCS_BUCKET")
        or os.environ.get("GCS_INPUT_BUCKET")
    )
    bindings = _bindings(
        args.template,
        example_root=args.examples_root,
        bucket_name=examples_bucket,
        dry_run=args.dry_run,
    )
    if not bindings:
        print("[sync] nothing to sync (no bound templates matched)", file=sys.stderr)
        return 0

    project = args.project or os.environ.get("GOOGLE_CLOUD_PROJECT")
    target = os.environ.get(
        "FIRESTORE_EMULATOR_HOST", f"real Firestore (project={project})"
    )
    print(
        f"[sync] target={target} project={project} "
        f"examples_bucket={examples_bucket or '-'}",
        file=sys.stderr,
    )

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
            f"version={doc['story_version']} "
            f"examples={len(doc['example_image_urls'])}"
        )
    print(f"[sync] done: {len(bindings)} template(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
