"""Unit tests for operation/sync_story_catalog.py — the pure binding/mapping
logic (the Firestore write is exercised against the emulator out of band).

The module lives under operation/ (not a package), so load it by path. Its
firestore import is lazy (inside main), so importing here needs no
google-cloud-firestore.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODULE_PATH = _REPO_ROOT / "operation" / "sync_story_catalog.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sync_story_catalog", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_story_doc_maps_prompt_fields() -> None:
    mod = _load()
    prompt = {
        "story_type": 1,
        "story_type_name": "life_lesson",
        "story_number": 7,
        "title": "The Magic Words",
        "lesson": 'Kind words like "please" and "thank you" make people smile.',
        "version": 3,
        "prompts": ["...ignored..."],
    }

    doc = mod._story_doc(prompt)

    assert doc == {
        "story_type": 1,
        "story_type_name": "life_lesson",
        "story_number": 7,
        "title": "The Magic Words",
        "lesson": 'Kind words like "please" and "thank you" make people smile.',
        "story_version": 3,  # prompt "version" -> "story_version"
    }


def test_story_doc_tolerates_missing_optional_fields() -> None:
    mod = _load()
    doc = mod._story_doc({"title": "x"})
    assert doc["story_type"] is None
    assert doc["story_type_name"] == ""
    assert doc["story_version"] is None


def test_bindings_resolve_real_templates() -> None:
    """Guards the worker's template->story binding (template 4 -> story 1_1)."""
    mod = _load()
    bindings = dict(mod._bindings(None))

    assert "4" in bindings, "template 4 must bind a story"
    assert bindings["4"]["title"] == "Kindness Comes Back Around"
    assert bindings["4"]["story_type"] == 1
    assert bindings["4"]["story_number"] == 1
    # Template 3 ("custom") binds no story and is skipped.
    assert "3" not in bindings


def test_bindings_filter_by_template() -> None:
    mod = _load()
    bindings = dict(mod._bindings("4"))
    assert set(bindings) == {"4"}
