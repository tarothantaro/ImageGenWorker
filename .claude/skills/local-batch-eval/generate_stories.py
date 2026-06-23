#!/usr/bin/env python3
"""Drive the real :class:`~imagegen.model.ComfyUIModel` directly for a batch of
stories — no Pub/Sub, no GCS, no Application stack — and write everything the
``local-batch-eval`` / ``image-eval`` skills need straight to a local run dir.

This is the *generate* half of the local eval loop. Where ``smoke_real_comfyui``
runs one story, this iterates **all** story prompt sets (``imagegen/prompts/<type>_<id>.json``,
or a ``--stories`` subset), running each through the live render template against
a single input photo at a fixed age, and lays the outputs out exactly like the
production GCS bucket — only on disk — so the unchanged ``fetch_outputs.py
--local-root`` can read them back.

It needs a **live ComfyUI** on ``--url`` (default :8188), same as the smoke test;
generation is real and slow (each panel is one ComfyUI run of minutes).

Run dir layout (mirrors the worker's GCS object names under ``outputs/``)::

    <run-dir>/
    ├── run.json                                  # batch metadata (input, age, per-story status)
    ├── input.<ext>                               # copy of the input photo, for the review UI
    ├── outputs/<user>/<story_id>/outputs/<i>.png # GCS-mirror — point --local-root here
    └── prompt_logs/<story_id>/panel_<NN>.json    # actual prompt + workflow per panel

``user_id`` defaults to the input filename stem; ``story_id`` is the prompt-file
stem itself (e.g. ``1_1``), so for the eval step ``--story``, ``--story-id`` and
the prompt-log dir all line up with no Application-assigned id to track.

Usage (from the repo root, ComfyUI up on :8188)::

    PYTHONPATH=. ~/python_env/torch-env/bin/python \
        .claude/skills/local-batch-eval/generate_stories.py \
        --input tests/assets/leo.jpg --age "4-year-old" \
        --run-dir eval_runs/latest

    # just two stories, against a remote ComfyUI
    PYTHONPATH=. ~/python_env/torch-env/bin/python \
        .claude/skills/local-batch-eval/generate_stories.py \
        --stories 1_1,1_2 --url http://localhost:8188 --run-dir eval_runs/latest

Exits non-zero if **any** story failed (the rest still run + are recorded).
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from imagegen.comfyui_client import HttpComfyUIClient
from imagegen.model import _RENDER_TEMPLATE_ID, ComfyUIModel

# This script lives at .claude/skills/local-batch-eval/generate_stories.py, so
# the repo root is four parents up.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROMPTS_DIR = _REPO_ROOT / "imagegen" / "prompts"

log = logging.getLogger("generate_stories")


def _discover_stories(arg: str | None) -> list[str]:
    """Return the ordered list of story stems to generate.

    With ``--stories`` it's that explicit comma/space list; otherwise every
    ``<type>_<id>.json`` in the prompts dir (``character.json`` excluded),
    numerically sorted so ``1_2`` precedes ``1_10``.
    """
    if arg:
        return [s for s in arg.replace(",", " ").split() if s]
    stems = [
        p.stem
        for p in _PROMPTS_DIR.glob("*.json")
        if p.stem != "character" and "_" in p.stem
    ]
    return sorted(stems, key=_story_sort_key)


def _story_sort_key(stem: str) -> tuple[int, int, str]:
    try:
        t, i = stem.split("_", 1)
        return (int(t), int(i), stem)
    except ValueError:
        return (1 << 30, 1 << 30, stem)


def _parse_story(stem: str) -> tuple[int, int]:
    """``"1_10"`` -> ``(1, 10)``; raises ValueError on a non ``type_id`` stem."""
    t, i = stem.split("_", 1)
    return int(t), int(i)


def _generate_one(
    model: ComfyUIModel,
    *,
    stem: str,
    user_id: str,
    age: str | None,
    image_bytes: bytes,
    outputs_root: Path,
) -> dict:
    """Generate every panel of one story, writing PNGs in GCS-mirror layout.

    Returns a per-story status record for ``run.json``. Never raises — a failed
    story is captured (``status='failed'``) so the batch keeps going.
    """
    prompt_type, prompt_id = _parse_story(stem)
    out_dir = outputs_root / user_id / stem / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    written = 0
    try:
        panels = model.generate(
            story_id=stem,
            user_id=user_id,
            prompt_type=prompt_type,
            prompt_id=prompt_id,
            input_images=[image_bytes],
            input_ages=[age],
        )
        for panel in panels:
            (out_dir / f"{panel.index}.png").write_bytes(panel.image)
            written += 1
            log.info(
                "  %s panel %d/%d %dx%d %.1fs",
                stem,
                panel.index + 1,
                panel.total,
                panel.width,
                panel.height,
                panel.processing_seconds,
            )
    except Exception as exc:  # noqa: BLE001 — one bad story must not sink the batch
        log.error(
            "  %s FAILED after %d image(s): %s: %s",
            stem,
            written,
            type(exc).__name__,
            exc,
        )
        return {
            "story": stem,
            "story_id": stem,
            "type": prompt_type,
            "id": prompt_id,
            "status": "failed",
            "images_written": written,
            "error": f"{type(exc).__name__}: {exc}",
            "seconds": round(time.monotonic() - started, 1),
        }
    return {
        "story": stem,
        "story_id": stem,
        "type": prompt_type,
        "id": prompt_id,
        "status": "ok",
        "images_written": written,
        "error": None,
        "seconds": round(time.monotonic() - started, 1),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--input",
        default="tests/assets/leo.jpg",
        help="input photo placed into every panel",
    )
    parser.add_argument(
        "--age",
        default="4-year-old",
        help="age phrase substituted for {INPUT_1_AGE} (e.g. '4-year-old')",
    )
    parser.add_argument(
        "--url", default="http://localhost:8188", help="live ComfyUI base URL"
    )
    parser.add_argument(
        "--run-dir",
        default="eval_runs/latest",
        help="output run dir (default eval_runs/latest)",
    )
    parser.add_argument(
        "--stories",
        default=None,
        help="comma/space list of story stems (default: all 1_*.json)",
    )
    parser.add_argument(
        "--user-id",
        default=None,
        help="GCS-path user component (default: input filename stem)",
    )
    parser.add_argument(
        "--timeout", type=float, default=300.0, help="per-panel ComfyUI timeout seconds"
    )
    parser.add_argument("--model-version", default="comfyui-qwen-edit-2511")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    input_path = Path(args.input).expanduser()
    if not input_path.exists():
        log.error("input photo not found: %s", input_path)
        return 2
    image_bytes = input_path.read_bytes()
    user_id = args.user_id or input_path.stem

    stories = _discover_stories(args.stories)
    if not stories:
        log.error("no stories to generate (looked in %s)", _PROMPTS_DIR)
        return 2

    run_dir = Path(args.run_dir).expanduser()
    outputs_root = run_dir / "outputs"
    prompt_logs = run_dir / "prompt_logs"
    run_dir.mkdir(parents=True, exist_ok=True)
    # Copy the input photo next to the run so the review UI can show it without
    # reaching back to wherever --input pointed.
    input_copy = run_dir / f"input{input_path.suffix.lower() or '.jpg'}"
    shutil.copyfile(input_path, input_copy)

    transport = HttpComfyUIClient(base_url=args.url, timeout=args.timeout)
    model = ComfyUIModel(
        transport,
        model_version=args.model_version,
        request_timeout_seconds=args.timeout,
        prompt_log_dir=prompt_logs,
    )

    log.info(
        "generating %d stor%s from %s (age=%r) -> %s",
        len(stories),
        "y" if len(stories) == 1 else "ies",
        input_path,
        args.age,
        run_dir,
    )
    batch_started = time.monotonic()
    records: list[dict] = []
    try:
        for n, stem in enumerate(stories, 1):
            log.info("[%d/%d] %s", n, len(stories), stem)
            records.append(
                _generate_one(
                    model,
                    stem=stem,
                    user_id=user_id,
                    age=args.age or None,
                    image_bytes=image_bytes,
                    outputs_root=outputs_root,
                )
            )
    finally:
        transport.close()

    run_meta = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "input_image": input_copy.name,
        "input_image_source": str(input_path),
        "age": args.age or None,
        "user_id": user_id,
        "comfyui_url": args.url,
        "model_version": args.model_version,
        "render_template_id": _RENDER_TEMPLATE_ID,
        "outputs_root": str(outputs_root),
        "prompt_logs": str(prompt_logs),
        "seconds": round(time.monotonic() - batch_started, 1),
        "stories": records,
    }
    (run_dir / "run.json").write_text(json.dumps(run_meta, indent=2))

    ok = [r for r in records if r["status"] == "ok"]
    failed = [r for r in records if r["status"] != "ok"]
    log.info(
        "done: %d ok, %d failed in %.1fs -> %s",
        len(ok),
        len(failed),
        run_meta["seconds"],
        run_dir / "run.json",
    )
    if failed:
        log.warning("failed stories: %s", ", ".join(r["story"] for r in failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
