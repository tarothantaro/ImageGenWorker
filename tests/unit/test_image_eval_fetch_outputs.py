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
    spec = importlib.util.spec_from_file_location(
        "image_eval_fetch_outputs", _MODULE_PATH
    )
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
        story_outputs = outputs / "liam" / stem / "outputs"
        story_outputs.mkdir(parents=True)
        (story_outputs / "0.png").write_bytes(b"png bytes")
    eval_dir = tmp_path / "eval"

    monkeypatch.setattr(mod, "_PROMPTS_DIR", prompts_dir)
    monkeypatch.setattr(mod, "_DEFAULT_EVAL_DIR", eval_dir)
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
    assert manifest_1["user_id"] == "liam"
    assert manifest_1["images"][0]["panel_dialog"] == "Dialog for one."
    assert manifest_2["story"] == "1_2"
    assert manifest_2["story_id"] == "1_2"
    assert manifest_2["images"][0]["panel_dialog"] == "Dialog for two."


def test_download_mirrors_latest_review_artifacts_and_marks_reports_outdated(
    tmp_path: Path, monkeypatch
) -> None:
    mod = _load()
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "character.json").write_text(json.dumps({"characters": {}}))
    _write_story(prompts_dir, "1_1", "Dialog for one.")
    outputs = tmp_path / "outputs" / "liam" / "app-story" / "outputs"
    outputs.mkdir(parents=True)
    (outputs / "0.png").write_bytes(b"png bytes")
    out_dir = tmp_path / "custom-eval"
    latest_root = tmp_path / "latest" / "eval"
    latest_dir = latest_root / "1_1__app-story"
    out_dir.mkdir()
    latest_dir.mkdir(parents=True)
    (out_dir / "report.md").write_text("# Custom Report\n\nold\n")
    (latest_dir / "report.md").write_text("# Latest Report\n\nold\n")
    (out_dir / "stale.png").write_bytes(b"old")
    (latest_dir / "stale.png").write_bytes(b"old")

    monkeypatch.setattr(mod, "_PROMPTS_DIR", prompts_dir)
    monkeypatch.setattr(mod, "_DEFAULT_EVAL_DIR", latest_root)
    monkeypatch.setattr(mod, "_variants_for_live_template", lambda: 1)

    result = mod.main(
        [
            "--local-root",
            str(tmp_path / "outputs"),
            "--log-dir",
            str(tmp_path / "prompt_logs"),
            "--out",
            str(out_dir),
            "--story",
            "1_1",
            "--story-id",
            "app-story",
            "--user-id",
            "liam",
        ]
    )

    assert result == 0
    assert (out_dir / "00_panel1.png").read_bytes() == b"png bytes"
    assert (latest_dir / "00_panel1.png").read_bytes() == b"png bytes"
    assert not (out_dir / "stale.png").exists()
    assert not (latest_dir / "stale.png").exists()
    latest_manifest = json.loads((latest_dir / "manifest.json").read_text())
    assert latest_manifest["images"][0]["file"] == str(latest_dir / "00_panel1.png")
    assert latest_manifest["images"][0]["raw_prompt"] == "Prompt for 1_1"
    assert latest_manifest["images"][0]["gist"] == "Gist for 1_1"
    assert latest_manifest["images"][0]["panel_dialog"] == "Dialog for one."
    assert latest_manifest["out_dir"] == str(latest_dir)
    assert latest_manifest["report_path"] == str(latest_dir / "report.md")
    assert (
        "> WARNING: This eval report is outdated."
        in (out_dir / "report.md").read_text()
    )
    assert (
        "> WARNING: This eval report is outdated."
        in (latest_dir / "report.md").read_text()
    )


def test_mark_reports_outdated_can_mark_all_reports(tmp_path: Path) -> None:
    mod = _load()
    eval_root = tmp_path / "eval"
    report_1 = eval_root / "1_1__1_1" / "report.md"
    report_2 = eval_root / "1_14__1_14" / "report.md"
    report_1.parent.mkdir(parents=True)
    report_2.parent.mkdir(parents=True)
    report_1.write_text("# One\n")
    report_2.write_text("# Two\n")

    mod.mark_reports_outdated(eval_root)

    assert "> WARNING: This eval report is outdated." in report_1.read_text()
    assert "> WARNING: This eval report is outdated." in report_2.read_text()


def test_partial_story_args_still_error() -> None:
    mod = _load()

    try:
        mod.main(["--story", "1_1"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("expected argparse to reject partial selector args")
