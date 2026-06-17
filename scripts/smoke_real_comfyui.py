#!/usr/bin/env python3
"""Manual smoke test: drive the real ComfyUIModel against a live ComfyUI.

Runs the production transport (``HttpComfyUIClient``, httpx + websocket-client)
and the real :class:`~imagegen.model.ComfyUIModel` through templates/1 /
workflow 1, rendering the prompt set selected by ``--type``/``--id``, against a
running ComfyUI container with a real input photo. This is the GPU/model
integration path that emulators and the in-process mock can't cover (TESTING.md
§7).

Usage (from the repo root, with the ComfyUI container up on :8188):

    ~/python_env/torch-env/bin/python scripts/smoke_real_comfyui.py \
        --url http://localhost:8188 \
        --input tests/assets/test.jpg \
        --type 1 --id 1 \
        --out /tmp/smoke_out

Exits non-zero on any failure. Saves each produced panel image to ``--out``.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from imagegen.model import ComfyUIModel
from imagegen.comfyui_client import HttpComfyUIClient


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8188")
    parser.add_argument("--input", default="tests/assets/test.jpg")
    parser.add_argument("--out", default="/tmp/smoke_out")
    parser.add_argument("--type", type=int, default=1, help="prompt type")
    parser.add_argument("--id", type=int, default=1, help="prompt id")
    parser.add_argument("--story-id", default="smoke")
    parser.add_argument("--user-id", default="tester")
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    log = logging.getLogger("smoke")

    image_bytes = Path(args.input).read_bytes()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    transport = HttpComfyUIClient(base_url=args.url, timeout=args.timeout)
    model = ComfyUIModel(
        transport,
        model_version="comfyui-flux2",
        request_timeout_seconds=args.timeout,
    )

    log.info(
        "submitting story=%s prompt=%s_%s to %s",
        args.story_id,
        args.type,
        args.id,
        args.url,
    )
    started = time.monotonic()
    try:
        panels = model.generate(
            story_id=args.story_id,
            user_id=args.user_id,
            prompt_type=args.type,
            prompt_id=args.id,
            input_images=[image_bytes],
        )
        count = 0
        for index, panel in enumerate(panels):
            dest = out_dir / f"{args.story_id}_{index}.png"
            dest.write_bytes(panel.image)
            count += 1
            log.info(
                "panel %d done: %dx%d, %d bytes, %.1fs → %s",
                index,
                panel.width,
                panel.height,
                len(panel.image),
                panel.processing_seconds,
                dest,
            )
    except Exception as exc:  # noqa: BLE001 — smoke test: report and fail
        log.error("FAILED: %s: %s", type(exc).__name__, exc)
        return 1
    finally:
        transport.close()

    log.info("OK: %d panel(s) in %.1fs total", count, time.monotonic() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
