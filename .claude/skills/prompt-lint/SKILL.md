---
name: prompt-lint
description: Evaluate and improve a story's image prompts from the text alone — no image generation, no ComfyUI, no GCS, no vision model. Use when asked to lint/check/review/critique a story's prompts before rendering, improve prompts cheaply or "without generating images", catch prompt problems early, verify a prompt would actually produce its intended panel, or check that each prompt matches its gist and follows the story-prompts rules (shot/framing cue, one action per person, position cued only when the beat needs it, verbatim scene anchor, identity-preserve ending, no cross-panel reference words, valid {TOKEN}s). The cheap, fast counterpart to `prompt-eval` (which grades generated images). Reads imagegen/prompts/<type>_<id>.json; pairs with the `story-prompts` and `prompt-eval` skills.
---

# prompt-lint

Grade a story's **prompt text** against its **gists** and the `story-prompts`
rules, and recommend concrete edits — **before** spending a ComfyUI run on it.
Image generation is the expensive, slow step; most prompt defects (a panel whose
prompt drifts from its intended beat, a scene anchor that isn't repeated verbatim,
an unresolvable `{TOKEN}`) are visible in the
text and can be fixed for free here. This skill **reads and grades only** — it
generates nothing and edits nothing; apply the fixes via the `story-prompts` skill.

This is the text-only sibling of `prompt-eval`:

| Skill | Input | Question it answers | Cost |
|---|---|---|---|
| **prompt-lint** (this) | the prompt + gist **text** | "Would this prompt, rendered faithfully, satisfy its gist — and does it obey the rules?" | free / instant |
| `prompt-eval` | the **generated images** | "Does the image the model actually produced satisfy the prompt + gist?" | a ComfyUI run + vision judging |

The **gist** (`imagegen/prompts/README.md`, the JSON `gists` array) is the shared
spec: a one-sentence statement of what each panel must show. prompt-lint checks
the prompt against that intent in text; prompt-eval checks the pixels against the
same intent. Run prompt-lint first; only generate once it's clean.

## Workflow

### 1. Run the mechanical linter

```bash
PYTHONPATH=. ~/python_env/torch-env/bin/python \
    .claude/skills/prompt-lint/lint_prompts.py --story 1_1     # one story
PYTHONPATH=. ~/python_env/torch-env/bin/python \
    .claude/skills/prompt-lint/lint_prompts.py --all           # whole catalog
```

`lint_prompts.py` decides everything that is deterministic from the text and
prints, per panel, the prompt, its gist, and any findings:

- **FAIL** (load-bearing, unambiguous) — wrong prompt/gist counts; a panel missing
  the preserve-identity ending; no `"person from the input image"` reference; a
  `{TOKEN}` that doesn't resolve in `character.json`; an empty gist or a gist that
  still carries a `{TOKEN}`; a banned cross-panel reference (`the same`, `back at`,
  `as before`, `transform the scene`, `place the person into`).
- **WARN** (review) — no named camera/shot cue (medium shot, eye-level, … — the
  lever for framing, rule 3); a missing/inconsistent style phrase (rule 8); a `{TOKEN}`
  used but not listed in `characters` (or listed but unused); a `\bagain\b` /
  `once more` that *might* be a cross-panel reference; **anchor drift** — panels
  that share a scene but whose setting-anchor wording isn't verbatim (rule 9).

Exit code is non-zero if any FAIL exists. Add `--json` for machine-readable
findings. **Resolve every FAIL** (via `story-prompts`); judge each WARN — many are
fine in context (e.g. "beginning to smile *again*" within one panel), but say why
you're keeping it.

### 2. Judge what the regex can't — prompt ↔ gist alignment (the core of this skill)

The linter cannot read meaning. Using the prompts + gists it printed, judge **each
panel** on the things only a reader can:

- **Gist alignment** — would this prompt, if the model rendered it faithfully,
  produce an image that **satisfies the gist**? Walk the gist's elements (setting,
  who is present, the key action/interaction, the narrative beat) and confirm each
  is actually instructed by the prompt. Flag every divergence **both ways**: the
  prompt asks for something the gist doesn't intend, or the gist's beat isn't in
  the prompt. Example: gist says the child *offers* a block and the friend
  *accepts*, but the prompt only says the child "holds a block" — the give/take
  beat isn't instructed, so the render won't show it. If prompt and gist disagree,
  decide which is right and recommend fixing the other (usually tighten the prompt;
  sometimes the gist was mis-stated).
- **One concrete action per person** (rule 4) — every person named (protagonist +
  each `{TOKEN}`) has a physical verb/gesture, never just placement + expression.
  A bare `stands`/`sits` + an expression (no action) is a defect; the
  model invents a pose for the idle body.
- **Scene-first, single-block person** (rule 1 / rule 9 "split reference") — the
  panel opens with the scene anchor (no person inside it), then introduces each
  person **once** in a contiguous position+action+expression block. A
  protagonist named near the scene and **again** later reads as two children.
- **Verbatim scene anchor across a scene** (rule 9) — panels set in the same place
  repeat the **identical** anchor string (same adjectives/landmarks/light, only the
  leading article free to differ). The linter's `anchor` WARN points at candidates;
  confirm by reading. A recurring changed-state object (a shattered vase, a tidied
  room) must be worded identically too.
- **Cast by token only** (rule 7) — supporting characters get position/action/
  expression only, never a re-described appearance.

### 3. Recommend concrete prompt fixes

For each issue, write the **specific edit** to the prompt (or gist) string, tied
to the rule it satisfies — concrete enough to paste in. Prefer fixing the prompt
to match the gist; only change the gist when the gist itself mis-states the intent.

### 4. Report back

Give a per-story verdict (`✅ ready to generate` / `⚠️ fix prompts first` /
`❌ rework`), the FAIL/WARN counts, the panels needing edits with their fixes, and
note that this is a **text-only** check — the image-side confirmation is
`prompt-eval` after a render. Structure the written report like:

```markdown
# Prompt lint — <title> (<story>)

- **Verdict:** ✅ ready to generate / ⚠️ fix prompts first / ❌ rework — one line
- **Mechanical:** <F> FAIL, <W> WARN (from lint_prompts.py)

## Panels
### Panel N
- Prompt: "<prompt>"
- Gist: "<gist>"
- Mechanical: <linter findings, or "clean">
- Gist alignment: pass | **gap** — <which gist element the prompt doesn't instruct>
- Action per person / scene-first / anchor / cast: <only the ones with an issue>
- Fix: <exact edit to the prompt string>

## Recommended prompt fixes (paste-ready)
- Panel N: <edit>
```

## Notes

- prompt-lint grades the **same rules** `story-prompts` writes to and the **same
  gist** `prompt-eval` grades the image against — so a clean lint is the
  pre-condition for generating, and `prompt-eval` is the post-condition.
- The natural loop: `prompt-lint` → fix via `story-prompts` → re-lint until clean →
  generate (`local-batch-eval`) → `prompt-eval` on the images.
- The linter reuses the worker's own `{TOKEN}` composer when importable, so its
  "resolvable" verdict matches what `workflow.py` would actually render.
