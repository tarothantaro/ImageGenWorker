---
name: story-text-eval
description: Evaluate and improve a story's gists + read-aloud texts from the text alone — no image generation, no ComfyUI, no GCS, no vision model. Use when asked to evaluate/grade/review/critique a story's gist, dialog, narration, or read-aloud text, check the story-book copy before rendering, verify the words match each panel's beat, or confirm the text follows the story-text rules (third-person {NAME} voice, named multi-turn dialogue, lands the lesson, {NAME}-only placeholder, no character {TOKEN}). The narrative-side counterpart of `story-prompts-eval` (which grades the image prompts). Reads imagegen/prompts/<type>_<id>.json; pairs with the `story-text` and `story-prompts-eval` skills.
---

# story-text-eval

Grade a story's **gists** and **read-aloud texts** (the `story-text` output)
against the `story-text` rules, and recommend concrete edits — **before** the
words ship to the catalog. The narrative spine is cheap to fix in text: a gist
that drifts from its panel, a flat one-line dialog, an un-converted second-person
"you", a leaked character `{TOKEN}` — all visible here, none needing a render.
This skill **reads and grades only** — it writes nothing; apply fixes via the
`story-text` skill.

This is the narrative-side sibling of `story-prompts-eval`:

| Skill | Grades | Question it answers |
|---|---|---|
| **story-text-eval** (this) | the `gists` + `texts` | "Does each panel's text land its gist, in the right voice, with vivid named dialogue?" |
| `story-prompts-eval` | the image `prompts` | "Would this prompt, rendered faithfully, satisfy its gist?" |
| `image-eval` | the **generated images** | "Does the image the model produced satisfy the prompt + gist?" |

The **gist** is the shared spec: a one-sentence statement of what each panel must
show, authored by `story-text`. The full pipeline is `story-text` (gist +
dialog) → **story-text-eval** (this) → `story-prompts` (prompt from the gist) →
`story-prompts-eval` → render → `image-eval`. Run this first; only move on once
the narrative is clean.

## Workflow

### 1. Run the mechanical linter

```bash
PYTHONPATH=. ~/python_env/torch-env/bin/python \
    .claude/skills/story-text-eval/lint_texts.py --story 1_1     # one story
PYTHONPATH=. ~/python_env/torch-env/bin/python \
    .claude/skills/story-text-eval/lint_texts.py --all           # whole catalog
```

`lint_texts.py` decides everything deterministic from the text and prints, per
panel, the gist, the text, and any findings:

- **FAIL** (load-bearing, unambiguous) — wrong gist/text counts; an empty gist or
  text; a gist that carries **any** `{placeholder}`; a text that uses a `{TOKEN}`
  other than `{NAME}` (a character appearance string leaked into the read-aloud
  copy).
- **WARN** (review) — a text with **no `{NAME}`** (is the hero named, or is it the
  old "you" voice?); a bare **`you`/`your`** (fine only as in-quote direct
  address, never narration); a panel with **no spoken dialogue**; quoted dialogue
  with **no speaker attribution** (no `said`/`asked`/… and no `{NAME}`); a very
  **short** text (< 8 words — not the 2–4 vivid sentences the skill wants); and at
  the story level, **too few panels with dialogue**.

Exit code is non-zero if any FAIL exists. Add `--json` for machine-readable
findings. **Resolve every FAIL** (via `story-text`); judge each WARN — many are
fine in context (a solo panel with a single spoken thought; a "you" inside a
character's direct address), but say why you're keeping it.

### 2. Judge what the regex can't (the core of this skill)

Using the gists + texts the linter printed, judge **each panel** on the things
only a reader can:

- **Gist ↔ text alignment** — does the read-aloud text narrate the **same beat**
  as the gist (same setting, same people, same key action/turn)? Flag every
  divergence both ways: the text invents something the gist doesn't intend, or the
  gist's beat is missing from the words. If they disagree, decide which is right
  and recommend fixing the other (usually tighten the text; sometimes the gist was
  mis-stated).
- **Voice** — third person, past tense, the hero named with **`{NAME}`** (never
  "you" in narration). Tense consistent across all six panels.
- **Dialogue is vivid and named** — where two characters share the scene, is there
  a **real, multi-turn conversation** (not one flat line), with **every spoken
  line attributed to its speaker** (`said {NAME}` / `said the old lady`)? Call out
  panels whose dialog is too short, one-sided, or unattributed.
- **Supporting cast by warm role** — characters named as "the old lady", "Dad", "a
  friend" — never an appearance dump, never a `{TOKEN}`.
- **Arc + lesson** — feelings carry across the six panels (worried → brave →
  proud), and the **final panel lands the `lesson`** warmly, not preachily.

### 3. Recommend concrete fixes

For each issue, write the **specific edit** to the gist or text string, tied to
the rule it satisfies — concrete enough to paste in. Prefer fixing the text to
match the gist; only change the gist when the gist itself mis-states the beat.

### 4. Report back

Give a per-story verdict (`✅ ready` / `⚠️ fix text first` / `❌ rework`), the
FAIL/WARN counts, and the panels needing edits with their fixes. Note this is a
**text-only** check. Structure the written report like:

```markdown
# Story-text eval — <title> (<story>)

- **Verdict:** ✅ ready / ⚠️ fix text first / ❌ rework — one line
- **Mechanical:** <F> FAIL, <W> WARN (from lint_texts.py)

## Panels
### Panel N
- Gist: "<gist>"
- Text: "<text>"
- Mechanical: <linter findings, or "clean">
- Gist alignment: pass | **gap** — <which beat the text misses or invents>
- Voice / dialogue / cast / lesson: <only the ones with an issue>
- Fix: <exact edit to the gist or text string>

## Recommended fixes (paste-ready)
- Panel N: <edit>
```

## Notes

- story-text-eval grades the **same gist** that `story-prompts-eval` checks the
  prompt against and `image-eval` checks the pixels against — so a clean
  story-text eval is the pre-condition for writing prompts.
- The natural loop: `story-text` → **story-text-eval** (this) → fix via
  `story-text` → re-check until clean → `story-prompts` → `story-prompts-eval` →
  generate (`local-batch-eval`) → `image-eval`.
- Texts ship to the catalog as `story_text` (`operation/sync_story_catalog.py`),
  where `../Application` substitutes `{NAME}` with the role's name — so a leaked
  `{TOKEN}` or a wrong placeholder would reach the reader. The linter's `{NAME}`
  checks guard exactly that.
