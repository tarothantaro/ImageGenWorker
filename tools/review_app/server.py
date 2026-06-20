#!/usr/bin/env python3
"""A tiny, dependency-free web UI for reviewing a local story-eval run.

The ``local-batch-eval`` skill generates every story locally, builds a per-story
``manifest.json`` (via ``prompt-eval/fetch_outputs.py``) and writes a per-story
``report.md`` from the vision judge. This server stitches those together into one
page so a human can review the whole batch at a glance — for each story it shows:

* the **input photo** + run metadata (age, model, when),
* every panel's **actual prompt** sent to ComfyUI (from the manifest, which
  prefers the worker's logged prompt),
* the **output images** (V1 pre-face-swap + V2 face-restored) side by side,
* the **eval result** — the judge's per-panel notes pulled out of ``report.md``
  and shown next to that panel, plus the verdict/summary/fixes.

It reads a run dir produced by ``scripts/generate_stories.py`` + the eval step::

    <run-dir>/
    ├── run.json                       # batch metadata
    ├── input.<ext>                    # the input photo
    └── eval/<story>__<story_id>/      # one per story
        ├── manifest.json
        ├── report.md
        └── *.png

Uses only the Python standard library (``http.server``) — no Flask, no build
step. Start it and open the printed URL::

    ~/python_env/torch-env/bin/python tools/review_app/server.py \
        --run-dir eval_runs/latest --port 8000
"""

from __future__ import annotations

import argparse
import html
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

_IMG_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


# --- run loading -------------------------------------------------------------


def _load_run(run_dir: Path) -> dict:
    """Read run.json (if any) plus every story's manifest + report.

    Returns ``{"meta": <run.json or {}>, "stories": [<story dict>, ...]}``.
    Each story dict carries its manifest, the parsed report sections, and the
    per-story status pulled from run.json — all keyed off the ``eval/`` subdirs
    so the UI works even when run.json is absent (e.g. a hand-built run).
    """
    meta = _read_json(run_dir / "run.json") or {}
    status_by_story = {s.get("story"): s for s in meta.get("stories", [])}

    stories: list[dict] = []
    eval_root = run_dir / "eval"
    for eval_dir in sorted(_iter_eval_dirs(eval_root)):
        manifest = _read_json(eval_dir / "manifest.json")
        if not manifest:
            continue
        report = ""
        report_path = eval_dir / "report.md"
        if report_path.exists():
            report = report_path.read_text(errors="replace")
        stories.append(
            {
                "dir": eval_dir.name,
                "manifest": manifest,
                "report": _parse_report(report),
                "status": status_by_story.get(manifest.get("story"), {}),
            }
        )
    return {"meta": meta, "stories": stories}


def _iter_eval_dirs(eval_root: Path):
    if not eval_root.is_dir():
        return
    for child in eval_root.iterdir():
        if child.is_dir() and (child / "manifest.json").exists():
            yield child


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


# --- report.md parsing -------------------------------------------------------

_VERDICT_RE = re.compile(r"\*\*Verdict:\*\*\s*(.+)")
_PANEL_HDR_RE = re.compile(r"^###\s+Panel\s+(\d+)", re.IGNORECASE)


def _parse_report(text: str) -> dict:
    """Split report.md into verdict, the ``## Summary`` block, per-``### Panel``
    blocks (keyed by panel number) and a trailing fixes block.

    Best-effort and forgiving: anything it can't slot is kept in ``rest`` so the
    UI can still show the whole report. ``panels`` maps ``int -> markdown``.
    """
    if not text.strip():
        return {"verdict": "", "summary": "", "panels": {}, "rest": "", "raw": text}

    verdict = ""
    m = _VERDICT_RE.search(text)
    if m:
        verdict = m.group(1).strip()

    lines = text.splitlines()
    summary: list[str] = []
    rest: list[str] = []
    panels: dict[int, list[str]] = {}
    # section: "pre" (before panels), "summary", "panels", "post"
    section = "pre"
    current_panel: int | None = None

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            heading = stripped[3:].strip().lower()
            if heading.startswith("summary"):
                section = "summary"
                current_panel = None
                continue
            if heading.startswith("panel"):  # "## Panels (V2)"
                section = "panels"
                current_panel = None
                continue
            # any other ## heading (e.g. Recommended fixes) -> trailing content
            section = "post"
            current_panel = None
            rest.append(line)
            continue

        pm = _PANEL_HDR_RE.match(stripped)
        if pm and section in ("panels", "post"):
            current_panel = int(pm.group(1))
            panels.setdefault(current_panel, [])
            continue

        if section == "summary":
            summary.append(line)
        elif section == "panels" and current_panel is not None:
            panels[current_panel].append(line)
        elif section in ("pre", "post"):
            rest.append(line)

    return {
        "verdict": verdict,
        "summary": "\n".join(summary).strip(),
        "panels": {k: "\n".join(v).strip() for k, v in panels.items()},
        "rest": "\n".join(rest).strip(),
        "raw": text,
    }


# --- minimal markdown -> HTML ------------------------------------------------


def _md_inline(text: str) -> str:
    """Escape, then apply ``**bold**`` and `` `code` `` inline."""
    out = html.escape(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"`([^`]+?)`", r"<code>\1</code>", out)
    return out


def _md_to_html(text: str) -> str:
    """Render the small markdown subset report.md uses: headings, ``-`` bullets,
    bold/code inline, blank-line paragraphs. Tables/other lines pass through as
    paragraphs. Good enough to read a report; not a full CommonMark engine."""
    html_parts: list[str] = []
    in_list = False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            html_parts.append("</ul>")
            in_list = False

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            close_list()
            continue
        if line.startswith("### "):
            close_list()
            html_parts.append(f"<h4>{_md_inline(line[4:])}</h4>")
        elif line.startswith("## "):
            close_list()
            html_parts.append(f"<h3>{_md_inline(line[3:])}</h3>")
        elif line.startswith("# "):
            close_list()
            html_parts.append(f"<h2>{_md_inline(line[2:])}</h2>")
        elif line.lstrip().startswith(("- ", "* ")):
            if not in_list:
                html_parts.append("<ul>")
                in_list = True
            item = line.lstrip()[2:]
            html_parts.append(f"<li>{_md_inline(item)}</li>")
        else:
            close_list()
            html_parts.append(f"<p>{_md_inline(line)}</p>")
    close_list()
    return "\n".join(html_parts)


# --- HTML rendering ----------------------------------------------------------

_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI",
    Roboto, Helvetica, Arial, sans-serif; color: #1a1a1a; background: #f6f7f9; }
a { color: #2563eb; text-decoration: none; }
a:hover { text-decoration: underline; }
header.top { position: sticky; top: 0; z-index: 5; background: #111827; color: #fff;
    padding: 12px 20px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
header.top h1 { font-size: 16px; margin: 0; }
header.top .meta { color: #9ca3af; font-size: 13px; }
header.top a { color: #93c5fd; }
.wrap { display: flex; gap: 24px; align-items: flex-start; max-width: 1400px;
    margin: 0 auto; padding: 20px; }
nav.side { position: sticky; top: 64px; width: 240px; flex: 0 0 240px;
    background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 8px;
    max-height: calc(100vh - 96px); overflow: auto; }
nav.side a { display: block; padding: 7px 10px; border-radius: 7px; color: #1a1a1a;
    font-size: 13.5px; }
nav.side a:hover { background: #f3f4f6; text-decoration: none; }
nav.side a.active { background: #eef2ff; font-weight: 600; }
main { flex: 1 1 auto; min-width: 0; }
.card { background: #fff; border: 1px solid #e5e7eb; border-radius: 12px;
    padding: 20px; margin-bottom: 20px; }
.story-head { display: flex; gap: 20px; align-items: flex-start; flex-wrap: wrap; }
.story-head img.input { width: 120px; height: 120px; object-fit: cover;
    border-radius: 10px; border: 1px solid #e5e7eb; }
.story-head h2 { margin: 0 0 4px; font-size: 20px; }
.lesson { color: #4b5563; margin: 0 0 8px; }
.badge { display: inline-block; padding: 2px 10px; border-radius: 999px;
    font-size: 12.5px; font-weight: 600; border: 1px solid transparent; }
.badge.ship { background: #dcfce7; color: #166534; }
.badge.revise { background: #fef9c3; color: #854d0e; }
.badge.regen { background: #fee2e2; color: #991b1b; }
.badge.unknown { background: #e5e7eb; color: #374151; }
.badge.failed { background: #fee2e2; color: #991b1b; }
.panel { display: grid; grid-template-columns: 1fr 1fr; gap: 20px;
    border-top: 1px solid #eef0f2; padding: 18px 0; }
.panel .imgs { display: flex; gap: 12px; flex-wrap: wrap; }
.panel figure { margin: 0; }
.panel figure img { width: 260px; max-width: 42vw; border-radius: 8px;
    border: 1px solid #e5e7eb; cursor: zoom-in; display: block; }
.panel figcaption { font-size: 12px; color: #6b7280; margin-top: 4px; }
.panel h4 { margin: 0 0 6px; }
.prompt { background: #f9fafb; border: 1px solid #eef0f2; border-radius: 8px;
    padding: 10px 12px; font-size: 13.5px; color: #111827; white-space: pre-wrap; }
.prompt .src { font-size: 11.5px; color: #6b7280; display: block; margin-top: 6px; }
.eval p, .eval li { margin: 3px 0; }
.eval ul { margin: 4px 0; padding-left: 20px; }
.eval h4 { margin-top: 10px; }
.report h3 { margin: 14px 0 6px; }
.report code { background: #f3f4f6; padding: 1px 5px; border-radius: 4px; }
.empty { color: #6b7280; font-style: italic; }
.index-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 16px; }
.index-grid a.tile { display: block; background: #fff; border: 1px solid #e5e7eb;
    border-radius: 12px; padding: 16px; color: inherit; }
.index-grid a.tile:hover { border-color: #93c5fd; text-decoration: none; }
.index-grid .tile h3 { margin: 0 0 6px; font-size: 16px; }

/* --- narrow screens (phones / small tablets) ------------------------------ */
@media (max-width: 760px) {
  header.top { padding: 10px 14px; gap: 10px; }
  .wrap { flex-direction: column; gap: 14px; padding: 14px; }
  /* Sidebar stops being a fixed rail: full-width, scrolls within a capped box,
     above the content rather than beside it. */
  nav.side { position: static; width: auto; flex: none; max-height: 38vh;
      top: auto; }
  main { width: 100%; }
  .card { padding: 14px; }
  /* Panel: stack prompt/eval above the images instead of side by side. */
  .panel { grid-template-columns: 1fr; gap: 14px; }
  /* Let the V1/V2 variants share the row and grow to fill it. */
  .panel .imgs figure { flex: 1 1 0; min-width: 0; }
  .panel figure img { width: 100%; max-width: 100%; }
  .index-grid { grid-template-columns: 1fr; }
}
"""

_LIGHTBOX_JS = """
document.addEventListener('click', function (e) {
  if (e.target.tagName === 'IMG' && e.target.closest('.panel')) {
    var o = document.createElement('div');
    o.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:50;'
      + 'display:flex;align-items:center;justify-content:center;cursor:zoom-out';
    var img = document.createElement('img');
    img.src = e.target.src;
    img.style.cssText = 'max-width:95vw;max-height:95vh;border-radius:8px';
    o.appendChild(img);
    o.addEventListener('click', function () { o.remove(); });
    document.body.appendChild(o);
  }
});
"""


def _verdict_badge(verdict: str, status: dict) -> str:
    if status and status.get("status") not in (None, "ok"):
        return '<span class="badge failed">generate failed</span>'
    v = verdict.lower()
    if "ship" in v or "✅" in verdict:
        cls, label = "ship", "ship"
    elif "regenerate" in v or "❌" in verdict:
        cls, label = "regen", "regenerate"
    elif "revise" in v or "⚠️" in verdict:
        cls, label = "revise", "revise"
    elif verdict:
        cls, label = "unknown", "reviewed"
    else:
        cls, label = "unknown", "not evaluated"
    return f'<span class="badge {cls}">{html.escape(label)}</span>'


def _page(title: str, body: str, run_dir_name: str) -> bytes:
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{_CSS}</style></head><body>
<header class="top">
  <h1><a href="/">📖 Story eval review</a></h1>
  <span class="meta">{html.escape(run_dir_name)}</span>
</header>
{body}
<script>{_LIGHTBOX_JS}</script>
</body></html>"""
    return doc.encode("utf-8")


def _render_index(run: dict, run_dir_name: str) -> bytes:
    meta = run["meta"]
    stories = run["stories"]
    bits = ['<div class="wrap" style="display:block">']
    bits.append('<div class="card">')
    if meta:
        gen = html.escape(str(meta.get("generated_at", "—")))
        age = html.escape(str(meta.get("age", "—")))
        model = html.escape(str(meta.get("model_version", "—")))
        bits.append(
            f"<p><strong>Run:</strong> {html.escape(run_dir_name)} &middot; "
            f"<strong>age</strong> {age} &middot; <strong>model</strong> {model} "
            f"&middot; <strong>generated</strong> {gen}</p>"
        )
        if meta.get("input_image"):
            bits.append(
                '<img class="input" style="width:120px;height:120px;'
                'object-fit:cover;border-radius:10px" src="/input">'
            )
    if not stories:
        bits.append(
            '<p class="empty">No stories found. Generate a run first '
            "(scripts/generate_stories.py) and build manifests "
            "(prompt-eval/fetch_outputs.py --local-root …).</p>"
        )
    bits.append("</div>")

    if stories:
        bits.append('<div class="index-grid">')
        for s in stories:
            man = s["manifest"]
            title = html.escape(str(man.get("title") or man.get("story") or s["dir"]))
            story = html.escape(str(man.get("story", "")))
            lesson = html.escape(str(man.get("lesson") or ""))
            badge = _verdict_badge(s["report"].get("verdict", ""), s["status"])
            href = "/story?dir=" + quote(s["dir"])
            bits.append(
                f'<a class="tile" href="{href}"><h3>{title}</h3>'
                f'<p class="lesson">{lesson}</p>'
                f'<p>{badge} <span class="meta">{story}</span></p></a>'
            )
        bits.append("</div>")
    bits.append("</div>")
    return _page("Story eval review", "\n".join(bits), run_dir_name)


def _render_story(run: dict, story: dict, run_dir_name: str) -> bytes:
    man = story["manifest"]
    report = story["report"]
    title = html.escape(str(man.get("title") or man.get("story") or story["dir"]))
    lesson = html.escape(str(man.get("lesson") or ""))
    source = html.escape(str(man.get("source") or man.get("bucket") or ""))
    prompt_source = html.escape(str(man.get("prompt_source") or "—"))
    badge = _verdict_badge(report.get("verdict", ""), story["status"])

    # Sidebar of all stories with the current one active.
    nav = ['<nav class="side">']
    for s in run["stories"]:
        man_s = s["manifest"]
        label = html.escape(str(man_s.get("title") or man_s.get("story") or s["dir"]))
        cls = " active" if s["dir"] == story["dir"] else ""
        nav.append(
            f'<a class="{cls.strip()}" href="/story?dir={quote(s["dir"])}">{label}</a>'
        )
    nav.append("</nav>")

    main = ["<main>"]
    main.append('<div class="card"><div class="story-head">')
    main.append('<img class="input" src="/input" alt="input photo">')
    main.append("<div>")
    main.append(f"<h2>{title} &nbsp;{badge}</h2>")
    if lesson:
        main.append(f'<p class="lesson">{lesson}</p>')
    if report.get("verdict"):
        main.append(
            f"<p><strong>Verdict:</strong> {html.escape(report['verdict'])}</p>"
        )
    main.append(
        f'<p class="meta">source: {source} &middot; prompt source: {prompt_source}</p>'
    )
    main.append("</div></div>")
    if report.get("summary"):
        main.append('<div class="report">' + _md_to_html(report["summary"]) + "</div>")
    main.append("</div>")

    # Panels: group manifest images by panel number; show prompt + V1/V2 + eval.
    panels = _group_panels(man.get("images", []))
    for panel_no in sorted(panels):
        imgs = panels[panel_no]
        prompt = next(
            (i.get("resolved_prompt") for i in imgs if i.get("resolved_prompt")), ""
        )
        psrc = next(
            (i.get("prompt_source") for i in imgs if i.get("prompt_source")), ""
        )
        main.append('<div class="card"><div class="panel">')
        # left: prompt + eval
        main.append("<div>")
        main.append(f"<h4>Panel {panel_no}</h4>")
        main.append('<div class="prompt">' + html.escape(prompt or "(no prompt)"))
        if psrc:
            main.append(f'<span class="src">prompt source: {html.escape(psrc)}</span>')
        main.append("</div>")
        eval_md = report.get("panels", {}).get(panel_no)
        if eval_md:
            main.append('<div class="eval">' + _md_to_html(eval_md) + "</div>")
        else:
            main.append('<p class="empty">No eval notes for this panel.</p>')
        main.append("</div>")
        # right: images
        main.append('<div class="imgs">')
        for img in imgs:
            src = (
                "/img?dir="
                + quote(story["dir"])
                + "&name="
                + quote(Path(img["file"]).name)
            )
            cap = html.escape(
                f"{img.get('variant_label', '')} · {img.get('variant_role', '')}"
            )
            main.append(
                f'<figure><img src="{src}" loading="lazy">'
                f"<figcaption>{cap}</figcaption></figure>"
            )
        main.append("</div>")
        main.append("</div></div>")

    if report.get("rest"):
        main.append(
            '<div class="card report">' + _md_to_html(report["rest"]) + "</div>"
        )
    main.append("</main>")

    body = '<div class="wrap">' + "\n".join(nav) + "\n".join(main) + "</div>"
    return _page(f"{title} — eval", body, run_dir_name)


def _group_panels(images: list[dict]) -> dict[int, list[dict]]:
    panels: dict[int, list[dict]] = {}
    for img in images:
        panels.setdefault(int(img.get("panel_number", 0)), []).append(img)
    for imgs in panels.values():
        imgs.sort(key=lambda i: i.get("variant", 0))
    return panels


# --- HTTP handler ------------------------------------------------------------


def _make_handler(run_dir: Path):
    run_dir = run_dir.resolve()
    run_dir_name = run_dir.name

    class Handler(BaseHTTPRequestHandler):
        # Re-read the run on every request so regenerating / re-judging shows up
        # on refresh without restarting the server.
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            route = parsed.path
            if route == "/":
                self._send_html(_render_index(_load_run(run_dir), run_dir_name))
            elif route == "/story":
                self._serve_story(qs)
            elif route == "/input":
                self._serve_input()
            elif route == "/img":
                self._serve_img(qs)
            else:
                self._send(404, b"not found", "text/plain")

        def _serve_story(self, qs: dict) -> None:
            want = (qs.get("dir") or [""])[0]
            run = _load_run(run_dir)
            story = next((s for s in run["stories"] if s["dir"] == want), None)
            if story is None:
                self._send(404, b"story not found", "text/plain")
                return
            self._send_html(_render_story(run, story, run_dir_name))

        def _serve_input(self) -> None:
            meta = _read_json(run_dir / "run.json") or {}
            name = meta.get("input_image")
            candidates = [run_dir / name] if name else []
            candidates += sorted(run_dir.glob("input.*"))
            for path in candidates:
                if path.exists():
                    self._serve_file(path)
                    return
            self._send(404, b"no input image", "text/plain")

        def _serve_img(self, qs: dict) -> None:
            eval_dir = (qs.get("dir") or [""])[0]
            name = (qs.get("name") or [""])[0]
            # Confine to <run>/eval/<dir>/<name> — no traversal outside the run.
            path = (run_dir / "eval" / eval_dir / name).resolve()
            base = (run_dir / "eval").resolve()
            if base in path.parents and path.exists():
                self._serve_file(path)
            else:
                self._send(404, b"image not found", "text/plain")

        def _serve_file(self, path: Path) -> None:
            mime = _IMG_MIME.get(path.suffix.lower(), "application/octet-stream")
            self._send(200, path.read_bytes(), mime)

        def _send_html(self, body: bytes) -> None:
            self._send(200, body, "text/html; charset=utf-8")

        def _send(self, code: int, body: bytes, mime: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:  # noqa: N802 - quiet the access log
            pass

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--run-dir",
        default="eval_runs/latest",
        help="run dir produced by generate_stories.py + the eval step",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="bind address (default 0.0.0.0 = reachable from other devices on "
        "the LAN; pass 127.0.0.1 to restrict to this machine)",
    )
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir).expanduser()
    if not run_dir.is_dir():
        print(f"[review] run dir not found: {run_dir}")
        return 2

    server = ThreadingHTTPServer((args.host, args.port), _make_handler(run_dir))
    # 0.0.0.0 isn't a browsable address — point at localhost for this machine.
    shown_host = "localhost" if args.host == "0.0.0.0" else args.host
    url = f"http://{shown_host}:{args.port}/"
    n = len(list(_iter_eval_dirs(run_dir / "eval")))
    print(f"[review] serving {run_dir} ({n} stor{'y' if n == 1 else 'ies'}) at {url}")
    print("[review] Ctrl-C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[review] bye")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
