"""ComfyUI-backed implementation of the worker's image-generation model.

``main.py`` imports ``load_model`` from here; ``job_handler.JobHandler`` drives
the returned object through the ``ImageGenModel`` protocol. The model:

1. validates inputs + ``configurable_options`` (worker-side terminal failures),
2. loads ``workflows/2`` + ``templates/3`` (see :mod:`imagegen.workflow`),
3. uploads the input photo(s) to ComfyUI under their per-story filenames,
4. **yields one image per template panel** (DESIGN.md §7.2: *one panel == one
   ComfyUI run == one output image*). For each panel it renders an API-format
   prompt, submits it, **streams ComfyUI's WebSocket events in real time** until
   that run reports completion (a generation takes minutes — we cannot poll on a
   fixed interval; we block on the event stream and return the instant ComfyUI
   signals done, subject to a configurable **per-request timeout**), then fetches
   that run's final (``_V2``, face-restored) image from history and yields it.

``generate`` returns an *iterator* of :class:`PanelResult`. The handler consumes
it lazily so it can upload + publish each panel as it lands, then send the next
panel's request — the worker never fires all panels at once.

The ComfyUI conversation sits behind the :class:`ComfyUITransport` Protocol,
mirroring how the rest of the worker injects ``google-cloud-*`` clients. The
transport exposes the real ComfyUI surface: ``upload_image`` / ``queue_prompt``
/ ``get_history`` / ``fetch_image`` over HTTP, and ``open_events`` — a context
manager yielding the live WebSocket message stream. Unit tests pass a fake
transport that replays a realistic event sequence (the "mock ComfyUI"); the
real transport lives in :mod:`imagegen.comfyui_client` and is wired by
:func:`load_model`.

Transport errors map onto the worker's failure taxonomy so
:mod:`imagegen.failure_classification` can route them:

* bad request (4xx / invalid prompt) → :class:`InvalidConfigError` (report failed)
* unavailable / WS timeout / 5xx     → :class:`ModelTransientError` (nack, retry)
* execution error on the WS stream    → :class:`ModelTransientError` (nack, retry)
* per-request timeout exceeded        → :class:`ModelTransientError` (nack, retry)
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from .failure_classification import (
    CorruptInputError,
    InvalidConfigError,
    ModelTransientError,
)
from .workflow import PreparedTemplate, WorkflowBuilder, _substitute

logger = logging.getLogger(__name__)

_PACKAGE_ROOT = Path(__file__).resolve().parent
_DEFAULT_WORKFLOW_ROOT = _PACKAGE_ROOT / "workflows"
_DEFAULT_TEMPLATE_ROOT = _PACKAGE_ROOT / "templates"

_DEFAULT_MODEL_VERSION = "comfyui-flux2"
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 180.0
_MIN_STEPS = 1
_MAX_STEPS = 30
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


# --- transport seam ----------------------------------------------------------


class ComfyUIError(Exception):
    """Base for errors surfaced by a :class:`ComfyUITransport`."""


class ComfyUIUnavailable(ComfyUIError):
    """Connection refused / WS timeout / 5xx — worth retrying (→ transient)."""


class ComfyUIBadRequest(ComfyUIError):
    """4xx, e.g. ComfyUI rejected the prompt as invalid — terminal (→ config)."""


class ComfyUIExecutionError(ComfyUIError):
    """Workflow reported an execution error (bad node, OOM) — retry (→ transient)."""


@runtime_checkable
class ComfyUITransport(Protocol):
    """The subset of the ComfyUI API the model depends on."""

    def upload_image(self, *, filename: str, data: bytes) -> str:
        """Upload an image; return the filename ComfyUI stored it under."""
        ...

    def open_events(
        self, *, client_id: str
    ) -> AbstractContextManager[Iterator[dict[str, Any]]]:
        """Open the ComfyUI WebSocket and yield decoded messages in real time.

        Used as a context manager so the socket is always closed. The yielded
        iterator blocks on each ``recv``; it raises :class:`ComfyUIUnavailable`
        on timeout and ends (``StopIteration``) when the socket closes.
        """
        ...

    def queue_prompt(self, *, prompt: dict[str, Any], client_id: str) -> str:
        """Submit an API-format workflow; return its ``prompt_id``."""
        ...

    def get_history(self, prompt_id: str) -> dict[str, Any]:
        """Return ComfyUI's ``/history/{prompt_id}`` payload."""
        ...

    def fetch_image(self, *, filename: str, subfolder: str, image_type: str) -> bytes:
        """Download a produced image's bytes via ``/view``."""
        ...


# --- result ------------------------------------------------------------------


@dataclass(frozen=True)
class PanelResult:
    """One generated panel. Satisfies ``job_handler.PanelResult`` structurally."""

    image: bytes
    width: int
    height: int
    model_version: str
    processing_seconds: float


# --- helpers -----------------------------------------------------------------


def _is_heic_or_avif(data: bytes) -> bool:
    if len(data) < 12 or data[4:8] != b"ftyp":
        return False
    return data[8:12] in (
        b"heic",
        b"heix",
        b"hevc",
        b"hevx",
        b"mif1",
        b"msf1",
        b"avif",
    )


def _looks_like_image(data: bytes) -> bool:
    """Magic-byte sniff for PNG / JPEG / WebP / HEIC / AVIF (legacy parity)."""
    return (
        data[:8] == _PNG_SIGNATURE
        or data[:2] == b"\xff\xd8"
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
        or _is_heic_or_avif(data)
    )


def _validate_input_images(images: list[bytes]) -> None:
    if not images:
        raise CorruptInputError("no input images provided")
    for index, data in enumerate(images):
        if not _looks_like_image(data):
            raise CorruptInputError(
                f"input image {index} is not a supported format "
                "(png/jpeg/webp/heic/avif)"
            )


def _png_dimensions(data: bytes) -> tuple[int, int]:
    """Read width/height from a PNG's IHDR. ComfyUI always returns PNGs."""
    if len(data) < 24 or data[:8] != _PNG_SIGNATURE:
        raise ModelTransientError("ComfyUI returned a non-PNG output image")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return width, height


def _extract_options(
    options: dict[str, object],
) -> tuple[str | None, int | None, int | None]:
    """Pull the three supported overrides out of ``configurable_options``.

    Unknown keys are ignored (forward-compat). Present-but-wrong-typed values
    are terminal :class:`InvalidConfigError`s. The overrides apply to *every*
    panel: ``prompt``/``steps`` replace that field in each panel; ``seed`` is a
    base — panel *i* uses ``seed + i`` so per-panel images still vary — while an
    absent ``seed`` lets each panel keep its own template ``noise_seed``.
    """
    prompt = options.get("prompt")
    if prompt is not None and not isinstance(prompt, str):
        raise InvalidConfigError("configurable_options.prompt must be a string")

    steps = options.get("steps")
    if steps is not None and (
        isinstance(steps, bool)
        or not isinstance(steps, int)
        or not _MIN_STEPS <= steps <= _MAX_STEPS
    ):
        raise InvalidConfigError(
            f"configurable_options.steps must be an int in "
            f"[{_MIN_STEPS}, {_MAX_STEPS}]"
        )

    seed = options.get("seed")
    if seed is not None and (
        isinstance(seed, bool) or not isinstance(seed, int) or seed < 0
    ):
        raise InvalidConfigError("configurable_options.seed must be a non-negative int")

    return prompt, steps, seed


# --- the model ---------------------------------------------------------------


class ComfyUIModel:
    """Drives a ComfyUI container to produce one image per template panel."""

    def __init__(
        self,
        transport: ComfyUITransport,
        *,
        workflow_root: Path = _DEFAULT_WORKFLOW_ROOT,
        template_root: Path = _DEFAULT_TEMPLATE_ROOT,
        model_version: str = _DEFAULT_MODEL_VERSION,
        request_timeout_seconds: float = _DEFAULT_REQUEST_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transport = transport
        self._builder = WorkflowBuilder(workflow_root, template_root)
        self._model_version = model_version
        self._request_timeout = request_timeout_seconds
        self._clock = clock

    def generate(
        self,
        *,
        story_id: str,
        user_id: str,
        template_id: str,
        configurable_options: dict[str, object],
        input_images: list[bytes],
    ) -> Iterator[PanelResult]:
        """Validate + upload eagerly, then return a per-panel result iterator.

        Worker-side terminal failures (bad input, bad options, unknown template)
        and a failed input upload are raised here, before any panel runs — so
        the caller sees them immediately. Per-panel ComfyUI failures surface as
        the iterator is consumed.
        """
        _validate_input_images(input_images)
        prompt, steps, seed_option = _extract_options(configurable_options)
        prepared = self._builder.prepare(template_id)  # UnsupportedTemplateError

        placeholders = {"USER_ID": user_id, "STORY_ID": story_id}
        try:
            self._upload_inputs(prepared, input_images, placeholders)
        except ComfyUIBadRequest as exc:
            raise InvalidConfigError(f"ComfyUI rejected the input: {exc}") from exc
        except (ComfyUIUnavailable, ComfyUIExecutionError) as exc:
            raise ModelTransientError(f"ComfyUI input upload failed: {exc}") from exc

        return self._iter_panels(
            prepared,
            story_id=story_id,
            placeholders=placeholders,
            prompt=prompt,
            steps=steps,
            seed_option=seed_option,
        )

    # -- internals --------------------------------------------------------

    def _iter_panels(
        self,
        prepared: PreparedTemplate,
        *,
        story_id: str,
        placeholders: dict[str, str],
        prompt: str | None,
        steps: int | None,
        seed_option: int | None,
    ) -> Iterator[PanelResult]:
        for index, panel in enumerate(prepared.panels):
            seed = None if seed_option is None else seed_option + index
            try:
                images, processing_seconds = self._run_panel(
                    prepared,
                    panel,
                    placeholders=placeholders,
                    prompt=prompt,
                    steps=steps,
                    seed=seed,
                    client_id=f"{story_id}-{index}",
                )
            except ComfyUIBadRequest as exc:
                raise InvalidConfigError(f"ComfyUI rejected the prompt: {exc}") from exc
            except (ComfyUIUnavailable, ComfyUIExecutionError) as exc:
                raise ModelTransientError(f"ComfyUI run failed: {exc}") from exc

            # One ComfyUI run saves every variant the workflow declares (V1, V2 …);
            # each becomes its own output — the user's A/B choices for this panel.
            per_image_seconds = processing_seconds / max(1, len(images))
            for image in images:
                width, height = _png_dimensions(image)
                yield PanelResult(
                    image=image,
                    width=width,
                    height=height,
                    model_version=self._model_version,
                    processing_seconds=per_image_seconds,
                )

    def _upload_inputs(
        self,
        prepared: PreparedTemplate,
        input_images: list[bytes],
        placeholders: dict[str, str],
    ) -> None:
        """Upload one input photo per image slot, under its per-story filename.

        Slots are filled positionally; when the job supplies fewer photos than
        the template has slots, the last photo is reused. The upload filename is
        the substituted slot name (e.g. ``u1_s1_INPUT_1.png``), so the rendered
        ``LoadImage`` nodes reference exactly what ComfyUI stored — no remap.
        """
        for slot_index, raw_slot in enumerate(prepared.image_slots):
            filename = _substitute(raw_slot, placeholders)
            data = input_images[min(slot_index, len(input_images) - 1)]
            self._transport.upload_image(filename=filename, data=data)

    def _run_panel(
        self,
        prepared: PreparedTemplate,
        panel: list[dict[str, Any]],
        *,
        placeholders: dict[str, str],
        prompt: str | None,
        steps: int | None,
        seed: int | None,
        client_id: str,
    ) -> tuple[list[bytes], float]:
        start = self._clock()
        workflow = self._builder.render(
            prepared,
            panel,
            placeholders=placeholders,
            prompt=prompt,
            steps=steps,
            seed=seed,
        )
        prefixes = self._builder.output_prefixes(workflow)

        # Open the WebSocket *before* queuing, so we don't miss early events,
        # then block on the live stream until ComfyUI signals completion.
        with self._transport.open_events(client_id=client_id) as events:
            prompt_id = self._transport.queue_prompt(
                prompt=workflow, client_id=client_id
            )
            self._await_execution(
                events, prompt_id, deadline=start + self._request_timeout
            )

        history = self._transport.get_history(prompt_id)
        outputs = history.get(prompt_id, {}).get("outputs", {})
        # Every variant the workflow saved, in variant order (V1, V2 …).
        images = [self._fetch_variant(outputs, prefix) for prefix in prefixes]
        return images, self._clock() - start

    def _await_execution(
        self, events: Iterator[dict[str, Any]], prompt_id: str, *, deadline: float
    ) -> None:
        """Consume the live WebSocket stream until this prompt completes.

        Returns when ComfyUI reports the prompt finished (``executing`` with a
        null node, or ``execution_success``). Raises on an execution error, on a
        closed stream with no completion, and when the per-request ``deadline``
        is exceeded — all transient (nack → redeliver).
        """
        for message in events:
            if self._clock() > deadline:
                raise ComfyUIUnavailable(
                    f"ComfyUI did not complete prompt {prompt_id} within the "
                    "per-request timeout"
                )
            data = message.get("data") or {}
            if data.get("prompt_id") != prompt_id:
                continue  # status pings / other prompts — not ours
            event = message.get("type")
            logger.debug(
                "comfyui_event", extra={"event": event, "prompt_id": prompt_id}
            )
            if event == "execution_error":
                raise ComfyUIExecutionError(
                    str(data.get("exception_message") or "workflow execution error")
                )
            if event == "execution_success" or (
                event == "executing" and data.get("node") is None
            ):
                return
        raise ComfyUIUnavailable(
            f"ComfyUI closed the event stream before prompt {prompt_id} completed"
        )

    def _fetch_variant(self, outputs: dict[str, Any], prefix: str) -> bytes:
        """Fetch the produced image whose filename matches ``prefix``."""
        ref = self._image_for_prefix(outputs, prefix)
        return self._transport.fetch_image(
            filename=ref["filename"],
            subfolder=ref.get("subfolder", ""),
            image_type=ref.get("type", "output"),
        )

    @staticmethod
    def _image_for_prefix(outputs: dict[str, Any], prefix: str) -> dict[str, Any]:
        """Find the produced image whose filename matches ``prefix``."""
        for node_output in outputs.values():
            for image in node_output.get("images", []):
                filename = image.get("filename", "")
                if filename.startswith(prefix):
                    return image
        raise ModelTransientError(
            f"ComfyUI produced no output image matching {prefix!r}"
        )


def load_model(cfg: Any) -> ComfyUIModel:  # pragma: no cover - wires real transport
    """Build a production :class:`ComfyUIModel` from worker config."""
    from .comfyui_client import HttpComfyUIClient

    timeout = float(cfg.comfyui_request_timeout_seconds)
    transport = HttpComfyUIClient(base_url=cfg.comfyui_url, timeout=timeout)
    return ComfyUIModel(
        transport,
        model_version=cfg.model_version,
        request_timeout_seconds=timeout,
    )
