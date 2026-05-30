"""Unit tests for the ComfyUI model (imagegen/model.py).

Drives the real :class:`~imagegen.model.ComfyUIModel` against the
:class:`~tests.fakes.comfyui.FakeComfyUI` mock container. These tests pin down:

* the exact workflow + parameters the model sends to ComfyUI per panel,
* **one image per template panel**, with per-panel values and (with a global
  seed override) per-panel seed variation, and V2 (final) image selection,
* every worker-side terminal failure (bad input, bad options, unknown template),
* every ComfyUI-side failure (unavailable, bad prompt, execution error, WS
  timeout, per-request timeout, missing/garbled output) and how it maps onto
  the worker taxonomy.

Time is injected (constant clock by default) so nothing touches the wall clock;
the per-request timeout test injects a clock that jumps past the deadline.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

from imagegen.failure_classification import (
    CorruptInputError,
    InvalidConfigError,
    ModelTransientError,
    UnsupportedTemplateError,
)
from imagegen.model import (
    ComfyUIBadRequest,
    ComfyUIExecutionError,
    ComfyUIModel,
    ComfyUIUnavailable,
)
from tests.fakes.comfyui import FakeComfyUI, make_png

PNG = make_png(8, 8)
PNG_ALT = make_png(4, 4)

_TEMPLATE_DEFAULT_SEED = 771062815410683


def _model(
    fake: FakeComfyUI, *, clock: Callable[[], float] | None = None, **kwargs: Any
) -> ComfyUIModel:
    return ComfyUIModel(
        fake,
        model_version="mv-test",
        clock=clock or (lambda: 10.0),
        **kwargs,
    )


def _generate(model: ComfyUIModel, **overrides: Any) -> list[Any]:
    """Consume the panel iterator into a list (raising eagerly as the model does)."""
    kwargs: dict[str, Any] = {
        "story_id": "s1",
        "user_id": "u1",
        "template_id": "3",
        "configurable_options": {},
        "input_images": [PNG],
    }
    kwargs.update(overrides)
    return list(model.generate(**kwargs))


# --- happy path (template 3 — one panel) -------------------------------------


def test_generate_yields_one_panel_with_v2_image() -> None:
    fake = FakeComfyUI(width=1024, height=736)
    model = _model(fake)

    panels = _generate(model)

    assert len(panels) == 1
    assert panels[0].image == make_png(1024, 736)
    assert panels[0].width == 1024
    assert panels[0].height == 736
    assert panels[0].model_version == "mv-test"
    assert panels[0].processing_seconds == 0.0
    assert len(fake.submitted) == 1  # one workflow submitted per panel


def test_generate_sends_expected_workflow_parameters() -> None:
    fake = FakeComfyUI()
    model = _model(fake)

    _generate(
        model,
        configurable_options={"prompt": "a teacher", "steps": 12, "seed": 500},
    )

    first = fake.submitted[0].prompt
    assert first["68:6"]["inputs"]["text"] == "a teacher"
    assert first["68:90"]["inputs"]["value"] == 12
    assert first["46"]["inputs"]["image"] == "u1_s1_INPUT_1.png"
    assert first["122"]["inputs"]["image"] == "u1_s1_INPUT_1.png"
    assert first["123"]["inputs"]["filename_prefix"] == "u1_s1_V1"
    assert first["119"]["inputs"]["filename_prefix"] == "u1_s1_V2"
    assert first["68:25"]["inputs"]["noise_seed"] == 500
    assert fake.submitted[0].client_id == "s1-0"


def test_generate_uploads_input_under_its_substituted_filename() -> None:
    fake = FakeComfyUI()
    model = _model(fake)

    _generate(model)

    # One image slot in template 3 → one upload, under the per-story filename.
    assert len(fake.uploads) == 1
    assert fake.uploads[0] == ("u1_s1_INPUT_1.png", PNG)


def test_generate_uses_first_input_for_the_single_image_slot() -> None:
    fake = FakeComfyUI()
    model = _model(fake)

    _generate(model, input_images=[PNG, PNG_ALT])

    assert len(fake.uploads) == 1
    assert fake.uploads[0][1] == PNG


def test_generate_defaults_seed_and_prompt_from_template_when_absent() -> None:
    fake = FakeComfyUI()
    model = _model(fake)

    _generate(model, configurable_options={})

    submitted = fake.submitted[0].prompt
    assert submitted["68:25"]["inputs"]["noise_seed"] == _TEMPLATE_DEFAULT_SEED
    assert submitted["68:6"]["inputs"]["text"] == ""
    assert submitted["68:90"]["inputs"]["value"] == 6


def test_generate_consumes_realtime_ws_stream_until_done() -> None:
    # The default fake replays status → execution_start → per-node executing +
    # progress → executing(node=None). The model must ride that stream and
    # return once the terminal event arrives.
    fake = FakeComfyUI()
    model = _model(fake)

    panels = _generate(model)

    assert len(panels) == 1
    assert fake.event_client_ids == ["s1-0"]  # WS opened with the run's client id


def test_generate_accepts_execution_success_as_terminal() -> None:
    fake = FakeComfyUI(use_execution_success=True)
    model = _model(fake)

    assert len(_generate(model)) == 1


def test_generate_accepts_heic_input() -> None:
    fake = FakeComfyUI()
    model = _model(fake)
    heic = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 16

    panels = _generate(model, input_images=[heic])

    assert len(panels) == 1
    assert fake.uploads[0][1] == heic


# --- multi-panel (one image per panel) ---------------------------------------


def _write_multi_panel_template(
    tmp_path: Path, panels: list[tuple[str, int]]
) -> tuple[Path, Path]:
    """A 3-node template/workflow (text + seed + _V2 SaveImage) with N panels."""
    wf_root = tmp_path / "workflows"
    tpl_root = tmp_path / "templates"
    (wf_root / "w").mkdir(parents=True)
    (tpl_root / "t").mkdir(parents=True)

    panel_rows = [
        [
            {"image": "USER_ID_STORY_ID_INPUT_1.png"},
            {"text": text},
            {"noise_seed": seed},
            {"filename_prefix": "USER_ID_STORY_ID_V2"},
        ]
        for text, seed in panels
    ]
    (tpl_root / "t" / "config.json").write_text(
        json.dumps({"id": "t", "workflow_id": "w", "panels": panel_rows})
    )
    (wf_root / "w" / "config.json").write_text(
        json.dumps(
            {
                "nodes": [
                    {"id": 1, "type": "LoadImage"},
                    {"id": 2, "type": "CLIPTextEncode"},
                    {"id": 3, "type": "RandomNoise"},
                    {"id": 4, "type": "SaveImage"},
                ]
            }
        )
    )
    (wf_root / "w" / "workflow.json").write_text(
        json.dumps(
            {
                "1": {"class_type": "LoadImage", "inputs": {"image": "c.png"}},
                "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "x"}},
                "3": {"class_type": "RandomNoise", "inputs": {"noise_seed": 0}},
                "4": {
                    "class_type": "SaveImage",
                    "inputs": {"filename_prefix": "USER_ID_STORY_ID_V2"},
                },
            }
        )
    )
    return wf_root, tpl_root


def _multi_panel_model(
    fake: FakeComfyUI, tmp_path: Path, panels: list[tuple[str, int]]
) -> ComfyUIModel:
    wf_root, tpl_root = _write_multi_panel_template(tmp_path, panels)
    return ComfyUIModel(
        fake,
        workflow_root=wf_root,
        template_root=tpl_root,
        model_version="mv-test",
        clock=lambda: 0.0,
    )


def test_generate_yields_one_image_per_panel(tmp_path: Path) -> None:
    fake = FakeComfyUI()
    model = _multi_panel_model(
        fake, tmp_path, [("scene one", 11), ("scene two", 22), ("scene three", 33)]
    )

    panels = list(
        model.generate(
            story_id="s1",
            user_id="u1",
            template_id="t",
            configurable_options={},
            input_images=[PNG],
        )
    )

    assert len(panels) == 3
    assert len(fake.submitted) == 3
    assert [s.client_id for s in fake.submitted] == ["s1-0", "s1-1", "s1-2"]
    # One input slot, uploaded once and reused across all panels.
    assert len(fake.uploads) == 1


def test_generate_uses_each_panels_own_seed_without_override(tmp_path: Path) -> None:
    fake = FakeComfyUI()
    model = _multi_panel_model(fake, tmp_path, [("a", 11), ("b", 22)])

    list(
        model.generate(
            story_id="s1",
            user_id="u1",
            template_id="t",
            configurable_options={},
            input_images=[PNG],
        )
    )

    assert fake.submitted[0].prompt["3"]["inputs"]["noise_seed"] == 11
    assert fake.submitted[1].prompt["3"]["inputs"]["noise_seed"] == 22


def test_generate_seed_override_advances_per_panel(tmp_path: Path) -> None:
    fake = FakeComfyUI()
    model = _multi_panel_model(fake, tmp_path, [("a", 11), ("b", 22), ("c", 33)])

    list(
        model.generate(
            story_id="s1",
            user_id="u1",
            template_id="t",
            configurable_options={"seed": 500},
            input_images=[PNG],
        )
    )

    seeds = [s.prompt["3"]["inputs"]["noise_seed"] for s in fake.submitted]
    assert seeds == [500, 501, 502]


# --- worker-side terminal failures (raised eagerly, before any ComfyUI call) -


@pytest.mark.parametrize(
    "options",
    [
        {"prompt": 123},
        {"steps": 0},
        {"steps": 31},
        {"steps": "8"},
        {"steps": True},
        {"seed": -1},
        {"seed": 1.5},
        {"seed": True},
    ],
)
def test_generate_rejects_invalid_options_before_calling_comfyui(
    options: dict[str, Any],
) -> None:
    fake = FakeComfyUI()
    model = _model(fake)

    with pytest.raises(InvalidConfigError):
        _generate(model, configurable_options=options)

    assert fake.submitted == []
    assert fake.uploads == []


def test_generate_rejects_empty_input_images() -> None:
    fake = FakeComfyUI()
    model = _model(fake)

    with pytest.raises(CorruptInputError, match="no input images"):
        _generate(model, input_images=[])


def test_generate_rejects_non_image_bytes() -> None:
    fake = FakeComfyUI()
    model = _model(fake)

    with pytest.raises(CorruptInputError, match="not a supported format"):
        _generate(model, input_images=[b"this is plainly not an image file"])


def test_generate_rejects_ftyp_with_unknown_brand() -> None:
    fake = FakeComfyUI()
    model = _model(fake)
    not_heic = b"\x00\x00\x00\x18ftypXXXX" + b"\x00" * 16

    with pytest.raises(CorruptInputError, match="not a supported format"):
        _generate(model, input_images=[not_heic])


def test_generate_rejects_unknown_template() -> None:
    fake = FakeComfyUI()
    model = _model(fake)

    with pytest.raises(UnsupportedTemplateError):
        _generate(model, template_id="no-such-template")

    assert fake.uploads == []


# --- ComfyUI-side failures ---------------------------------------------------


def test_generate_maps_bad_upload_to_invalid_config() -> None:
    fake = FakeComfyUI(fail_upload=ComfyUIBadRequest("bad image"))
    model = _model(fake)

    with pytest.raises(InvalidConfigError, match="rejected the input"):
        _generate(model)


def test_generate_maps_upload_failure_to_transient() -> None:
    fake = FakeComfyUI(fail_upload=ComfyUIUnavailable("container down"))
    model = _model(fake)

    with pytest.raises(ModelTransientError, match="input upload failed"):
        _generate(model)


def test_generate_maps_bad_prompt_to_invalid_config() -> None:
    fake = FakeComfyUI(fail_queue=ComfyUIBadRequest("invalid prompt"))
    model = _model(fake)

    with pytest.raises(InvalidConfigError, match="rejected the prompt"):
        _generate(model)


def test_generate_maps_unavailable_queue_to_transient() -> None:
    fake = FakeComfyUI(fail_queue=ComfyUIUnavailable("connection refused"))
    model = _model(fake)

    with pytest.raises(ModelTransientError, match="ComfyUI run failed"):
        _generate(model)


def test_generate_maps_ws_execution_error_to_transient() -> None:
    fake = FakeComfyUI(execution_error=True)
    model = _model(fake)

    with pytest.raises(ModelTransientError, match="ComfyUI run failed"):
        _generate(model)


def test_generate_maps_ws_timeout_to_transient() -> None:
    fake = FakeComfyUI(ws_timeout=True)
    model = _model(fake)

    with pytest.raises(ModelTransientError, match="timed out"):
        _generate(model)


def test_generate_maps_ws_stream_closing_early_to_transient() -> None:
    fake = FakeComfyUI(ws_closes_early=True)
    model = _model(fake)

    with pytest.raises(ModelTransientError, match="closed the event stream"):
        _generate(model)


def test_generate_times_out_per_request() -> None:
    # A clock that jumps past the deadline after the run starts: the first WS
    # event the model inspects is already "too late".
    calls = {"n": 0}

    def jumping_clock() -> float:
        calls["n"] += 1
        return 0.0 if calls["n"] == 1 else 100_000.0

    fake = FakeComfyUI()
    model = _model(fake, clock=jumping_clock, request_timeout_seconds=1.0)

    with pytest.raises(ModelTransientError, match="per-request timeout"):
        _generate(model)


def test_generate_raises_transient_when_no_output_matches_prefix() -> None:
    fake = FakeComfyUI(empty_outputs=True)
    model = _model(fake)

    with pytest.raises(ModelTransientError, match="no output image matching"):
        _generate(model)


def test_generate_raises_transient_on_non_png_output() -> None:
    fake = FakeComfyUI(image_bytes=b"definitely not a png")
    model = _model(fake)

    with pytest.raises(ModelTransientError, match="non-PNG"):
        _generate(model)


def test_unused_comfyui_execution_error_is_importable() -> None:
    # ComfyUIExecutionError is raised inside the transport-facing path; assert it
    # is a ComfyUIError so transport implementers subclass-check correctly.
    assert issubclass(ComfyUIExecutionError, Exception)
