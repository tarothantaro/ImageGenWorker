from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODULE_PATH = _REPO_ROOT / "tools" / "review_app" / "server.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("review_app_server", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parse_report_extracts_warning_from_report_body() -> None:
    mod = _load()
    report = """# Eval

> WARNING: This eval report is outdated.

- **Verdict:** ship

## Summary
- Good enough.
"""

    parsed = mod._parse_report(report)

    assert parsed["warnings"] == ["WARNING: This eval report is outdated."]
    assert "WARNING" not in parsed["rest"]
    assert "Good enough" in parsed["summary"]


def test_render_story_places_warning_block_before_summary() -> None:
    mod = _load()
    story = {
        "dir": "1_14__1_14",
        "manifest": {
            "title": "Come Play With Us",
            "lesson": "Invite someone in.",
            "source": "eval_runs/latest/outputs",
            "prompt_source": "worker_log",
            "images": [],
        },
        "report": {
            "verdict": "ship",
            "warnings": ["WARNING: This eval report is outdated."],
            "summary": "- Summary text.",
            "panels": {},
            "rest": "",
        },
        "status": {},
    }
    run = {"stories": [story]}

    html = mod._render_story(run, story, "latest").decode("utf-8")

    warning_pos = html.index('class="warning-subblock"')
    summary_pos = html.index("Summary text.")
    assert warning_pos < summary_pos
    assert "Outdated Eval" in html
    assert "WARNING: This eval report is outdated." in html


def test_render_story_shows_panel_gist_without_eval_notes() -> None:
    mod = _load()
    story = {
        "dir": "1_14__1_14",
        "manifest": {
            "title": "Come Play With Us",
            "source": "eval_runs/latest/outputs",
            "prompt_source": "worker_log",
            "images": [
                {
                    "panel_number": 1,
                    "index": 0,
                    "variant": 0,
                    "file": "eval_runs/latest/outputs/liam/1_14/outputs/0.png",
                    "resolved_prompt": "A child waves from a playground.",
                    "gist": "The child notices someone playing alone.",
                }
            ],
        },
        "report": {
            "verdict": "",
            "warnings": [],
            "summary": "",
            "panels": {},
            "rest": "",
        },
        "status": {},
    }
    run = {"stories": [story]}

    html = mod._render_story(run, story, "latest").decode("utf-8")

    assert '<div class="gist"><strong>Gist</strong>' in html
    assert "The child notices someone playing alone." in html
    assert "/img?dir=1_14__1_14&index=0" in html
    assert "No eval notes for this panel." in html


def test_manifest_image_path_resolves_output_file_inside_run_dir(
    tmp_path: Path,
) -> None:
    mod = _load()
    run_dir = tmp_path / "latest"
    output = run_dir / "outputs" / "liam" / "1_14" / "outputs" / "0.png"
    eval_dir = run_dir / "eval" / "1_14__1_14"
    output.parent.mkdir(parents=True)
    eval_dir.mkdir(parents=True)
    output.write_bytes(b"png bytes")
    (eval_dir / "manifest.json").write_text(
        json.dumps(
            {
                "images": [
                    {
                        "index": 0,
                        "file": str(output),
                    }
                ]
            }
        )
    )

    assert mod._manifest_image_path(run_dir, "1_14__1_14", "0") == output.resolve()


def test_manifest_image_path_rejects_file_outside_run_dir(tmp_path: Path) -> None:
    mod = _load()
    run_dir = tmp_path / "latest"
    eval_dir = run_dir / "eval" / "1_14__1_14"
    outside = tmp_path / "outside.png"
    eval_dir.mkdir(parents=True)
    outside.write_bytes(b"png bytes")
    (eval_dir / "manifest.json").write_text(
        json.dumps(
            {
                "images": [
                    {
                        "index": 0,
                        "file": str(outside),
                    }
                ]
            }
        )
    )

    assert mod._manifest_image_path(run_dir, "1_14__1_14", "0") is None
