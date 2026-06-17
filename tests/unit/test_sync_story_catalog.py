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
    """Guards the per-story binding convention: each ``templates/<type>_<n>``
    binds story ``<type>_<n>`` (template id == story id), so the catalog sync
    writes one ``templates/{id}`` doc per prompt. Legacy template 4 still binds
    story 1_1 for back-compat; template 3 ("custom") binds no story."""
    mod = _load()
    bindings = dict(mod._bindings(None))

    # Every prompts/<id>.json has a matching templates/<id> that binds it.
    assert "1_1" in bindings
    assert bindings["1_1"]["title"] == "Kindness Comes Back Around"
    assert bindings["1_1"]["story_type"] == 1
    assert bindings["1_1"]["story_number"] == 1
    # The full life-lesson catalog (1_1..1_21) is present and each carries a
    # non-empty title — its story id == its template id.
    story_ids = {b for b in bindings if b.startswith("1_")}
    assert story_ids == {f"1_{n}" for n in range(1, 22)}
    assert all(bindings[s]["title"] for s in story_ids)

    # Legacy template 4 still binds story 1_1 (back-compat).
    assert bindings["4"]["story_number"] == 1
    # Template 3 ("custom") binds no story and is skipped.
    assert "3" not in bindings


def test_bindings_filter_by_template() -> None:
    mod = _load()
    bindings = dict(mod._bindings("1_5"))
    assert set(bindings) == {"1_5"}
    assert bindings["1_5"]["title"] == "Tidy-Up Time"
