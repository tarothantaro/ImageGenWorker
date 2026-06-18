"""Integration: the real HTTP+WebSocket transport against the mock ComfyUI.

This is the only place ``imagegen/comfyui_client.py`` (excluded from unit
coverage) runs for real. The mock ComfyUI from ``tests/mock_comfyui/server.py``
is started in-process on a random port; a real ``HttpComfyUIClient`` +
``ComfyUIModel`` drives a full job through it, riding the live WebSocket stream
to completion — proving the streaming/blocking path the worker relies on.

Skipped automatically if aiohttp / websocket-client aren't installed (they are
optional, container-side deps). Not part of the unit coverage gate.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from collections.abc import Iterator
from typing import Any

import pytest

pytest.importorskip("aiohttp")
pytest.importorskip("websocket")

from aiohttp import web  # noqa: E402

from imagegen.comfyui_client import HttpComfyUIClient  # noqa: E402
from imagegen.failure_classification import (  # noqa: E402
    InvalidConfigError,
    ModelTransientError,
)
from imagegen.model import ComfyUIModel  # noqa: E402
from tests.fakes.comfyui import make_png  # noqa: E402
from tests.mock_comfyui.server import build_app  # noqa: E402

INPUT_PNG = make_png(64, 64)


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _serve(fail_mode: str = "none") -> Iterator[str]:
    """Run the mock ComfyUI in a background event loop; yield its base URL."""
    port = _free_port()
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    state: dict[str, object] = {}

    def run() -> None:
        asyncio.set_event_loop(loop)
        runner = web.AppRunner(build_app(step_delay=0.0, fail_mode=fail_mode))
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "127.0.0.1", port)
        loop.run_until_complete(site.start())
        state["runner"] = runner
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(timeout=10), "mock ComfyUI did not start"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        # Cleanly shut the server (closes lingering WebSocket handlers) before
        # stopping the loop, so no asyncio task is left pending.
        runner = state["runner"]
        asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result(timeout=10)  # type: ignore[attr-defined]
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10)
        loop.close()


@pytest.fixture
def mock_comfyui() -> Iterator[str]:
    yield from _serve()


@pytest.fixture
def mock_comfyui_bad_prompt() -> Iterator[str]:
    yield from _serve(fail_mode="bad_prompt")


@pytest.fixture
def mock_comfyui_execution_error() -> Iterator[str]:
    yield from _serve(fail_mode="execution_error")


def _generate(base_url: str, **overrides: object) -> list[Any]:
    transport = HttpComfyUIClient(base_url=base_url, timeout=15.0)
    try:
        model = ComfyUIModel(transport, model_version="it-test")
        kwargs = {
            "story_id": "s1",
            "user_id": "u1",
            "prompt_type": 1,
            "prompt_id": 1,
            "input_images": [INPUT_PNG],
        }
        kwargs.update(overrides)
        # Consume the panel iterator here, while the transport is still open.
        return list(model.generate(**kwargs))  # type: ignore[arg-type]
    finally:
        transport.close()


def test_real_transport_streams_to_completion(mock_comfyui: str) -> None:
    panels = _generate(mock_comfyui)

    # templates/2: 6 panels × 2 saved variants (V1, V2) → 12 outputs.
    assert len(panels) == 12
    assert all(p.width == 1024 and p.height == 736 for p in panels)
    assert all(p.image[:8] == b"\x89PNG\r\n\x1a\n" for p in panels)


def test_real_transport_maps_bad_prompt(mock_comfyui_bad_prompt: str) -> None:
    with pytest.raises(InvalidConfigError):
        _generate(mock_comfyui_bad_prompt)


def test_real_transport_maps_execution_error(
    mock_comfyui_execution_error: str,
) -> None:
    with pytest.raises(ModelTransientError):
        _generate(mock_comfyui_execution_error)
