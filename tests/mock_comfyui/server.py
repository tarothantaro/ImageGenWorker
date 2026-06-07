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
  MOCK_VARIANTS_PER_PANEL  A/B layout for the panel/variant label drawn on each
                      image (default 1). Set to the story's template value
                      (the demo's template 4 = 2) so labels read correctly.
  MOCK_MAX_UPLOAD_MB  /upload/image body cap (default 64; real ComfyUI takes
                      large photos, so mirror that — aiohttp's own default is
                      only 1 MB, which 413s a normal re-encoded photo)
"""

from __future__ import annotations

import asyncio
import os
import re
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
VARIANTS: web.AppKey[int] = web.AppKey("variants")


# A curated, high-contrast palette stepped through by output index so the N
# outputs of a story form a clear, *ordered* sequence (output 0 = teal, 1 =
# orange, 2 = purple…). Index past the end wraps — fine for a mock.
_PALETTE: list[tuple[int, int, int]] = [
    (38, 166, 154),  # teal
    (239, 108, 0),  # orange
    (126, 87, 194),  # purple
    (67, 160, 71),  # green
    (236, 64, 122),  # pink
    (30, 136, 229),  # blue
    (251, 192, 45),  # amber
    (141, 110, 99),  # brown
]

# 5×7 bitmap glyphs — just the characters the labels need: the digits, the
# variant letters (A–D), 'P', a separator dot, and space.
_FONT: dict[str, tuple[str, ...]] = {
    "0": (".###.", "#...#", "#..##", "#.#.#", "##..#", "#...#", ".###."),
    "1": ("..#..", ".##..", "..#..", "..#..", "..#..", "..#..", ".###."),
    "2": (".###.", "#...#", "....#", "...#.", "..#..", ".#...", "#####"),
    "3": ("#####", "...#.", "..#..", "...#.", "....#", "#...#", ".###."),
    "4": ("...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."),
    "5": ("#####", "#....", "####.", "....#", "....#", "#...#", ".###."),
    "6": ("..##.", ".#...", "#....", "####.", "#...#", "#...#", ".###."),
    "7": ("#####", "....#", "...#.", "..#..", ".#...", ".#...", ".#..."),
    "8": (".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."),
    "9": (".###.", "#...#", "#...#", ".####", "....#", "...#.", ".##.."),
    "A": (".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "B": ("####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."),
    "C": (".###.", "#...#", "#....", "#....", "#....", "#...#", ".###."),
    "D": ("###..", "#..#.", "#...#", "#...#", "#...#", "#..#.", "###.."),
    "P": ("####.", "#...#", "#...#", "####.", "#....", "#....", "#...."),
    "·": (".....", ".....", ".....", ".##..", ".##..", ".....", "....."),
    " ": (".....", ".....", ".....", ".....", ".....", ".....", "....."),
}
_GLYPH_W, _GLYPH_H = 5, 7


def _png_bytes(width: int, height: int, raw: bytes) -> bytes:
    """Wrap already-filtered RGB scanlines (``raw``) as a PNG."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _solid_buffer(width: int, height: int, rgb: tuple[int, int, int]) -> bytearray:
    """A mutable raw-scanline buffer (filter byte 0 + RGB pixels) of one colour."""
    stride = 1 + width * 3
    buf = bytearray(stride * height)
    row_pixels = bytes(rgb) * width
    for y in range(height):
        base = y * stride
        buf[base] = 0  # filter: none
        buf[base + 1 : base + stride] = row_pixels
    return buf


def _draw_text(
    buf: bytearray,
    width: int,
    text: str,
    *,
    cx: int,
    cy: int,
    scale: int,
    rgb: tuple[int, int, int],
) -> None:
    """Plot ``text`` centred on (``cx``, ``cy``) into ``buf`` at ``scale``×."""
    stride = 1 + width * 3
    pix = bytes(rgb)
    char_w = (_GLYPH_W + 1) * scale  # +1 column of spacing between glyphs
    total_w = char_w * len(text) - scale  # no trailing gap → true centring
    x0 = cx - total_w // 2
    y0 = cy - (_GLYPH_H * scale) // 2
    for i, ch in enumerate(text):
        glyph = _FONT.get(ch, _FONT[" "])
        gx = x0 + i * char_w
        for ry, row in enumerate(glyph):
            for rxc, cell in enumerate(row):
                if cell != "#":
                    continue
                for sy in range(scale):
                    py = y0 + ry * scale + sy
                    if py < 0:
                        continue
                    base = py * stride + 1
                    for sx in range(scale):
                        px = gx + rxc * scale + sx
                        off = base + px * 3
                        if 0 <= px < width and off + 3 <= len(buf):
                            buf[off : off + 3] = pix


def _ink_for(bg: tuple[int, int, int]) -> tuple[int, int, int]:
    """Black or white, whichever reads on ``bg`` (perceived luminance)."""
    lum = 0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]
    return (20, 20, 20) if lum > 140 else (245, 245, 245)


def make_png(width: int, height: int, seed: int = 0) -> bytes:
    """A valid solid-colour PNG. The colour is derived from ``seed`` so distinct
    panels/variants render as visibly different images. Kept for callers that
    only need a plain image; the worker path uses :func:`make_labeled_png`."""
    rgb = _PALETTE[seed % len(_PALETTE)]
    return _png_bytes(width, height, bytes(_solid_buffer(width, height, rgb)))


def make_labeled_png(
    width: int, height: int, *, seq: int, panel: int, variant: int
) -> bytes:
    """A PNG that *shows its place in the sequence*: the 1-based output number
    big and centred, with a ``P{panel}·{variant-letter}`` label beneath, on a
    per-index colour. Lets the client's generating animation + review page be
    visually verified against the order the worker produced them."""
    bg = _PALETTE[seq % len(_PALETTE)]
    ink = _ink_for(bg)
    buf = _solid_buffer(width, height, bg)
    letter = chr(ord("A") + variant) if 0 <= variant < 26 else str(variant)

    # Lay the big number above its panel/variant label as one vertically-centred
    # block so both stay on-canvas at any size.
    num_scale = max(4, min(width, height) // 12)
    label_scale = max(2, num_scale // 3)
    num_h = _GLYPH_H * num_scale
    label_h = _GLYPH_H * label_scale
    spacing = 2 * label_scale
    top = (height - (num_h + spacing + label_h)) // 2
    _draw_text(buf, width, str(seq + 1), cx=width // 2, cy=top + num_h // 2,
               scale=num_scale, rgb=ink)
    _draw_text(buf, width, f"P{panel + 1}·{letter}", cx=width // 2,
               cy=top + num_h + spacing + label_h // 2, scale=label_scale, rgb=ink)
    return _png_bytes(width, height, bytes(buf))


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


def _seq_from_client_id(client_id: str) -> int:
    """The worker's client_id is ``{story_id}-{index}``; pull off the index
    (story_id is a ULID, so it has no ``-``). Falls back to 0 if absent."""
    tail = client_id.rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def _panel_variant_from_prefix(
    prefix: str, *, default_panel: int
) -> tuple[int, int]:
    """Read the storybook panel + variant out of a SaveImage ``filename_prefix``.

    The worker substitutes the template's per-panel prefixes, e.g.
    ``{user}_{story}_P3_V2`` (panel 3, variant V2) or, for the single-panel
    template, ``{user}_{story}_V1``. So one ComfyUI run that saves V1 + V2
    yields two correctly-distinguished images even though they share a
    client_id. ``_V<n>`` is 1-based; we return a 0-based variant (V1→0=A …).
    Falls back to ``default_panel``/variant 0 when the prefix carries no marker.
    """
    match = re.search(r"(?:_P(\d+))?_V(\d+)$", prefix)
    if match is None:
        return default_panel, 0
    panel = int(match.group(1)) if match.group(1) is not None else default_panel
    return panel, max(0, int(match.group(2)) - 1)


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

    # A single run saves every variant (e.g. V1 + V2), so we read each output's
    # storybook panel/variant from its own filename_prefix — they share a
    # client_id and can't be told apart otherwise. The flat sequence number
    # (panel × variants + variant) drives the colour + big digit, so the images
    # *announce their place in the sequence*. client_id's trailing index is the
    # fallback panel when a prefix carries no _P<n> marker (single-panel template).
    variants = max(1, app[VARIANTS])
    default_panel = _seq_from_client_id(client_id)

    outputs: dict[str, Any] = {}
    for node_id, node in prompt.items():
        if node.get("class_type") == "SaveImage":
            prefix = node.get("inputs", {}).get("filename_prefix", "output")
            filename = f"{prefix}_00001_.png"
            panel, variant = _panel_variant_from_prefix(
                prefix, default_panel=default_panel
            )
            seq = panel * variants + variant
            app[IMAGES][filename] = make_labeled_png(
                app[WIDTH], app[HEIGHT], seq=seq, panel=panel, variant=variant
            )
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
    variants_per_panel: int = 1,
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
    # ComfyUI's API carries no panel/variant info, so the mock can't infer the
    # A/B layout from the wire. It's set out-of-band to match the story's
    # template (the demo's template 4 = 2) so the labels read correctly.
    app[VARIANTS] = variants_per_panel
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
        variants_per_panel=int(os.environ.get("MOCK_VARIANTS_PER_PANEL", "1")),
        max_upload_bytes=int(os.environ.get("MOCK_MAX_UPLOAD_MB", "64")) * 1024 * 1024,
    )
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("MOCK_PORT", "8188")))


if __name__ == "__main__":  # pragma: no cover
    main()
