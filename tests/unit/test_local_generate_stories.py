from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODULE_PATH = (
    _REPO_ROOT / ".claude" / "skills" / "local-batch-eval" / "generate_stories.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "local_generate_stories", _MODULE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_discover_stories_filters_supporting_character_configs(tmp_path, monkeypatch):
    mod = _load()
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    for filename in [
        "1_10.json",
        "1_2.json",
        "2_1.json",
        "character.json",
        "adventure_character.json",
        "draft_1.json",
        "1_draft.json",
    ]:
        (prompts_dir / filename).write_text("{}")
    monkeypatch.setattr(mod, "_PROMPTS_DIR", prompts_dir)

    stories = mod._discover_stories(None)

    assert stories == ["1_2", "1_10", "2_1"]


def test_discover_stories_preserves_explicit_subset():
    mod = _load()

    stories = mod._discover_stories("2_1, 1_10")

    assert stories == ["2_1", "1_10"]


def test_render_template_ids_for_stories_reflects_story_type():
    mod = _load()

    ids = mod._render_template_ids_for_stories(["1_10", "2_1"])

    assert ids == {"1_10": "2", "2_1": "3"}
