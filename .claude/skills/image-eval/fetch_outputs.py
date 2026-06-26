#!/usr/bin/env python3
"""Fetch a story's generated panel images from the Application local GCS and
build a per-image manifest mapping each output back to its panel prompt.

The "results" a `prompts/<type>_<id>.json` produces are the panel PNGs the worker
writes to the **Application local stack's** fake-gcs-server (the bucket the API
server and worker share — DESIGN.md §5.1):

    gs://$GCS_BUCKET/<user_id>/<story_id>/outputs/<index>.png

This script does the I/O half of the `image-eval` skill: it lists / downloads
GCS PNGs from the local emulator, or references local PNGs in place, and writes
``manifest.json`` joining each file to the panel prompt it was generated from — with ``{TOKEN}``
characters resolved from ``character.json`` and ``{INPUT_n_AGE}`` dropped, so the
vision judge reads the *effective* prompt the model actually rendered. The judging
itself is done by the agent reading each PNG; this script never calls an LLM.

Output index → storybook layout (mirrors ``imagegen/model.py``): the live render
template emits **one image per panel**, written at a flat output index. The helper
derives the per-panel image count from the live workflow, so the math below still
holds if a template is ever changed to emit more than one image per panel
(they would be flattened in index order)::

    panel_index = index // variants
    variant     = index %  variants

Usage (host side, from the repo root; the Application local stack must be up so
fake-gcs is published on :4443):

    # discover which <user>/<story> output sets exist in the bucket
    PYTHONPATH=. ~/python_env/torch-env/bin/python \
        .claude/skills/image-eval/fetch_outputs.py --list

    # build the manifest for prompt set 1_1
    PYTHONPATH=. ~/python_env/torch-env/bin/python \
        .claude/skills/image-eval/fetch_outputs.py \
        --story 1_1 --user-id <uid> --story-id <sid>

    # use customized log and output folder
    PYTHONPATH=. ~/python_env/torch-env/bin/python \
    .claude/skills/image-eval/fetch_outputs.py --local-root eval_runs/latest/outputs \
    --log-dir eval_runs/latest/prompt_logs --story 1_1 --user-id leo --story-id 1_1 \
    --out eval_runs/latest/eval/1_1__1_1    

Without ``--out``, ``manifest.json`` and ``report.md`` are written under
``eval_runs/latest/eval/`` so the review app sees them by default. In GCS mode,
the fetched images are downloaded there too. In local mode, the manifest
references the canonical PNGs under ``--local-root`` instead of copying them into
``eval/``. When ``--out`` points elsewhere, the manifest is also mirrored under
the latest eval directory for review.

``--story`` is the prompt-file stem (the worker ``type_id``, e.g. ``1_1``) — used
to load ``imagegen/prompts/1_1.json`` for panel prompts/gists/texts. ``--story-id``
is the GCS path component (the Application's opaque job/story id, e.g. a ULID) —
used to build ``<user_id>/<story_id>/outputs/`` to find the output PNGs. In the
Application stack these are genuinely different: prompt set ``1_1`` might write its
outputs under a UUID the Application assigned at job creation. In the
``local-batch-eval`` flow they happen to be the same value because
``generate_stories.py`` uses the prompt stem as the output directory name — but both
params are still required here because this script serves both contexts.

Output source is **configurable**. By default the script reads from the
Application local stack's fake-gcs (the flow above). Pass ``--local-root`` (or set
``LOCAL_OUTPUT_ROOT``) to instead read PNGs straight off the local filesystem in
the very same ``<user>/<story>/outputs/<index>.png`` layout — this is what the
``local-batch-eval`` skill uses when the worker is driven directly from
*this* repo (``.claude/skills/local-batch-eval/generate_stories.py``) with no Application stack and no
GCS in the loop. Everything downstream (the index→panel/variant math, the prompt
log join, the manifest) is identical; only the bytes come from disk instead of
GCS.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

_SKILL_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SKILL_DIR.parents[2]  # .claude/skills/image-eval -> repo root
_PROMPTS_DIR = _REPO_ROOT / "imagegen" / "prompts"
_DEFAULT_EVAL_DIR = _REPO_ROOT / "eval_runs" / "latest" / "eval"
_OUTDATED_WARNING = (
    "> WARNING: This eval report is outdated. The story outputs, prompts, gists, "
    "or dialog were refreshed after this report was written, so keep the report "
    "for history only and regenerate eval before using it for quality decisions."
)
# Where the dev worker writes its per-panel actual-prompt records (PROMPT_LOG_DIR
# host mount, deploy/stages/dev). Each story_id is a subdir of panel_NN.json.
_DEFAULT_LOG_DIR = _REPO_ROOT / "prompt_logs"

_DEFAULT_BUCKET = "tarostory-local-images"
_DEFAULT_GCS_HOST = "http://localhost:4443"
_DEFAULT_PROJECT = "tarostory-local"
_OUTPUT_RE = re.compile(
    r"^(?P<user>[^/]+)/(?P<story>[^/]+)/outputs/(?P<index>\d+)\.png$"
)
_AGE_TOKEN_RE = re.compile(r"\{INPUT_\d+_AGE\}")


# --- refreshed artifact / stale-report handling -----------------------------


def _same_path(left: Path, right: Path) -> bool:
    return left.expanduser().resolve() == right.expanduser().resolve()


def _review_dir(story: str, story_id: str) -> Path:
    return _DEFAULT_EVAL_DIR / f"{story}__{story_id}"


def _mark_report_outdated(report: Path) -> bool:
    if not report.exists():
        return False
    text = report.read_text()
    if _OUTDATED_WARNING in text:
        return False
    lines = text.splitlines()
    if lines and lines[0].startswith("# "):
        updated = "\n".join([lines[0], "", _OUTDATED_WARNING, "", *lines[1:]])
    else:
        updated = _OUTDATED_WARNING + "\n\n" + text
    report.write_text(updated.rstrip() + "\n")
    return True


def mark_reports_outdated(eval_root: Path, story_id: str | None = None) -> None:
    """Mark existing review reports stale after their artifacts are refreshed.

    ``story_id`` keeps the legacy ``generate_latest.py`` behavior for local runs,
    where ``story`` and ``story_id`` are the same stem. Direct fetches with
    distinct ``story`` / ``story_id`` mark their concrete output dirs via
    ``_mark_report_outdated``.
    """
    if story_id:
        reports = [eval_root / f"{story_id}__{story_id}" / "report.md"]
    else:
        reports = sorted(eval_root.glob("*__*/report.md"))
    for report in reports:
        _mark_report_outdated(report)


def _write_manifest(out_dir: Path, manifest: dict) -> None:
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))


def _clear_pngs(out_dir: Path) -> None:
    for image in out_dir.glob("*.png"):
        image.unlink()


def _manifest_for_dir(manifest: dict, out_dir: Path) -> dict:
    mirrored = dict(manifest)
    mirrored["out_dir"] = str(out_dir)
    mirrored["report_path"] = str(out_dir / "report.md")
    return mirrored


def _mirror_to_latest(out_dir: Path, manifest: dict) -> Path | None:
    latest_dir = _review_dir(str(manifest["story"]), str(manifest["story_id"]))
    if _same_path(out_dir, latest_dir):
        return None

    latest_dir.mkdir(parents=True, exist_ok=True)
    _clear_pngs(latest_dir)
    if not manifest.get("uses_local_output_refs"):
        for entry in manifest["images"]:
            image = Path(str(entry["file"]))
            shutil.copyfile(image, latest_dir / image.name)
        mirrored = _manifest_for_dir(manifest, latest_dir)
        mirrored["images"] = []
        for entry in manifest["images"]:
            image_entry = dict(entry)
            image_entry["file"] = str(latest_dir / Path(str(entry["file"])).name)
            mirrored["images"].append(image_entry)
    else:
        mirrored = _manifest_for_dir(manifest, latest_dir)
    _write_manifest(latest_dir, mirrored)
    return latest_dir


# --- actual-prompt logs ------------------------------------------------------


def _load_prompt_log(log_dir: Path, story_id: str) -> dict[int, dict]:
    """Return ``{panel_index: record}`` from the worker's prompt log for a story.

    The dev worker writes one ``<log_dir>/<story_id>/panel_<NN>.json`` per panel,
    holding the *actual* prompt + rendered workflow it submitted to ComfyUI
    (``imagegen/prompt_log.py``). Reading these lets the judge grade against the
    real prompt instead of re-deriving the substitution. Missing dir / unreadable
    files degrade to ``{}`` so the caller falls back to reconstruction.
    """
    story_dir = log_dir / story_id
    if not story_dir.is_dir():
        return {}
    records: dict[int, dict] = {}
    for path in sorted(story_dir.glob("panel_*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            print(
                f"[eval] warn: could not read prompt log {path}: {exc}", file=sys.stderr
            )
            continue
        idx = record.get("panel_index")
        if isinstance(idx, int):
            record["_log_path"] = str(path)
            records[idx] = record
    return records


# --- prompt resolution -------------------------------------------------------


def _load_characters() -> dict[str, str]:
    """Return ``{TOKEN: description}`` from ``character.json`` (may be empty)."""
    path = _PROMPTS_DIR / "character.json"
    data = json.loads(path.read_text())
    chars = data.get("characters", {})
    descriptions: dict[str, str] = {}
    for token, entry in chars.items():
        if not token.startswith("GENDER_"):
            continue
        if isinstance(entry, dict):
            description = entry.get("description")
        elif isinstance(entry, str):
            description = entry
        else:
            description = None
        if isinstance(description, str):
            descriptions[token] = description
    return descriptions


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


def _logged_prompt_texts(logged: dict | None) -> tuple[str | None, str | None]:
    """Return positive/negative prompt text from a worker prompt log.

    New logs carry explicit ``prompt_text`` and ``negative_prompt_text`` fields.
    Older logs joined both prompt fields into ``prompt_text``; their
    ``panel_fields`` still preserve panel order, so split the first non-empty
    prompt-like field as positive and any later prompt-like fields as negative.
    """
    if not logged:
        return None, None

    panel_fields = logged.get("panel_fields")
    if isinstance(panel_fields, list):
        prompt_values = [
            str(value)
            for entry in panel_fields
            if isinstance(entry, dict)
            for key, value in entry.items()
            if key in {"prompt", "text", "positive"}
            and isinstance(value, str)
            and value
        ]
        negative_values = [
            str(value)
            for entry in panel_fields
            if isinstance(entry, dict)
            for key, value in entry.items()
            if key in {"negative", "negative_prompt"}
            and isinstance(value, str)
            and value
        ]
        if prompt_values:
            return (
                prompt_values[0],
                "\n".join([*negative_values, *prompt_values[1:]]) or None,
            )

    prompt = logged.get("prompt_text")
    negative = logged.get("negative_prompt_text")
    return (
        prompt if isinstance(prompt, str) and prompt else None,
        negative if isinstance(negative, str) and negative else None,
    )


def _variants_for_live_template() -> int:
    """How many output images the live render template emits per panel.

    The live ``templates/2`` emits a **single** image per panel. This derives
    that count from the real worker code path (``WorkflowBuilder`` + the live
    ``_RENDER_TEMPLATE_ID``) so the index math stays correct if a template is ever
    changed to emit more than one image per panel. Falls back to 1 if the imports
    change.
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
        return count or 1
    except Exception as exc:  # noqa: BLE001 - best-effort; fall back gracefully
        print(
            f"[eval] warn: could not derive image count ({exc}); assuming 1",
            file=sys.stderr,
        )
        return 1


# --- output source (GCS or local filesystem) ---------------------------------
#
# Both modes expose the same blob-like surface — ``.name`` (the
# ``<user>/<story>/outputs/<i>.png`` object key) and ``.download_to_filename`` —
# so the listing / manifest code below is mostly source-agnostic. GCS is the
# default (Application local stack); ``--local-root`` switches to in-place disk
# references.


class _LocalBlob:
    """A GCS-blob-like shim over a local file (name + download_to_filename)."""

    def __init__(self, name: str, path: Path) -> None:
        self.name = name
        self.path = path

    def download_to_filename(self, dest: str) -> None:
        shutil.copyfile(self.path, dest)


def _client(project: str):
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import storage

    return storage.Client(project=project, credentials=AnonymousCredentials())


def _local_blobs(root: Path, prefix: str = ""):
    """Yield ``_LocalBlob`` for every ``*.png`` under ``root`` whose relative
    POSIX path starts with ``prefix`` (mirrors GCS prefix listing)."""
    for path in sorted(root.rglob("*.png")):
        name = path.relative_to(root).as_posix()
        if name.startswith(prefix):
            yield _LocalBlob(name, path)


def _source_blobs(args: argparse.Namespace, prefix: str = ""):
    """List blob-likes from the configured source (local dir or GCS bucket)."""
    if args.local_root:
        return _local_blobs(Path(args.local_root).expanduser(), prefix)
    client = _client(args.project)
    bucket = client.bucket(args.bucket)
    return client.list_blobs(bucket, prefix=prefix or None)


def _source_label(args: argparse.Namespace) -> str:
    """Human-readable name of the active source, for messages + the manifest."""
    if args.local_root:
        return str(Path(args.local_root).expanduser())
    return f"gs://{args.bucket}"


def _blob_uri(args: argparse.Namespace, name: str) -> str:
    """Provenance URI for one output (a ``gs://`` URI or a local path)."""
    if args.local_root:
        return str(Path(args.local_root).expanduser() / name)
    return f"gs://{args.bucket}/{name}"


def _output_sets(args: argparse.Namespace) -> dict[tuple[str, str], int]:
    """Return ``{(user_id, story_id): image_count}`` for generated outputs."""
    sets: dict[tuple[str, str], int] = {}
    for blob in _source_blobs(args):
        m = _OUTPUT_RE.match(blob.name)
        if m:
            key = (m.group("user"), m.group("story"))
            sets[key] = sets.get(key, 0) + 1
    return sets


def _list_sets(args: argparse.Namespace) -> int:
    sets = _output_sets(args)
    label = _source_label(args)
    if not sets:
        print(f"[eval] no output sets found in {label}/*/*/outputs/")
        return 1
    print(f"[eval] output sets in {label}:")
    for (user, story), n in sorted(sets.items()):
        print(f"    --user-id {user} --story-id {story}    ({n} images)")
    return 0


def _batch_download(args: argparse.Namespace) -> int:
    """Build manifests for every generated story, using story_id as story.

    This is intended for local batch eval, where generated output dirs are named
    after the prompt stem (for example ``outputs/leo/1_7/outputs/0.png``).
    """
    sets = _output_sets(args)
    label = _source_label(args)
    if not sets:
        print(f"[eval] no output sets found in {label}/*/*/outputs/", file=sys.stderr)
        return 1
    if args.out:
        out_root = Path(args.out).expanduser()
    elif args.local_root:
        out_root = Path(args.local_root).expanduser().parent / "eval"
    else:
        out_root = _DEFAULT_EVAL_DIR

    status = 0
    for (user_id, story_id), _ in sorted(sets.items()):
        child_args = argparse.Namespace(**vars(args))
        child_args.user_id = user_id
        child_args.story_id = story_id
        child_args.story = story_id
        child_args.out = str(out_root / f"{story_id}__{story_id}")
        result = _download(child_args)
        if result != 0:
            status = result
    return status


def _download(args: argparse.Namespace) -> int:
    story_json = _PROMPTS_DIR / f"{args.story}.json"
    if not story_json.exists():
        print(f"[eval] error: prompt set not found: {story_json}", file=sys.stderr)
        return 2
    spec = json.loads(story_json.read_text())
    prompts: list[str] = spec.get("prompts", [])
    negative_prompts: list[str] = spec.get("negative_prompts") or []
    # Per-panel storybook text/dialog shown to the reader (parallel to prompts).
    texts: list[str] = spec.get("texts", [])
    # Per-panel gist: the authored, eval-ready intent of the panel (parallel to
    # prompts). The judge also checks whether the image *satisfies the gist*, not
    # just the literal prompt. Absent on older stories -> per-panel gist is None.
    gists: list[str] = spec.get("gists", [])
    characters = _load_characters()
    variants = _variants_for_live_template()
    # The actual prompts the worker logged for THIS run (keyed by panel index).
    # Present only if the dev worker ran with PROMPT_LOG_DIR set — when it is, the
    # judge grades against the real submitted prompt rather than a reconstruction.
    prompt_log = _load_prompt_log(Path(args.log_dir), args.story_id)
    if prompt_log:
        print(
            f"[eval] using actual prompt log: {len(prompt_log)} panel(s) from "
            f"{Path(args.log_dir) / args.story_id}"
        )
    else:
        print(
            f"[eval] no prompt log under {Path(args.log_dir) / args.story_id}; "
            "falling back to reconstructed prompts",
            file=sys.stderr,
        )

    out_dir = Path(
        args.out or _DEFAULT_EVAL_DIR / f"{args.story}__{args.story_id}"
    ).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    _clear_pngs(out_dir)
    use_local_refs = bool(args.local_root)

    prefix = f"{args.user_id}/{args.story_id}/outputs/"
    blobs = sorted(
        (b for b in _source_blobs(args, prefix) if b.name.endswith(".png")),
        key=lambda b: int(Path(b.name).stem),
    )
    if not blobs:
        print(
            f"[eval] error: no PNGs under {_source_label(args)}/{prefix}",
            file=sys.stderr,
        )
        print("[eval] hint: run with --list to see available sets", file=sys.stderr)
        return 3

    entries = []
    for blob in blobs:
        index = int(Path(blob.name).stem)
        panel_index = index // variants
        variant = index % variants
        # One image per panel on the live template -> no variant suffix; keep a
        # generic _vN only if a template is ever changed to emit several.
        suffix = f"_v{variant + 1}" if variants > 1 else ""
        eval_file = out_dir / f"{index:02d}_panel{panel_index + 1}{suffix}.png"
        if use_local_refs:
            local = (Path(args.local_root).expanduser() / blob.name).resolve()
        else:
            local = eval_file
            blob.download_to_filename(str(local))
        raw = prompts[panel_index] if panel_index < len(prompts) else None
        raw_negative = (
            negative_prompts[panel_index]
            if panel_index < len(negative_prompts)
            else None
        )
        reconstructed = _resolve_prompt(raw, characters) if raw else None
        reconstructed_negative = (
            _resolve_prompt(raw_negative, characters) if raw_negative else None
        )
        # Prefer the prompt the worker actually logged for this panel; only fall
        # back to reconstruction (token-expanded prompt file) when no log exists.
        logged = prompt_log.get(panel_index)
        logged_prompt, logged_negative = _logged_prompt_texts(logged)
        resolved = logged_prompt or reconstructed
        resolved_negative = logged_negative or reconstructed_negative
        entries.append(
            {
                "index": index,
                "panel_index": panel_index,
                "panel_number": panel_index + 1,
                "variant": variant,
                # The image users receive. With one image per panel this is the
                # only image; if a template emits several, the last is delivered.
                "is_delivered": variant == variants - 1,
                "file": str(local),
                "gcs": _blob_uri(args, blob.name),
                "raw_prompt": raw,
                "resolved_prompt": resolved,
                "raw_negative_prompt": raw_negative,
                "resolved_negative_prompt": resolved_negative,
                "panel_dialog": (
                    texts[panel_index] if panel_index < len(texts) else None
                ),
                "gist": gists[panel_index] if panel_index < len(gists) else None,
                # Provenance so the judge/report knows which prompt it graded.
                "prompt_source": "worker_log" if logged_prompt else "reconstructed",
                "reconstructed_prompt": reconstructed,
                "logged_prompt": logged_prompt,
                "reconstructed_negative_prompt": reconstructed_negative,
                "logged_negative_prompt": logged_negative,
                "comfyui_prompt_id": (logged or {}).get("comfyui_prompt_id"),
                "prompt_log": (logged or {}).get("_log_path"),
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
        "source": _source_label(args),
        "variants_per_panel": variants,
        "panel_count": len(prompts),
        # Whether this story carries authored per-panel gists (the intent the
        # judge also grades the image against). False on pre-gist stories.
        "has_gists": bool(gists),
        # Which prompt the judge should grade against: "worker_log" means each
        # entry's resolved_prompt is the ACTUAL prompt the worker logged; mixed/
        # reconstructed means some/all fell back to re-deriving from the file.
        "prompt_source": _manifest_prompt_source(entries),
        "out_dir": str(out_dir),
        "report_path": str(out_dir / "report.md"),
        "uses_local_output_refs": use_local_refs,
        "images": entries,
    }
    _write_manifest(out_dir, manifest)
    _mark_report_outdated(out_dir / "report.md")
    mirror_dir = _mirror_to_latest(out_dir, manifest)
    if mirror_dir is not None:
        _mark_report_outdated(mirror_dir / "report.md")

    print(f"[eval] story {args.story!r}: {spec.get('title')!r}")
    verb = "referenced" if use_local_refs else "downloaded"
    print(
        f"[eval] {verb} {len(entries)} image(s) "
        f"({len(prompts)} panels x {variants} image(s)/panel); eval artifacts -> {out_dir}"
    )
    print(f"[eval] manifest -> {out_dir / 'manifest.json'}")
    if mirror_dir is not None:
        print(f"[eval] mirrored latest review artifacts -> {mirror_dir}")
    print(f"[eval] write the report to -> {out_dir / 'report.md'}")
    if len(entries) != len(prompts) * variants:
        print(
            f"[eval] warn: expected {len(prompts) * variants} images, "
            f"got {len(entries)} (set may be partial)",
            file=sys.stderr,
        )
    return 0


def _manifest_prompt_source(entries: list[dict]) -> str:
    """'worker_log' if every panel used the log, 'reconstructed' if none did,
    else 'mixed' — so the report can flag a partial/absent prompt log."""
    sources = {e["prompt_source"] for e in entries if e.get("resolved_prompt")}
    if sources == {"worker_log"}:
        return "worker_log"
    if sources == {"reconstructed"}:
        return "reconstructed"
    return "mixed" if sources else "none"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list <user>/<story> output sets in the bucket and exit",
    )
    parser.add_argument(
        "--story",
        help="prompt-file stem / worker type_id, e.g. 1_1 (loads imagegen/prompts/<story>.json)",
    )
    parser.add_argument("--user-id", help="GCS path user_id component")
    parser.add_argument(
        "--story-id",
        help="GCS path story_id (Application job id; equals --story in local-batch-eval, differs in the Application stack)",
    )
    parser.add_argument(
        "--out",
        help=(
            "eval artifact dir (default " "eval_runs/latest/eval/<story>__<story_id>)"
        ),
    )
    parser.add_argument(
        "--bucket", default=os.environ.get("GCS_BUCKET", _DEFAULT_BUCKET)
    )
    parser.add_argument(
        "--project", default=os.environ.get("GCP_PROJECT_ID", _DEFAULT_PROJECT)
    )
    parser.add_argument(
        "--local-root",
        default=os.environ.get("LOCAL_OUTPUT_ROOT"),
        help="read outputs from this local dir tree (<user>/<story>/outputs/<i>.png) "
        "instead of GCS — set when generating locally from this repo",
    )
    parser.add_argument(
        "--log-dir",
        default=os.environ.get("PROMPT_LOG_DIR_HOST", str(_DEFAULT_LOG_DIR)),
        help="dir of worker actual-prompt logs (default repo-root prompt_logs/)",
    )
    args = parser.parse_args(argv)

    # The fake-gcs endpoint only matters in GCS mode; local mode needs no emulator
    # (and no google-cloud-storage import).
    if not args.local_root:
        os.environ.setdefault("STORAGE_EMULATOR_HOST", _DEFAULT_GCS_HOST)

    if args.list:
        return _list_sets(args)

    provided = {n for n in ("story", "user_id", "story_id") if getattr(args, n)}
    if not provided:
        return _batch_download(args)

    missing = sorted({"story", "user_id", "story_id"} - provided)
    if missing:
        parser.error(
            "single-story download mode needs --"
            + ", --".join(m.replace("_", "-") for m in missing)
            + "; omit all three selector args to build every generated story manifest"
        )
    return _download(args)


if __name__ == "__main__":
    sys.exit(main())
