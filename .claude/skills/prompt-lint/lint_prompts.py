#!/usr/bin/env python3
"""Static, text-only linter for a story's image prompts + gists.

The cheap half of prompt iteration: catch the mechanical defects in
``imagegen/prompts/<type>_<id>.json`` **before** spending a ComfyUI run on it.
This checks only what can be decided from the prompt/gist *text* — the structural
invariants and the deterministic `story-prompts` rules (far-left when
multi-person, face/camera cue, identity-preserve ending, banned cross-panel
reference words, `{TOKEN}` validity, gist↔prompt parity). It does **not** judge
the semantic prompt↔gist alignment or whether every person has a concrete action
— that is the agent's job in the `prompt-lint` skill, reading the prompts + gists
this script prints. No image generation, no LLM, no GCS.

Usage (from the repo root):

    PYTHONPATH=. ~/python_env/torch-env/bin/python \
        .claude/skills/prompt-lint/lint_prompts.py --story 1_1
    PYTHONPATH=. ~/python_env/torch-env/bin/python \
        .claude/skills/prompt-lint/lint_prompts.py --all
    # machine-readable findings (one JSON object):
    ... lint_prompts.py --story 1_1 --json

Exit code is non-zero if any panel has a FAIL-level finding.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_SKILL_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SKILL_DIR.parents[2]  # .claude/skills/prompt-lint -> repo root
_PROMPTS_DIR = _REPO_ROOT / "imagegen" / "prompts"

# Identity-preserve sentence every prompt must end with (story-prompts rule 6).
_IDENTITY_TAIL = (
    "Preserve the facial features, skin tone and hairstyle of the person "
    "from the input image."
)
# How the protagonist must be referenced (rule 5).
_PROTAGONIST_REF = "person from the input image"
# Camera / shot cue that controls face visibility (rule 3) — any one suffices.
_CAMERA_CUES = (
    "medium shot",
    "medium-wide",
    "medium wide",
    "wide shot",
    "close-up",
    "close view",
    "eye level",
    "eye-level",
    "three-quarter",
    "three quarter",
    "full shot",
    "long shot",
)
# Far-left placement phrasings (constraint #1) — required in any multi-person panel.
_FAR_LEFT = ("far left", "left-most", "leftmost", "on the left", "to the far left")
# Shared visual register that should appear in every panel (rule 8).
_STYLE_PHRASE = "cinematic photography style"

# Cross-panel reference words the no-memory pipeline cannot honour (rule 9).
# FAIL: unambiguous violations — they point at a panel the model never saw.
_BANNED_FAIL = (
    re.compile(r"\bthe same\b", re.I),
    re.compile(r"\bback at\b", re.I),
    re.compile(r"\bas before\b", re.I),
    re.compile(r"transform the scene", re.I),
    re.compile(r"place the person into", re.I),
)
# WARN: usually a cross-panel reference, but legitimately within-panel sometimes
# ("beginning to smile again", a state "now softly lit") — flag for a human look.
_BANNED_WARN = (
    re.compile(r"\bagain\b", re.I),
    re.compile(r"\bonce more\b", re.I),
)

# A supporting-character placeholder (workflow.py's contract): GENDER_/AGE_/RACE_.
_CHAR_TOKEN_RE = re.compile(r"^GENDER_[A-Z]+_AGE_[A-Z0-9]+_RACE_[A-Z_]+$")
_PLACEHOLDER_RE = re.compile(r"\{([A-Z0-9_]+)\}")


def _token_resolvable(token: str, char_data: dict) -> bool:
    """True if ``token`` resolves to a description the worker would render.

    Enumerated (``characters[token].description``) or composable from the modular
    tables (``GENDER_<g>_AGE_<a>_RACE_<r>`` with known dimensions). Reuses the
    worker's own composer when importable so this stays in lockstep with runtime.
    """
    enumerated = char_data.get("characters", {}).get(token, {})
    if isinstance(enumerated, dict) and enumerated.get("description"):
        return True
    if not _CHAR_TOKEN_RE.match(token):
        return False
    try:  # best-effort: grade against the real composer
        import random

        from imagegen.workflow import _compose_random_character

        return _compose_random_character(token, char_data, random.Random(0)) is not None
    except Exception:  # noqa: BLE001 - fall back to a shallow dimensions check
        dims = char_data.get("dimensions", {})
        m = re.match(r"^GENDER_([A-Z]+)_AGE_([A-Z0-9]+)_RACE_([A-Z_]+)$", token)
        if not m:
            return False
        g, a, _ = m.groups()
        return g in dims.get("gender", {}) and a in dims.get("age", {})


def _leading_anchor(prompt: str) -> str:
    """The scene-anchor clause: text before the protagonist is introduced.

    Used only to compare scenes across panels (rule 9 wants the anchor repeated
    verbatim). Cuts at the first protagonist/character mention.
    """
    cut = len(prompt)
    for marker in ("the {INPUT", "{INPUT", _PROTAGONIST_REF, "{GENDER"):
        i = prompt.find(marker)
        if i != -1:
            cut = min(cut, i)
    anchor = prompt[:cut].strip(" ,.—-")
    return anchor


def _norm_anchor(anchor: str) -> str:
    """Normalise an anchor for same-scene grouping (drop leading article, case)."""
    a = anchor.lower().strip()
    a = re.sub(r"^(in|on|at|inside|beside|near)\s+(a|an|the)?\s*", "", a)
    return re.sub(r"[^a-z0-9]+", " ", a).strip()


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
    """Run every mechanical check over one story; record findings; return spec."""
    path = _PROMPTS_DIR / f"{stem}.json"
    spec = json.loads(path.read_text(encoding="utf-8"))
    prompts: list[str] = spec.get("prompts", [])
    gists: list[str] = spec.get("gists", [])
    texts: list[str] = spec.get("texts", [])
    declared = list(spec.get("characters", []))
    char_data = json.loads((_PROMPTS_DIR / "character.json").read_text())

    # --- structural ---------------------------------------------------------
    n = len(prompts)
    if n == 0:
        f.add("FAIL", None, "structure", "no prompts")
        return spec
    if n != 6:
        f.add("FAIL", None, "structure", f"len(prompts) == {n}, expected 6")
    if len(gists) != n:
        f.add("FAIL", None, "gists", f"len(gists) == {len(gists)} != {n} prompts")
    if texts and len(texts) != n:
        f.add("WARN", None, "texts", f"len(texts) == {len(texts)} != {n} prompts")

    used_tokens: set[str] = set()
    anchors: dict[str, list[int]] = {}  # norm anchor -> panel numbers

    for i, prompt in enumerate(prompts):
        p = i + 1
        low = prompt.lower()

        if not prompt.strip().endswith(_IDENTITY_TAIL):
            f.add("FAIL", p, "identity-tail", "missing the preserve-identity ending")
        if _PROTAGONIST_REF not in prompt:
            f.add("FAIL", p, "protagonist-ref", f"no '{_PROTAGONIST_REF}'")
        if not any(c in low for c in _CAMERA_CUES):
            f.add(
                "WARN",
                p,
                "camera-cue",
                "no named camera/shot cue (medium shot, eye-level, …) — rule 3's "
                "lever for face visibility; the panel leans on 'facing the camera' alone",
            )
        if "photorealistic" not in low:
            f.add("WARN", p, "style", "no 'photorealistic' style word (rule 8)")
        if _STYLE_PHRASE not in low:
            f.add("WARN", p, "style", f"no '{_STYLE_PHRASE}' (rule 8 consistency)")

        tokens = [t for t in _PLACEHOLDER_RE.findall(prompt) if _CHAR_TOKEN_RE.match(t)]
        used_tokens.update(tokens)
        if tokens and not any(fl in low for fl in _FAR_LEFT):
            f.add(
                "FAIL",
                p,
                "far-left",
                "multi-person panel without an explicit far-left placement "
                "(constraint #1)",
            )
        for tok in tokens:
            if tok not in declared:
                f.add("WARN", p, "tokens", f"{{{tok}}} used but not in `characters`")
            if not _token_resolvable(tok, char_data):
                f.add(
                    "FAIL", p, "tokens", f"{{{tok}}} not resolvable in character.json"
                )

        for rx in _BANNED_FAIL:
            if rx.search(prompt):
                f.add("FAIL", p, "cross-panel", f"banned reference {rx.pattern!r}")
        for rx in _BANNED_WARN:
            if rx.search(prompt):
                f.add(
                    "WARN",
                    p,
                    "cross-panel",
                    f"possible cross-panel word {rx.pattern!r}",
                )

        anchors.setdefault(_norm_anchor(_leading_anchor(prompt)), []).append(p)

        if i < len(gists):
            g = gists[i]
            if not g or not g.strip():
                f.add("FAIL", p, "gist", "empty gist")
            elif "{" in g:
                f.add("FAIL", p, "gist", "gist contains a {TOKEN} placeholder")

    # tokens declared but never used
    for tok in declared:
        if tok not in used_tokens:
            f.add(
                "WARN",
                None,
                "tokens",
                f"`characters` lists {tok} but no prompt uses it",
            )

    # anchor drift: panels that share a scene but not the verbatim anchor string
    for norm, panel_list in anchors.items():
        if len(panel_list) < 2:
            continue
        raws = {_leading_anchor(prompts[p - 1]) for p in panel_list}
        # ignore differences that are only the leading article (In a / On a / At a)
        stripped = {re.sub(r"^\s*\w+\s+(a|an|the)\s+", "", r, flags=re.I) for r in raws}
        if len(stripped) > 1:
            f.add(
                "WARN",
                None,
                "anchor",
                f"panels {panel_list} share a scene but their setting-anchor wording "
                f"differs (rule 9 wants it verbatim): {sorted(raws)}",
            )

    return spec


def _print_human(stem: str, spec: dict, f: Findings) -> None:
    prompts = spec.get("prompts", [])
    gists = spec.get("gists", [])
    print(f"\n=== {stem}  {spec.get('title', '')!r} ===")
    by_panel: dict[int | None, list[tuple[str, str, str]]] = {}
    for level, panel, check, msg in f.items:
        by_panel.setdefault(panel, []).append((level, check, msg))

    if None in by_panel:
        for level, check, msg in by_panel[None]:
            print(f"  [{level}] (story) {check}: {msg}")

    for i, prompt in enumerate(prompts):
        p = i + 1
        gist = gists[i] if i < len(gists) else "(no gist)"
        issues = by_panel.get(p, [])
        mark = (
            "FAIL"
            if any(lv == "FAIL" for lv, *_ in issues)
            else ("warn" if issues else "ok")
        )
        print(f"\n  P{p} [{mark}]")
        print(f"    prompt: {prompt}")
        print(f"    gist  : {gist}")
        for level, check, msg in issues:
            print(f"    [{level}] {check}: {msg}")

    print(
        f"\n  summary: {f.fails()} FAIL, {f.warns()} WARN across {len(prompts)} panels"
    )
    print(
        "  (mechanical checks only — now judge prompt↔gist alignment + "
        "per-person action by reading the prompts/gists above)"
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
