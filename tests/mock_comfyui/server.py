"""A runnable mock ComfyUI container.

Speaks enough of the real ComfyUI HTTP + WebSocket API for the worker's
``HttpComfyUIClient`` to drive it exactly as it would a real ComfyUI — but
without a GPU, models, or minutes of compute. It **mocks the real-time
workflow**: when a prompt is queued it streams the same WebSocket event
sequence ComfyUI emits during a generation (``execution_start`` → per-node
``executing`` + ``progress`` → terminal ``executing(node=None)`` /
``execution_success``), pacing them with ``MOCK_STEP_DELAY`` so callers
exercise the real streaming/blocking path.

It produces a real PNG for each SaveImage node (dimensions from
``MOCK_WIDTH``/``MOCK_HEIGHT``) and serves it from ``/view``.

Endpoints (subset of the real ComfyUI API):
  GET  /system_stats         healthcheck
  POST /upload/image         multipart upload (echoes the stored name)
  GET  /ws?clientId=<id>     execution event stream
  POST /prompt               {"prompt": <workflow>, "client_id": <id>}
  GET  /history/{prompt_id}  execution status + outputs
  GET  /view?filename=...    produced image bytes

Env knobs (all optional):
  MOCK_PORT        (default 8188)
  MOCK_STEP_DELAY  seconds between per-node events (default 2.0 — set ~0 in tests)
  MOCK_FAIL_MODE   none | bad_prompt | execution_error  (default none)
  MOCK_WIDTH       output PNG width  (default 1024)
  MOCK_HEIGHT      output PNG height (default 736)
  MOCK_MAX_UPLOAD_MB  /upload/image body cap (default 64; real ComfyUI takes
                      large photos, so mirror that — aiohttp's own default is
                      only 1 MB, which 413s a normal re-encoded photo)
"""

from __future__ import annotations

import asyncio
import os
import struct
import uuid
import zlib
from typing import Any

from aiohttp import web

# Typed application keys (aiohttp's blessed way to stash per-app state).
IMAGES: web.AppKey[dict[str, bytes]] = web.AppKey("images")
HISTORY: web.AppKey[dict[str, Any]] = web.AppKey("history")
CLIENTS: web.AppKey[dict[str, web.WebSocketResponse]] = web.AppKey("clients")
TASKS: web.AppKey[set[asyncio.Task[None]]] = web.AppKey("tasks")
STEP_DELAY: web.AppKey[float] = web.AppKey("step_delay")
FAIL_MODE: web.AppKey[str] = web.AppKey("fail_mode")
WIDTH: web.AppKey[int] = web.AppKey("width")
HEIGHT: web.AppKey[int] = web.AppKey("height")


def make_png(width: int, height: int, seed: int = 0) -> bytes:
    """A valid solid-colour PNG. The colour is derived from ``seed`` so distinct
    panels/variants render as visibly different images (the A/B demo needs the
    two variants of a panel to actually differ)."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    r, g, b = (seed * 73 + 41) % 256, (seed * 151 + 17) % 256, (seed * 199 + 89) % 256
    # Each row: a filter byte (0 = none) then `width` RGB pixels of the colour.
    row = b"\x00" + bytes((r, g, b)) * width
    idat = zlib.compress(row * height)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


async def system_stats(request: web.Request) -> web.Response:
    return web.json_response({"system": {"comfyui_version": "mock"}, "devices": []})


async def upload_image(request: web.Request) -> web.Response:
    reader = await request.multipart()
    filename = "upload.png"
    async for part in reader:
        if part.name == "image":
            filename = part.filename or filename
            request.app[IMAGES][filename] = await part.read(decode=False)
        else:
            await part.read()  # drain the 'overwrite' field
    return web.json_response({"name": filename, "subfolder": "", "type": "input"})


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    client_id = request.query.get("clientId", "")
    request.app[CLIENTS][client_id] = ws
    await ws.send_json(
        {"type": "status", "data": {"status": {"exec_info": {"queue_remaining": 0}}}}
    )
    try:
        async for _msg in ws:  # keep the socket open; we don't read client input
            pass
    finally:
        request.app[CLIENTS].pop(client_id, None)
    return ws


async def prompt_handler(request: web.Request) -> web.Response:
    body = await request.json()
    if request.app[FAIL_MODE] == "bad_prompt":
        return web.json_response(
            {
                "error": {"type": "invalid_prompt", "message": "mock: bad prompt"},
                "node_errors": {"0": "mock node error"},
            },
            status=400,
        )
    prompt_id = uuid.uuid4().hex
    task = asyncio.create_task(
        _execute(
            request.app,
            prompt_id=prompt_id,
            prompt=body.get("prompt", {}),
            client_id=body.get("client_id", ""),
        )
    )
    request.app[TASKS].add(task)
    task.add_done_callback(request.app[TASKS].discard)
    return web.json_response({"prompt_id": prompt_id, "number": 1, "node_errors": {}})


async def history_handler(request: web.Request) -> web.Response:
    prompt_id = request.match_info["prompt_id"]
    entry = request.app[HISTORY].get(prompt_id)
    return web.json_response({prompt_id: entry} if entry is not None else {})


async def view_handler(request: web.Request) -> web.Response:
    data = request.app[IMAGES].get(request.query.get("filename", ""))
    if data is None:
        return web.Response(status=404)
    return web.Response(body=data, content_type="image/png")


async def _execute(
    app: web.Application, *, prompt_id: str, prompt: dict[str, Any], client_id: str
) -> None:
    """Stream a realistic event sequence, then publish history + outputs."""
    ws = app[CLIENTS].get(client_id)

    async def send(payload: dict[str, Any]) -> None:
        if ws is not None and not ws.closed:
            await ws.send_json(payload)

    await send({"type": "execution_start", "data": {"prompt_id": prompt_id}})
    for node_id in prompt:
        await send(
            {"type": "executing", "data": {"node": node_id, "prompt_id": prompt_id}}
        )
        await send(
            {
                "type": "progress",
                "data": {"value": 1, "max": 1, "node": node_id, "prompt_id": prompt_id},
            }
        )
        await asyncio.sleep(app[STEP_DELAY])

    if app[FAIL_MODE] == "execution_error":
        await send(
            {
                "type": "execution_error",
                "data": {
                    "prompt_id": prompt_id,
                    "node_id": next(iter(prompt), "?"),
                    "exception_message": "mock: execution error",
                },
            }
        )
        return

    outputs: dict[str, Any] = {}
    for node_id, node in prompt.items():
        if node.get("class_type") == "SaveImage":
            prefix = node.get("inputs", {}).get("filename_prefix", "output")
            filename = f"{prefix}_00001_.png"
            # Distinct colour per filename → each panel/variant (unique prefix)
            # renders as a different image.
            seed = zlib.crc32(filename.encode()) & 0xFFFFFFFF
            app[IMAGES][filename] = make_png(app[WIDTH], app[HEIGHT], seed=seed)
            outputs[node_id] = {
                "images": [{"filename": filename, "subfolder": "", "type": "output"}]
            }

    # Publish history *before* the terminal event so a client that fetches
    # history the instant it sees "done" always finds the outputs.
    app[HISTORY][prompt_id] = {
        "prompt": prompt,
        "outputs": outputs,
        "status": {"completed": True, "status_str": "success", "messages": []},
    }
    await send({"type": "executing", "data": {"node": None, "prompt_id": prompt_id}})
    await send({"type": "execution_success", "data": {"prompt_id": prompt_id}})


def build_app(
    *,
    step_delay: float = 0.05,
    fail_mode: str = "none",
    width: int = 1024,
    height: int = 736,
    max_upload_bytes: int = 64 * 1024 * 1024,
) -> web.Application:
    # Real ComfyUI accepts full-size photos; aiohttp's default client_max_size is
    # only 1 MB, which 413s a normal re-encoded upload. Lift it so the mock
    # mirrors real ComfyUI instead of failing the worker's /upload/image.
    app = web.Application(client_max_size=max_upload_bytes)
    app[IMAGES] = {}
    app[HISTORY] = {}
    app[CLIENTS] = {}
    app[TASKS] = set()
    app[STEP_DELAY] = step_delay
    app[FAIL_MODE] = fail_mode
    app[WIDTH] = width
    app[HEIGHT] = height
    app.add_routes(
        [
            web.get("/system_stats", system_stats),
            web.post("/upload/image", upload_image),
            web.get("/ws", ws_handler),
            web.post("/prompt", prompt_handler),
            web.get("/history/{prompt_id}", history_handler),
            web.get("/view", view_handler),
        ]
    )
    return app


def main() -> None:  # pragma: no cover - container entrypoint
    app = build_app(
        step_delay=float(os.environ.get("MOCK_STEP_DELAY", "2.0")),
        fail_mode=os.environ.get("MOCK_FAIL_MODE", "none"),
        width=int(os.environ.get("MOCK_WIDTH", "1024")),
        height=int(os.environ.get("MOCK_HEIGHT", "736")),
        max_upload_bytes=int(os.environ.get("MOCK_MAX_UPLOAD_MB", "64")) * 1024 * 1024,
    )
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("MOCK_PORT", "8188")))


if __name__ == "__main__":  # pragma: no cover
    main()
