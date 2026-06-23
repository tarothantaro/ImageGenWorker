#!/usr/bin/env python3
"""Static, text-only linter for a story's gists + read-aloud texts.

The cheap half of story-text iteration: catch the mechanical defects in the
``gists`` and ``texts`` arrays of ``imagegen/prompts/<type>_<id>.json`` — the
output of the ``story-text`` skill — before a human reads them. This checks only
what can be decided from the text: the structural invariants and the
deterministic `story-text` rules (gist/text parity, `{NAME}`-only placeholder in
texts, no placeholders in gists, dialogue present + attributed, no second-person
"you"). It does **not** judge gist↔text alignment or whether the dialogue is
*vivid* — that is the agent's job in the `story-text-eval` skill, reading the
gists + texts this script prints. No image generation, no LLM, no GCS.

Usage (from the repo root):

    PYTHONPATH=. ~/python_env/torch-env/bin/python \
        .claude/skills/story-text-eval/lint_texts.py --story 1_1
    PYTHONPATH=. ~/python_env/torch-env/bin/python \
        .claude/skills/story-text-eval/lint_texts.py --all
    # machine-readable findings (one JSON object):
    ... lint_texts.py --story 1_1 --json

Exit code is non-zero if any panel has a FAIL-level finding.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_SKILL_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SKILL_DIR.parents[2]  # .claude/skills/story-text-eval -> repo root
_PROMPTS_DIR = _REPO_ROOT / "imagegen" / "prompts"

# The one placeholder allowed in a read-aloud text: the protagonist's name,
# substituted with the role name by ../Application at runtime.
_NAME_TOKEN = "NAME"
_PLACEHOLDER_RE = re.compile(r"\{([A-Z0-9_]+)\}")
# Any straight or curly double-quote marks a line of spoken dialogue.
_QUOTE_RE = re.compile(r'["“”]')
# Second person — banned in narration (the hero is named, not the listener).
# Legitimate only inside direct-address dialogue, so this is a WARN.
_SECOND_PERSON_RE = re.compile(r"\b(you|your|you're|you'll|you've|yourself)\b", re.I)
# Speech verbs that attribute a quoted line to a speaker.
_SPEECH_VERBS = (
    "said",
    "asked",
    "replied",
    "answered",
    "added",
    "called",
    "cried",
    "shouted",
    "whispered",
    "murmured",
    "laughed",
    "giggled",
    "chuckled",
    "grinned",
    "smiled",
    "exclaimed",
    "gasped",
    "sighed",
    "wondered",
    "agreed",
    "begged",
    "promised",
    "cheered",
    "yelled",
)
_SPEECH_RE = re.compile(r"\b(" + "|".join(_SPEECH_VERBS) + r")\b", re.I)

_MIN_WORDS = 8  # texts shorter than this aren't the 2–4 vivid sentences the skill wants


class Findings:
    """Collects (level, panel, check, message) findings for one story."""

    def __init__(self) -> None:
        self.items: list[tuple[str, int | None, str, str]] = []

    def add(self, level: str, panel: int | None, check: str, message: str) -> None:
        self.items.append((level, panel, check, message))

    def fails(self) -> int:
        return sum(1 for level, *_ in self.items if level == "FAIL")

    def warns(self) -> int:
        return sum(1 for level, *_ in self.items if level == "WARN")


def lint_story(stem: str, f: Findings) -> dict:
    """Run every mechanical check over one story's gists+texts; record findings."""
    path = _PROMPTS_DIR / f"{stem}.json"
    spec = json.loads(path.read_text(encoding="utf-8"))
    prompts: list[str] = spec.get("prompts", [])
    gists: list[str] = spec.get("gists", [])
    texts: list[str] = spec.get("texts", [])

    # --- structural ---------------------------------------------------------
    n = len(prompts) if prompts else len(gists)
    if n == 0:
        f.add("FAIL", None, "structure", "no gists/texts to lint")
        return spec
    if not texts:
        f.add("FAIL", None, "texts", "no `texts` array")
    if not gists:
        f.add("FAIL", None, "gists", "no `gists` array")
    if texts and len(texts) != n:
        f.add("FAIL", None, "texts", f"len(texts) == {len(texts)} != {n}")
    if gists and len(gists) != n:
        f.add("FAIL", None, "gists", f"len(gists) == {len(gists)} != {n}")

    name_used_anywhere = False
    panels_with_dialogue = 0

    for i in range(n):
        p = i + 1

        # --- gist checks ----------------------------------------------------
        if i < len(gists):
            g = gists[i]
            if not g or not g.strip():
                f.add("FAIL", p, "gist", "empty gist")
            elif "{" in g:
                f.add(
                    "FAIL",
                    p,
                    "gist",
                    "gist contains a {placeholder} (gists carry none)",
                )

        # --- text checks ----------------------------------------------------
        if i >= len(texts):
            continue
        t = texts[i]
        if not t or not t.strip():
            f.add("FAIL", p, "text", "empty text")
            continue

        bad = [tok for tok in _PLACEHOLDER_RE.findall(t) if tok != _NAME_TOKEN]
        if bad:
            f.add(
                "FAIL",
                p,
                "placeholder",
                f"text uses {{{bad[0]}}} — only {{NAME}} is allowed in read-aloud text",
            )
        if f"{{{_NAME_TOKEN}}}" in t:
            name_used_anywhere = True
        else:
            f.add("WARN", p, "name", "no {NAME} — is the hero named (not 'you')?")

        if _SECOND_PERSON_RE.search(t):
            f.add(
                "WARN",
                p,
                "second-person",
                "'you/your' present — fine only as in-quote direct address, "
                "not narration",
            )

        has_dialogue = bool(_QUOTE_RE.search(t))
        if has_dialogue:
            panels_with_dialogue += 1
            if not (f"{{{_NAME_TOKEN}}}" in t or _SPEECH_RE.search(t)):
                f.add(
                    "WARN",
                    p,
                    "attribution",
                    "quoted dialogue but no speaker attribution (said/asked/… "
                    "or {NAME})",
                )
        else:
            f.add(
                "WARN",
                p,
                "dialogue",
                "no spoken dialogue — story-text wants a conversation where the "
                "scene invites it",
            )

        if len(t.split()) < _MIN_WORDS:
            f.add(
                "WARN",
                p,
                "length",
                f"only {len(t.split())} words — aim for 2–4 vivid sentences",
            )

    # --- story-level --------------------------------------------------------
    if texts and not name_used_anywhere:
        f.add(
            "WARN",
            None,
            "name",
            "no {NAME} anywhere — the hero is never named (old 'you' voice?)",
        )
    if texts and panels_with_dialogue * 2 < len(texts):
        f.add(
            "WARN",
            None,
            "dialogue",
            f"only {panels_with_dialogue}/{len(texts)} panels have dialogue — "
            "the conversational voice is the point",
        )

    return spec


def _print_human(stem: str, spec: dict, f: Findings) -> None:
    gists = spec.get("gists", [])
    texts = spec.get("texts", [])
    n = max(len(gists), len(texts))
    print(f"\n=== {stem}  {spec.get('title', '')!r} ===")
    by_panel: dict[int | None, list[tuple[str, str, str]]] = {}
    for level, panel, check, msg in f.items:
        by_panel.setdefault(panel, []).append((level, check, msg))

    if None in by_panel:
        for level, check, msg in by_panel[None]:
            print(f"  [{level}] (story) {check}: {msg}")

    for i in range(n):
        p = i + 1
        gist = gists[i] if i < len(gists) else "(no gist)"
        text = texts[i] if i < len(texts) else "(no text)"
        issues = by_panel.get(p, [])
        mark = (
            "FAIL"
            if any(lv == "FAIL" for lv, *_ in issues)
            else ("warn" if issues else "ok")
        )
        print(f"\n  P{p} [{mark}]")
        print(f"    gist: {gist}")
        print(f"    text: {text}")
        for level, check, msg in issues:
            print(f"    [{level}] {check}: {msg}")

    print(f"\n  summary: {f.fails()} FAIL, {f.warns()} WARN across {n} panels")
    print(
        "  (mechanical checks only — now judge gist↔text alignment, the {NAME} "
        "third-person voice, and whether the dialogue is vivid by reading above)"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--story", help="prompt-file stem, e.g. 1_1")
    g.add_argument("--all", action="store_true", help="lint every story")
    ap.add_argument(
        "--json", action="store_true", help="emit machine-readable findings"
    )
    args = ap.parse_args(argv)

    if args.all:
        stems = sorted(
            (p.stem for p in _PROMPTS_DIR.glob("[0-9]*_[0-9]*.json")),
            key=lambda s: tuple(int(x) for x in s.split("_")),
        )
    else:
        stems = [args.story]

    total_fail = 0
    payload: list[dict] = []
    for stem in stems:
        path = _PROMPTS_DIR / f"{stem}.json"
        if not path.exists():
            print(f"[lint] error: not found: {path}", file=sys.stderr)
            return 2
        f = Findings()
        spec = lint_story(stem, f)
        total_fail += f.fails()
        if args.json:
            payload.append(
                {
                    "story": stem,
                    "title": spec.get("title"),
                    "fails": f.fails(),
                    "warns": f.warns(),
                    "findings": [
                        {"level": lv, "panel": pn, "check": ck, "message": ms}
                        for lv, pn, ck, ms in f.items
                    ],
                }
            )
        else:
            _print_human(stem, spec, f)

    if args.json:
        print(json.dumps(payload if args.all else payload[0], indent=2))
    elif args.all:
        print(f"\n[lint] {len(stems)} stories, {total_fail} FAIL finding(s) total")

    return 1 if total_fail else 0


if __name__ == "__main__":
    sys.exit(main())
