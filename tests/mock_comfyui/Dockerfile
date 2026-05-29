# Mock ComfyUI container — emulates the real ComfyUI HTTP + WebSocket API
# (see ../../DESIGN.md §7.2). No GPU, no models: it streams realistic execution
# events and returns generated PNGs, standing in for ../../../ImageGenComfyui
# during dev and integration tests.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

ENV MOCK_PORT=8188 \
    MOCK_STEP_DELAY=2.0 \
    MOCK_FAIL_MODE=none \
    MOCK_WIDTH=1024 \
    MOCK_HEIGHT=736 \
    PYTHONUNBUFFERED=1

EXPOSE 8188

# python (not curl) — keeps the image to python:slim with no extra packages.
HEALTHCHECK --interval=10s --timeout=5s --start-period=5s --retries=5 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://localhost:8188/system_stats').status==200 else 1)"

CMD ["python", "server.py"]
