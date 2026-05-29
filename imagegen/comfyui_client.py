"""Real HTTP + WebSocket transport to a ComfyUI container (production seam).

Implements :class:`imagegen.model.ComfyUITransport` against ComfyUI's actual
API: HTTP for upload/prompt/history/view via a synchronous ``httpx.Client``,
and the ``/ws`` WebSocket via ``websocket-client`` for **real-time** execution
events. A generation takes minutes, so the model blocks on the WebSocket
stream (``open_events``) rather than polling — exactly how the ComfyUI web UI
and the legacy ``ImageGenCp`` client tracked progress.

This is thin I/O glue: it maps transport conditions onto the ``ComfyUI*``
exception hierarchy and decodes frames; it has no business logic. It is
excluded from unit-test coverage (``[tool.coverage.run] omit`` in
``pyproject.toml``) like ``main.py``, and is instead pinned by the integration
test that runs it against the mock ComfyUI container
(``tests/integration/test_comfyui_client.py``).

Endpoint surface (from the live ComfyUI API + ``../ImageGenCp`` client):

* ``POST /upload/image``     — multipart upload, ``overwrite=true``
* WS   ``/ws?clientId=<id>`` — execution event stream
* ``POST /prompt``           — ``{"prompt": <workflow>, "client_id": <id>}``
* ``GET  /history/{id}``     — execution status + outputs
* ``GET  /view``             — fetch a produced image's bytes
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import websocket  # websocket-client (synchronous)

from .model import ComfyUIBadRequest, ComfyUIUnavailable


class HttpComfyUIClient:
    """Blocking ComfyUI client. One instance is shared across job threads."""

    def __init__(self, *, base_url: str, timeout: float) -> None:
        base = base_url.rstrip("/")
        self._timeout = timeout
        self._client = httpx.Client(base_url=base, timeout=httpx.Timeout(timeout))
        self._ws_base = base.replace("https://", "wss://").replace("http://", "ws://")

    def upload_image(self, *, filename: str, data: bytes) -> str:
        try:
            response = self._client.post(
                "/upload/image",
                files={"image": (filename, data, "image/png")},
                data={"overwrite": "true"},
            )
        except httpx.RequestError as exc:
            raise ComfyUIUnavailable(f"upload {filename!r} failed: {exc}") from exc
        self._raise_for_status(response, context=f"upload {filename!r}")
        return response.json().get("name", filename)

    @contextmanager
    def open_events(self, *, client_id: str) -> Iterator[Iterator[dict[str, Any]]]:
        url = f"{self._ws_base}/ws?clientId={client_id}"
        try:
            conn = websocket.create_connection(url, timeout=self._timeout)
        except (OSError, websocket.WebSocketException) as exc:
            raise ComfyUIUnavailable(f"WebSocket connect failed: {exc}") from exc
        try:
            yield self._iter_events(conn)
        finally:
            conn.close()

    @staticmethod
    def _iter_events(conn: websocket.WebSocket) -> Iterator[dict[str, Any]]:
        while True:
            try:
                raw = conn.recv()
            except websocket.WebSocketTimeoutException as exc:
                raise ComfyUIUnavailable(
                    "timed out waiting for ComfyUI events"
                ) from exc
            except websocket.WebSocketConnectionClosedException:
                return
            if isinstance(raw, bytes):
                continue  # binary preview frames carry no execution status
            if not raw:
                continue
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                continue

    def queue_prompt(self, *, prompt: dict[str, Any], client_id: str) -> str:
        try:
            response = self._client.post(
                "/prompt",
                json={"prompt": prompt, "client_id": client_id},
            )
        except httpx.RequestError as exc:
            raise ComfyUIUnavailable(f"queue prompt failed: {exc}") from exc
        self._raise_for_status(response, context="queue prompt")
        prompt_id = response.json().get("prompt_id")
        if not prompt_id:
            raise ComfyUIUnavailable("ComfyUI returned no prompt_id")
        return str(prompt_id)

    def get_history(self, prompt_id: str) -> dict[str, Any]:
        try:
            response = self._client.get(f"/history/{prompt_id}")
        except httpx.RequestError as exc:
            raise ComfyUIUnavailable(f"history fetch failed: {exc}") from exc
        self._raise_for_status(response, context="history")
        return response.json()

    def fetch_image(self, *, filename: str, subfolder: str, image_type: str) -> bytes:
        params = {"filename": filename, "type": image_type}
        if subfolder:
            params["subfolder"] = subfolder
        try:
            response = self._client.get("/view", params=params)
        except httpx.RequestError as exc:
            raise ComfyUIUnavailable(f"view {filename!r} failed: {exc}") from exc
        self._raise_for_status(response, context=f"view {filename!r}")
        return response.content

    def close(self) -> None:
        self._client.close()

    @staticmethod
    def _raise_for_status(response: httpx.Response, *, context: str) -> None:
        if response.status_code == 200:
            return
        detail = f"{context}: HTTP {response.status_code} {response.text}"
        if 400 <= response.status_code < 500:
            raise ComfyUIBadRequest(detail)
        raise ComfyUIUnavailable(detail)
