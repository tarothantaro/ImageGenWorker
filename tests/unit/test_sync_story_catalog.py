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
        "type": 1,
        "id": 7,
        "title": "The Magic Words",
        "lesson": 'Kind words like "please" and "thank you" make people smile.',
        "version": 3,
        "prompts": ["...ignored..."],
        "texts": ["You sat at the table.", '"Please?" you asked.'],
    }

    doc = mod._story_doc(prompt)

    assert doc == {
        "story_type": 1,  # prompt "type" -> Firestore "story_type"
        "story_type_name": "life_lesson",  # mapped from type in code
        "story_number": 7,  # prompt "id" -> Firestore "story_number"
        "title": "The Magic Words",
        "lesson": 'Kind words like "please" and "thank you" make people smile.',
        "story_version": 3,  # prompt "version" -> "story_version"
        # prompt "texts" (per-panel storybook narration) -> "story_text"
        "story_text": ["You sat at the table.", '"Please?" you asked.'],
        "example_image_urls": [],
    }


def test_story_doc_tolerates_missing_optional_fields() -> None:
    mod = _load()
    doc = mod._story_doc({"title": "x"})
    assert doc["story_type"] is None
    assert doc["story_type_name"] == ""
    assert doc["story_version"] is None
    assert doc["story_text"] == []  # missing "texts" -> empty list, not absent
    assert doc["example_image_urls"] == []


def test_bindings_include_sorted_example_urls(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_EMULATOR_HOST", raising=False)
    mod = _load()
    outputs = tmp_path / "1_1" / "outputs"
    outputs.mkdir(parents=True)
    for name in ("10.png", "2.png", "0.png"):
        (outputs / name).write_bytes(b"png")

    bindings = dict(
        mod._bindings(
            "1_1",
            example_root=tmp_path,
            bucket_name="catalog-bucket",
            dry_run=True,
        )
    )

    assert bindings["1_1"]["example_image_urls"] == [
        "https://storage.googleapis.com/catalog-bucket/"
        "catalog/examples/liam/1_1/0.png",
        "https://storage.googleapis.com/catalog-bucket/"
        "catalog/examples/liam/1_1/2.png",
        "https://storage.googleapis.com/catalog-bucket/"
        "catalog/examples/liam/1_1/10.png",
    ]


def test_bindings_resolve_real_prompts() -> None:
    """The catalog sync writes one ``templates/<type>_<id>`` doc per prompt set
    (the doc id == the prompt file stem == the catalog id the API serves)."""
    mod = _load()
    bindings = dict(mod._bindings(None))

    assert "1_1" in bindings
    assert bindings["1_1"]["title"] == "Kindness Comes Back Around"
    assert bindings["1_1"]["story_type"] == 1
    assert bindings["1_1"]["story_type_name"] == "life_lesson"
    assert bindings["1_1"]["story_number"] == 1
    # The full life-lesson catalog (1_1..1_21) is present and each carries a
    # non-empty title.
    life_lesson_ids = {f"1_{n}" for n in range(1, 22)}
    assert set(bindings) == life_lesson_ids | {"2_1"}
    assert all(bindings[s]["title"] for s in bindings)
    # Every life-lesson story also carries 6 per-panel storybook lines
    # (story_text), one per prompt/panel — the `story-text` skill's output,
    # surfaced to the catalog.
    assert len(bindings["1_1"]["story_text"]) == 6
    assert life_lesson_ids.issubset(bindings)
    assert all(len(bindings[s]["story_text"]) == 6 for s in life_lesson_ids)
    assert bindings["2_1"]["story_type_name"] == "adventure"
    assert len(bindings["2_1"]["story_text"]) == 12


def test_bindings_filter_by_template() -> None:
    mod = _load()
    bindings = dict(mod._bindings("1_5"))
    assert set(bindings) == {"1_5"}
    assert bindings["1_5"]["title"] == "Tidy-Up Time"
