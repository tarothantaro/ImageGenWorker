"""In-process mock of a ComfyUI container.

Implements :class:`imagegen.model.ComfyUITransport` so tests can drive the real
:class:`~imagegen.model.ComfyUIModel` end-to-end without a GPU or a running
container — and, crucially, **mocks the real-time workflow**: ``open_events``
replays the same WebSocket message sequence a real ComfyUI emits during a
generation (``status`` → ``execution_start`` → per-node ``executing`` +
``progress`` → terminal ``executing(node=None)`` / ``execution_success``). The
model consumes that live stream exactly as it would in production.

It also:

* records every uploaded image and submitted workflow, so tests can assert
  exactly what the model sent (the rendered ``workflow.json`` + node params);
* synthesizes ``/history`` outputs from the submitted workflow's SaveImage
  prefixes, so the model's V1/V2 filename filtering runs against realistic data;
* returns a real (minimal) PNG carrying the configured dimensions;
* can fail like a real container does — refuse upload/queue, 4xx a bad prompt,
  raise an execution error mid-stream, time out the socket, close the stream
  early, or emit no matching output — driving every error path.

For a *runnable* mock (HTTP + real WebSocket, Dockerized), see
``tests/mock_comfyui/`` and the integration test that exercises the real
transport against it.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from imagegen.model import ComfyUIError, ComfyUIUnavailable


def make_png(width: int, height: int) -> bytes:
    """Build a minimal but valid PNG with the given dimensions in its IHDR."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    idat = zlib.compress(b"\x00" * (width * 3 + 1))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


@dataclass
class SubmittedPrompt:
    prompt: dict[str, Any]
    client_id: str


class FakeComfyUI:
    """A configurable stand-in for the ComfyUI HTTP + WebSocket API."""

    def __init__(
        self,
        *,
        width: int = 1024,
        height: int = 736,
        image_bytes: bytes | None = None,
        fail_upload: ComfyUIError | None = None,
        fail_queue: ComfyUIError | None = None,
        execution_error: bool = False,
        ws_timeout: bool = False,
        ws_closes_early: bool = False,
        use_execution_success: bool = False,
        empty_outputs: bool = False,
    ) -> None:
        self.uploads: list[tuple[str, bytes]] = []
        self.submitted: list[SubmittedPrompt] = []
        self.event_client_ids: list[str] = []
        self._image_bytes = (
            image_bytes if image_bytes is not None else make_png(width, height)
        )
        self._fail_upload = fail_upload
        self._fail_queue = fail_queue
        self._execution_error = execution_error
        self._ws_timeout = ws_timeout
        self._ws_closes_early = ws_closes_early
        self._use_execution_success = use_execution_success
        self._empty_outputs = empty_outputs
        self._last_prompt_id: str | None = None

    # --- ComfyUITransport ------------------------------------------------

    def upload_image(self, *, filename: str, data: bytes) -> str:
        if self._fail_upload is not None:
            raise self._fail_upload
        self.uploads.append((filename, data))
        return filename  # real ComfyUI echoes the stored name

    @contextmanager
    def open_events(self, *, client_id: str) -> Iterator[Iterator[dict[str, Any]]]:
        self.event_client_ids.append(client_id)
        yield self._iter_events()

    def queue_prompt(self, *, prompt: dict[str, Any], client_id: str) -> str:
        if self._fail_queue is not None:
            raise self._fail_queue
        self.submitted.append(SubmittedPrompt(prompt=prompt, client_id=client_id))
        self._last_prompt_id = f"prompt-{len(self.submitted)}"
        return self._last_prompt_id

    def get_history(self, prompt_id: str) -> dict[str, Any]:
        return {
            prompt_id: {
                "status": {"completed": True, "status_str": "success"},
                "outputs": self._outputs_for(prompt_id),
            }
        }

    def fetch_image(self, *, filename: str, subfolder: str, image_type: str) -> bytes:
        return self._image_bytes

    # --- real-time event replay ------------------------------------------

    def _iter_events(self) -> Iterator[dict[str, Any]]:
        """Replay a real-ish ComfyUI WebSocket stream for the latest prompt.

        Runs lazily (first ``next()`` happens after ``queue_prompt`` set the
        prompt id), mirroring how the model opens the socket before queuing.
        """
        pid = self._last_prompt_id
        submitted = self.submitted[-1].prompt

        yield {
            "type": "status",
            "data": {"status": {"exec_info": {"queue_remaining": 1}}},
        }
        yield {"type": "execution_start", "data": {"prompt_id": pid}}
        for node_id in submitted:
            yield {"type": "executing", "data": {"node": node_id, "prompt_id": pid}}
            yield {
                "type": "progress",
                "data": {"value": 1, "max": 1, "node": node_id, "prompt_id": pid},
            }
            if self._ws_timeout:
                raise ComfyUIUnavailable("timed out waiting for ComfyUI events")

        if self._execution_error:
            yield {
                "type": "execution_error",
                "data": {"prompt_id": pid, "exception_message": "node blew up"},
            }
            return
        if self._ws_closes_early:
            return  # socket closes before a completion event arrives
        if self._use_execution_success:
            yield {"type": "execution_success", "data": {"prompt_id": pid}}
        else:
            yield {"type": "executing", "data": {"node": None, "prompt_id": pid}}

    def _outputs_for(self, prompt_id: str) -> dict[str, Any]:
        if self._empty_outputs:
            return {}
        submitted = self.submitted[int(prompt_id.split("-")[1]) - 1]
        outputs: dict[str, Any] = {}
        for node_id, node in submitted.prompt.items():
            if node.get("class_type") == "SaveImage":
                prefix = node["inputs"]["filename_prefix"]
                outputs[node_id] = {
                    "images": [
                        {
                            "filename": f"{prefix}_00001_.png",
                            "subfolder": "",
                            "type": "output",
                        }
                    ]
                }
        return outputs
