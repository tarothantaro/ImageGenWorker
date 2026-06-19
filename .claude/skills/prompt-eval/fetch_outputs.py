#!/usr/bin/env python3
"""Fetch a story's generated panel images from the Application local GCS and
build a per-image manifest mapping each output back to its panel prompt.

The "results" a `prompts/<type>_<id>.json` produces are the panel PNGs the worker
writes to the **Application local stack's** fake-gcs-server (the bucket the API
server and worker share — DESIGN.md §5.1):

    gs://$GCS_BUCKET/<user_id>/<story_id>/outputs/<index>.png

This script does the I/O half of the `prompt-eval` skill: it lists / downloads
those PNGs from the local emulator and writes ``manifest.json`` joining each
downloaded file to the panel prompt it was generated from — with ``{TOKEN}``
characters resolved from ``character.json`` and ``{INPUT_n_AGE}`` dropped, so the
vision judge reads the *effective* prompt the model actually rendered. The judging
itself is done by the agent reading each PNG; this script never calls an LLM.

Output index → storybook layout (mirrors ``imagegen/model.py``): the live render
template emits ``variants`` images per panel (V1 = pre-face-swap, V2 =
face-restored), flattened in order, so::

    panel_index = index // variants
    variant     = index %  variants   # 0 -> V1, 1 -> V2, ...

Usage (host side, from the repo root; the Application local stack must be up so
fake-gcs is published on :4443):

    # discover which <user>/<story> output sets exist in the bucket
    PYTHONPATH=. ~/python_env/torch-env/bin/python \
        .claude/skills/prompt-eval/fetch_outputs.py --list

    # download one set + build the manifest for prompt set 1_1
    PYTHONPATH=. ~/python_env/torch-env/bin/python \
        .claude/skills/prompt-eval/fetch_outputs.py \
        --story 1_1 --user-id <uid> --story-id <sid>

``--story`` is the prompt-file stem (the worker ``type_id``). ``--story-id`` is
the GCS path component (the Application's job/story id) — they are *not* the same
thing, which is why both are required for a download.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

_SKILL_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SKILL_DIR.parents[2]  # .claude/skills/prompt-eval -> repo root
_PROMPTS_DIR = _REPO_ROOT / "imagegen" / "prompts"

_DEFAULT_BUCKET = "tarostory-local-images"
_DEFAULT_GCS_HOST = "http://localhost:4443"
_DEFAULT_PROJECT = "tarostory-local"
_OUTPUT_RE = re.compile(r"^(?P<user>[^/]+)/(?P<story>[^/]+)/outputs/(?P<index>\d+)\.png$")
_AGE_TOKEN_RE = re.compile(r"\{INPUT_\d+_AGE\}")


# --- prompt resolution -------------------------------------------------------


def _load_characters() -> dict[str, str]:
    """Return ``{TOKEN: description}`` from ``character.json`` (may be empty)."""
    path = _PROMPTS_DIR / "character.json"
    data = json.loads(path.read_text())
    chars = data.get("characters", {})
    return {token: entry.get("description", "") for token, entry in chars.items()}


def _resolve_prompt(raw: str, characters: dict[str, str]) -> str:
    """Mirror the worker's runtime substitution for *display* to the judge.

    Replaces every ``{TOKEN}`` with its ``character.json`` description (flat
    string replace, as ``workflow.py`` does) and drops ``{INPUT_n_AGE}`` plus the
    space after it (matching ``model._age_placeholders`` for an age-less job), so
    the judge sees the natural sentence the model rendered.
    """
    resolved = raw
    for token, description in characters.items():
        resolved = resolved.replace("{" + token + "}", description)
    resolved = _AGE_TOKEN_RE.sub("", resolved).replace("  ", " ")
    return resolved.strip()


def _variants_for_live_template() -> int:
    """How many SaveImage variants the live render template emits per panel.

    Prefers the real worker code path (``WorkflowBuilder`` + the live
    ``_RENDER_TEMPLATE_ID``); falls back to counting ``SaveImage`` nodes in the
    template's workflow JSON, then to 2 (V1/V2) so the skill still runs if the
    imports change.
    """
    try:
        from imagegen.model import _RENDER_TEMPLATE_ID
        from imagegen.workflow import WorkflowBuilder

        builder = WorkflowBuilder(
            _REPO_ROOT / "imagegen" / "workflows",
            _REPO_ROOT / "imagegen" / "templates",
            _PROMPTS_DIR,
        )
        # output_prefixes works off the base workflow alone — no prompt set needed.
        base = json.loads(
            (
                _REPO_ROOT
                / "imagegen"
                / "workflows"
                / _RENDER_TEMPLATE_ID
                / "workflow.json"
            ).read_text()
        )
        count = len(builder.output_prefixes(base))
        return count or 2
    except Exception as exc:  # noqa: BLE001 - best-effort; fall back gracefully
        print(f"[eval] warn: could not derive variant count ({exc}); assuming 2",
              file=sys.stderr)
        return 2


# --- GCS access --------------------------------------------------------------


def _client(project: str):
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import storage

    return storage.Client(project=project, credentials=AnonymousCredentials())


def _list_sets(bucket_name: str, project: str) -> int:
    client = _client(project)
    bucket = client.bucket(bucket_name)
    sets: dict[tuple[str, str], int] = {}
    for blob in client.list_blobs(bucket):
        m = _OUTPUT_RE.match(blob.name)
        if m:
            key = (m.group("user"), m.group("story"))
            sets[key] = sets.get(key, 0) + 1
    if not sets:
        print(f"[eval] no output sets found in gs://{bucket_name}/*/*/outputs/")
        return 1
    print(f"[eval] output sets in gs://{bucket_name}:")
    for (user, story), n in sorted(sets.items()):
        print(f"    --user-id {user} --story-id {story}    ({n} images)")
    return 0


def _download(args: argparse.Namespace) -> int:
    story_json = _PROMPTS_DIR / f"{args.story}.json"
    if not story_json.exists():
        print(f"[eval] error: prompt set not found: {story_json}", file=sys.stderr)
        return 2
    spec = json.loads(story_json.read_text())
    prompts: list[str] = spec.get("prompts", [])
    characters = _load_characters()
    variants = _variants_for_live_template()

    out_dir = Path(
        args.out or f"/tmp/prompt_eval/{args.story}__{args.story_id}"
    ).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    client = _client(args.project)
    bucket = client.bucket(args.bucket)
    prefix = f"{args.user_id}/{args.story_id}/outputs/"
    blobs = sorted(
        (b for b in client.list_blobs(bucket, prefix=prefix) if b.name.endswith(".png")),
        key=lambda b: int(Path(b.name).stem),
    )
    if not blobs:
        print(f"[eval] error: no PNGs under gs://{args.bucket}/{prefix}",
              file=sys.stderr)
        print("[eval] hint: run with --list to see available sets", file=sys.stderr)
        return 3

    entries = []
    for blob in blobs:
        index = int(Path(blob.name).stem)
        panel_index = index // variants
        variant = index % variants
        local = out_dir / f"{index:02d}_panel{panel_index + 1}_V{variant + 1}.png"
        blob.download_to_filename(str(local))
        raw = prompts[panel_index] if panel_index < len(prompts) else None
        entries.append(
            {
                "index": index,
                "panel_index": panel_index,
                "panel_number": panel_index + 1,
                "variant": variant,
                "variant_label": f"V{variant + 1}",
                "variant_role": "pre-face-swap" if variant == 0 else "face-restored",
                "file": str(local),
                "gcs": f"gs://{args.bucket}/{blob.name}",
                "raw_prompt": raw,
                "resolved_prompt": _resolve_prompt(raw, characters) if raw else None,
            }
        )

    manifest = {
        "story": args.story,
        "title": spec.get("title"),
        "lesson": spec.get("lesson"),
        "characters": {t: characters.get(t, "") for t in spec.get("characters", [])},
        "user_id": args.user_id,
        "story_id": args.story_id,
        "bucket": args.bucket,
        "variants_per_panel": variants,
        "panel_count": len(prompts),
        "out_dir": str(out_dir),
        "report_path": str(out_dir / "report.md"),
        "images": entries,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    print(f"[eval] story {args.story!r}: {spec.get('title')!r}")
    print(f"[eval] downloaded {len(entries)} image(s) "
          f"({len(prompts)} panels x {variants} variants) -> {out_dir}")
    print(f"[eval] manifest -> {out_dir / 'manifest.json'}")
    print(f"[eval] write the report to -> {out_dir / 'report.md'}")
    if len(entries) != len(prompts) * variants:
        print(f"[eval] warn: expected {len(prompts) * variants} images, "
              f"got {len(entries)} (set may be partial)", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true",
                        help="list <user>/<story> output sets in the bucket and exit")
    parser.add_argument("--story", help="prompt-file stem / worker type_id, e.g. 1_1")
    parser.add_argument("--user-id", help="GCS path user_id component")
    parser.add_argument("--story-id", help="GCS path story_id (the job/story id)")
    parser.add_argument("--out", help="download dir (default /tmp/prompt_eval/<story>__<story_id>)")
    parser.add_argument("--bucket", default=os.environ.get("GCS_BUCKET", _DEFAULT_BUCKET))
    parser.add_argument("--project", default=os.environ.get("GCP_PROJECT_ID", _DEFAULT_PROJECT))
    args = parser.parse_args(argv)

    os.environ.setdefault("STORAGE_EMULATOR_HOST", _DEFAULT_GCS_HOST)

    if args.list:
        return _list_sets(args.bucket, args.project)

    missing = [n for n in ("story", "user_id", "story_id") if not getattr(args, n)]
    if missing:
        parser.error("download mode needs --" + ", --".join(m.replace("_", "-") for m in missing))
    return _download(args)


if __name__ == "__main__":
    sys.exit(main())
