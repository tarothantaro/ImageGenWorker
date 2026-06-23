"""Unit tests for image-eval/fetch_outputs.py manifest-building behavior.

The helper lives under .claude/skills (not a package), so load it by path. Tests
use --local-root to avoid GCS and monkeypatch the live-template variant count so
no workflow/model imports are needed for these CLI invariants.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODULE_PATH = _REPO_ROOT / ".claude" / "skills" / "image-eval" / "fetch_outputs.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("image_eval_fetch_outputs", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_story(prompts_dir: Path, stem: str, text: str) -> None:
    (prompts_dir / f"{stem}.json").write_text(
        json.dumps(
            {
                "title": f"Story {stem}",
                "lesson": "Share clearly.",
                "characters": [],
                "prompts": [f"Prompt for {stem}"],
                "texts": [text],
                "gists": [f"Gist for {stem}"],
            }
        )
    )


def test_omitted_story_args_build_manifests_for_every_generated_story(
    tmp_path: Path, monkeypatch
) -> None:
    mod = _load()
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "character.json").write_text(json.dumps({"characters": {}}))
    _write_story(prompts_dir, "1_1", "Dialog for one.")
    _write_story(prompts_dir, "1_2", "Dialog for two.")
    outputs = tmp_path / "outputs"
    for stem in ("1_1", "1_2"):
        story_outputs = outputs / "leo" / stem / "outputs"
        story_outputs.mkdir(parents=True)
        (story_outputs / "0.png").write_bytes(b"png bytes")
    eval_dir = tmp_path / "eval"

    monkeypatch.setattr(mod, "_PROMPTS_DIR", prompts_dir)
    monkeypatch.setattr(mod, "_variants_for_live_template", lambda: 1)

    result = mod.main(
        [
            "--local-root",
            str(outputs),
            "--log-dir",
            str(tmp_path / "prompt_logs"),
            "--out",
            str(eval_dir),
        ]
    )

    assert result == 0
    manifest_1 = json.loads((eval_dir / "1_1__1_1" / "manifest.json").read_text())
    manifest_2 = json.loads((eval_dir / "1_2__1_2" / "manifest.json").read_text())
    assert manifest_1["story"] == "1_1"
    assert manifest_1["story_id"] == "1_1"
    assert manifest_1["user_id"] == "leo"
    assert manifest_1["images"][0]["panel_dialog"] == "Dialog for one."
    assert manifest_2["story"] == "1_2"
    assert manifest_2["story_id"] == "1_2"
    assert manifest_2["images"][0]["panel_dialog"] == "Dialog for two."


def test_partial_story_args_still_error() -> None:
    mod = _load()

    try:
        mod.main(["--story", "1_1"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("expected argparse to reject partial selector args")
