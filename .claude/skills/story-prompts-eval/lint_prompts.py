#!/usr/bin/env python3
"""Static, text-only linter for a story's image prompts + gists.

The cheap half of prompt iteration: catch the mechanical defects in
``imagegen/prompts/<type>_<id>.json`` **before** spending a ComfyUI run on it.
This checks only what can be decided from the prompt/gist *text* — the structural
invariants and the deterministic `story-prompts` rules (shot/framing cue,
identity-preserve pin right after the protagonist block, the exact person-count
guard as the final sentence, banned cross-panel
reference words, `{TOKEN}` validity,
gist↔prompt parity). It does **not** judge
the semantic prompt↔gist alignment or whether every person has a concrete action
— that is the agent's job in the `story-prompts-eval` skill, reading the prompts + gists
this script prints. No image generation, no LLM, no GCS.

Usage (from the repo root):

    PYTHONPATH=. ~/python_env/torch-env/bin/python \
        .claude/skills/story-prompts-eval/lint_prompts.py --story 1_1
    PYTHONPATH=. ~/python_env/torch-env/bin/python \
        .claude/skills/story-prompts-eval/lint_prompts.py --all
    # machine-readable findings (one JSON object):
    ... lint_prompts.py --story 1_1 --json

Exit code is non-zero if any panel has a FAIL-level finding.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

_SKILL_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SKILL_DIR.parents[2]  # .claude/skills/story-prompts-eval -> repo root
_PROMPTS_DIR = _REPO_ROOT / "imagegen" / "prompts"

# Identity-preserve instruction (story-prompts rule 6). It pins the
# protagonist's look and must sit right AFTER the protagonist's intro block —
# no longer at the prompt tail; the prompt now ENDS with the rule-12 count
# guard. Recognised either as the {INPUT_IMAGE_IDENTITY} placeholder or its
# expanded sentence (the live character.json wording includes "clothes"; the
# older wording omits it).
_IDENTITY_TAILS = (
    "Preserve the clothes, facial features, skin tone and hairstyle of the "
    "person from the input image.",
    "Preserve the facial features, skin tone and hairstyle of the person "
    "from the input image.",
)
_IDENTITY_PLACEHOLDER = "{INPUT_IMAGE_IDENTITY}"
# How the protagonist must be referenced (rule 5).
_PROTAGONIST_REF = "person from the input image"
# Non-person placeholders: everything else in a prompt names a person — the
# protagonist (always +1) and the supporting cast (one per distinct {TOKEN}).
# Used to verify the rule-12 person-count guard.
_NON_PERSON_PLACEHOLDERS = {"INPUT_1_AGE", "INPUT_IMAGE_IDENTITY", "IMAGE_STYLE"}
# Mandatory rule-12 guard: "Exactly <headcount> in the frame, and no other people."
# The headcount list is a single clause (no sentence break), so [^.] keeps the
# match from spanning across an earlier targeted anti-twin guard (rule 11) into
# this canonical one.
_COUNT_GUARD_RE = re.compile(
    r"Exactly\s+(?P<list>[^.]+?)\s+in the frame, and no other people\.", re.I
)
_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
# Camera / shot cue that controls framing (rule 3) — any one suffices.
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

# Standard lesson stories are 6 panels. Adventure stories may be the original
# 6-panel shape or the expanded 12-panel quest shape.
_EXPECTED_PANEL_COUNTS_BY_TYPE = {
    "2": {6, 12},
}
_DEFAULT_EXPECTED_PANEL_COUNTS = {6}

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

# Interaction warnings: common prompt phrasings that render as isolated figures
# instead of one shared action. These stay WARN-level because the human
# prompt↔gist review decides whether the panel actually needs the interaction.
_BALL_HANDOFF_RE = re.compile(
    r"\b(toss(?:es|ing)?|throw(?:s|ing)?)\b.*\bball\b.*\bcatch", re.I
)
_ONE_SHARED_BALL_RE = re.compile(
    r"\b(one|single)\b[^.]*\bball\b|\bball\b[^.]*\bbetween them\b", re.I
)
_FURNITURE_APPROACH_RE = re.compile(
    r"\b(walks?|hurries|runs|steps)\b[^.]{0,80}\btoward the "
    r"(bench|doorway|gate|table|counter|shelf|toy box|sink)\b",
    re.I,
)
_TOKEN_TARGET_RE = re.compile(r"\btoward\s+\{GENDER_[A-Z0-9_]+\}", re.I)
_GREETING_ROW_RE = re.compile(
    r"\b(say(?:ing)? hello|greeting|wave(?:s|d|ing)? back)\b"
    r"(?=[^.]{0,140}\b(row|left-to-right|front-facing)\b)|"
    r"\b(row|left-to-right|front-facing)\b"
    r"(?=[^.]{0,140}\b(say(?:ing)? hello|greeting|wave(?:s|d|ing)? back)\b)",
    re.I,
)
# Generic, reusable interaction templates that reference the cast without naming
# them (e.g. "each person faces the other person or the shared object, with hands
# and gaze directed to one connected action"). A line that points at "each person",
# "the other person", "both people", "the shared object", etc. instead of the
# actual role nouns + the specific prop tells the model there are unspecified extra
# people, and it invents duplicates. Always replace with the exact people and the
# exact shared object/action of the panel (rule 4). FAIL: these templates are never
# correct, regardless of context.
_GENERIC_INTERACTION_RES = (
    re.compile(r"\beach person\b", re.I),
    re.compile(r"\bthe other person\b", re.I),
    re.compile(r"\bboth people\b", re.I),
    re.compile(r"\beach character\b", re.I),
    re.compile(r"\bfaces the others\b", re.I),
    re.compile(r"\bthe shared object\b", re.I),
)

# A supporting-character placeholder (workflow.py's contract): GENDER_/AGE_ with
# an OPTIONAL _RACE_<r> segment and/or a trailing disambiguator suffix.
_CHAR_TOKEN_RE = re.compile(r"^GENDER_[A-Z]+_AGE_[A-Z0-9]+(_[A-Z0-9_]+)?$")
_PLACEHOLDER_RE = re.compile(r"\{([A-Z0-9_]+)\}")


def _identity_pos(prompt: str) -> int:
    """Char index of the identity pin (placeholder or expanded sentence), or -1.

    The pin belongs right after the protagonist's intro block (rule 6); callers
    compare this against the count-guard position to confirm it isn't trailing.
    """
    pos = prompt.find(_IDENTITY_PLACEHOLDER)
    if pos != -1:
        return pos
    for tail in _IDENTITY_TAILS:
        pos = prompt.find(tail)
        if pos != -1:
            return pos
    return -1


def _expected_panel_counts(stem: str) -> set[int]:
    """Return valid prompt counts for a story stem such as ``1_4`` or ``2_1``."""
    story_type = stem.split("_", 1)[0]
    return _EXPECTED_PANEL_COUNTS_BY_TYPE.get(
        story_type, _DEFAULT_EXPECTED_PANEL_COUNTS
    )


def _format_expected_counts(counts: set[int]) -> str:
    """Human-readable panel count expectation for findings."""
    ordered = sorted(counts)
    if len(ordered) == 1:
        return str(ordered[0])
    return " or ".join(str(count) for count in ordered)


def _token_resolvable(token: str, char_data: dict) -> bool:
    """True if ``token`` resolves to a description the worker would render.

    Enumerated (``characters[token].description``) or composable from the modular
    tables (``GENDER_<g>_AGE_<a>`` with an optional ``_RACE_<r>`` and known
    dimensions). Reuses the worker's own composer when importable so this stays
    in lockstep with runtime.
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
        m = re.match(r"^GENDER_([A-Z]+)_AGE_([A-Z0-9]+)(?:_[A-Z0-9_]+)?$", token)
        if not m:
            return False
        g, a = m.groups()
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
    expected_counts = _expected_panel_counts(stem)
    if n not in expected_counts:
        f.add(
            "FAIL",
            None,
            "structure",
            f"len(prompts) == {n}, expected {_format_expected_counts(expected_counts)}",
        )
    if len(gists) != n:
        f.add("FAIL", None, "gists", f"len(gists) == {len(gists)} != {n} prompts")
    if texts and len(texts) != n:
        f.add("WARN", None, "texts", f"len(texts) == {len(texts)} != {n} prompts")

    used_tokens: set[str] = set()
    anchors: dict[str, list[int]] = {}  # norm anchor -> panel numbers

    for i, prompt in enumerate(prompts):
        p = i + 1
        low = prompt.lower()

        # rule 6 — identity pin sits right after the protagonist's intro block,
        # never at the prompt tail (the prompt ends with the count guard).
        id_pos = _identity_pos(prompt)
        if id_pos == -1:
            f.add(
                "FAIL",
                p,
                "identity-pin",
                "missing the preserve-identity pin {INPUT_IMAGE_IDENTITY} (rule 6)",
            )

        # rule 12 — mandatory exact person-count guard, as the FINAL sentence.
        person_tokens = {
            t
            for t in _PLACEHOLDER_RE.findall(prompt)
            if t not in _NON_PERSON_PLACEHOLDERS
        }
        expected_total = 1 + len(person_tokens)  # protagonist + 1 per distinct {TOKEN}
        guard = _COUNT_GUARD_RE.search(prompt)
        if guard is None:
            f.add(
                "FAIL",
                p,
                "count-guard",
                "missing the exact person-count guard (rule 12) — the prompt must "
                "end with 'Exactly <headcount> in the frame, and no other people.'",
            )
        else:
            if prompt[guard.end() :].strip() != "":
                f.add(
                    "FAIL",
                    p,
                    "count-guard",
                    "the person-count guard must be the last sentence of the "
                    "prompt (rule 12)",
                )
            stated = sum(
                _NUM_WORDS.get(w, 0)
                for w in re.findall(r"[a-z]+", guard["list"].lower())
            )
            if stated != expected_total:
                f.add(
                    "FAIL",
                    p,
                    "count-guard",
                    f"person-count guard states {stated} people but the prompt "
                    f"names {expected_total} (1 protagonist + {len(person_tokens)} "
                    "distinct {TOKEN}) — rule 12",
                )

        protagonist_pos = prompt.find(_PROTAGONIST_REF)
        if protagonist_pos == -1:
            f.add("FAIL", p, "protagonist-ref", f"no '{_PROTAGONIST_REF}'")
        elif id_pos != -1:
            expected_id_pos = protagonist_pos + len(_PROTAGONIST_REF) + 1
            if id_pos != expected_id_pos:
                f.add(
                    "FAIL",
                    p,
                    "identity-pin",
                    "the {INPUT_IMAGE_IDENTITY} pin must sit immediately after "
                    "'person from the input image' (rule 6)",
                )
        if not any(c in low for c in _CAMERA_CUES):
            f.add(
                "WARN",
                p,
                "camera-cue",
                "no named camera/shot cue (medium shot, eye-level, …) — rule 3's "
                "lever for framing",
            )
        tokens = [t for t in _PLACEHOLDER_RE.findall(prompt) if _CHAR_TOKEN_RE.match(t)]
        used_tokens.update(tokens)
        for tok, count in Counter(tokens).items():
            if count > 1:
                f.add(
                    "FAIL",
                    p,
                    "tokens",
                    f"{{{tok}}} appears {count}× in one prompt — a character "
                    "{TOKEN} must be used at most once per prompt (it expands to "
                    "the full appearance description, so a repeat injects the whole "
                    "description twice). Name it once and refer to the character "
                    "elsewhere by a role noun/pronoun (rule 5)",
                )
        for tok in dict.fromkeys(tokens):  # unique, first-seen order
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
        if _BALL_HANDOFF_RE.search(prompt) and not _ONE_SHARED_BALL_RE.search(prompt):
            f.add(
                "WARN",
                p,
                "interaction",
                "ball handoff/play should specify one shared ball between the people",
            )
        if _FURNITURE_APPROACH_RE.search(prompt) and not _TOKEN_TARGET_RE.search(
            prompt
        ):
            f.add(
                "WARN",
                p,
                "interaction",
                "approach/invitation should name the target character, not only furniture or an area",
            )
        if _GREETING_ROW_RE.search(prompt):
            f.add(
                "WARN",
                p,
                "interaction",
                "greeting/hello beats should avoid a front-facing row; cue an inward-facing pair or small semicircle with reciprocal gaze/body/wave direction",
            )
        for rx in _GENERIC_INTERACTION_RES:
            if rx.search(prompt):
                f.add(
                    "FAIL",
                    p,
                    "interaction",
                    f"generic interaction template {rx.pattern!r} — name the exact "
                    "people (role nouns) and the exact shared object/action of this "
                    "panel instead; a generic cast reference ('each person', 'the "
                    "other person', 'the shared object', …) makes the model add "
                    "unspecified extra people (rule 4)",
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
