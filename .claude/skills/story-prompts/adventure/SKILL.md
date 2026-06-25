---
name: story-prompts-adventure
description: Sub-skill of `story-prompts` for adventure (type 2) stories. Inherits every story-prompts rule and adds the adventure-specific arc — panel 1 establishes where the input-photo person comes from, panel 2 connects them to the adventure, the middle panels are the quest, and the last panel ties the adventure's outcome back to the person's own life. Also swaps the hard-coded style phrase for a runtime `{IMAGE_STYLE}` placeholder so the visual register is chosen at job time. Use when writing or editing the image `prompts` of an adventure story (`imagegen/prompts/2_<id>.json`).
---

# story-prompts-adventure

A specialization of the parent **`story-prompts`** skill for **adventure (type
2)** stories — the longer quest narrative in the README story-type registry.

**Inherit the parent first.** Read `../SKILL.md` (the `story-prompts` skill) and
apply **every** rule it states — scene-first prompts, one contiguous block per
person, concrete action per person, `{TOKEN}`-only supporting cast,
`{INPUT_IMAGE_IDENTITY}` at the end, verbatim per-scene setting anchors, the
duplication guards, the per-panel checklist, and the validate-before-done gate.
Nothing here replaces those rules; this file only **adds** the adventure arc and
**overrides one rule** (the style phrase → a runtime placeholder, see below).

## The adventure arc (fixed panel roles)

An adventure has a variable panel count (confirm it with the user, same as the
parent). Whatever the count, **three panels carry fixed narrative roles** and the
panels between them are the quest. Every panel is still an independent edit of the
**same single input photo**, so the protagonist is the input-photo person in all
of them.

1. **Panel 1 — origin / where the person comes from.** Establish the
   input-photo person's *ordinary world* before any adventure: the place they
   belong to and a small everyday action that shows their normal life (their
   home, village, neighbourhood, routine). No quest, no call to action yet —
   this panel exists so the later panels have a "home" to contrast against and
   return to. Give it its own canonical setting anchor; this is the anchor you
   will **reuse verbatim** in the final panel.
2. **Panel 2 — the connection to the adventure.** Show *how this particular
   person gets pulled in*: the call, the problem arriving, the discovery, or the
   meeting that hooks them. The beat must make the link personal — the
   protagonist reacts to or accepts the thing that starts the quest, not a
   generic "an adventure begins" tableau. This is usually where the first
   supporting character or inciting object appears.
3. **Middle panels — the quest.** The journey, obstacles, helpers, and the
   turning point, authored exactly per the parent rules. Introduce new scenes
   with new verbatim anchors; reuse an earlier anchor when the story returns to a
   place.
4. **Last panel — outcome tied back to the person's life.** Close the loop:
   bring the adventure's result *home* to the protagonist's own world. Reuse
   panel 1's setting anchor (or clearly return to that ordinary world) and show
   how the person and their everyday life are **changed by what they did** —
   honoured, reunited, braver, carrying the reward into their normal routine.
   The final beat must connect the quest's outcome to *this person*, not end on
   the wider world alone.

Keep these three roles intact even when the user asks for more or fewer middle
panels: the first two panels and the last panel are load-bearing for the arc.

## Image style placeholder (overrides parent rule 9)

The parent says: pick one literal style phrase in panel 1 and repeat it in every
panel. For adventure stories, **do not hard-code the style** — leave it for the
runtime to decide.

- Use the placeholder **`{IMAGE_STYLE}`** wherever the parent would put the
  literal style phrase (e.g. instead of `soft storybook illustration style`).
- Put it in the **same camera/style clause**, right before the closing
  `{INPUT_IMAGE_IDENTITY}` pin, and keep the **camera/shot cue** literal (rule 3
  still applies — `Eye-level medium-wide shot, {IMAGE_STYLE}. {INPUT_IMAGE_IDENTITY}`).
- Use the **identical** `{IMAGE_STYLE}` token in **every** panel — that is how the
  whole book stays one consistent register once the runtime fills it. Never mix a
  literal style phrase and the placeholder within a story.

`{IMAGE_STYLE}` is a **render-time placeholder**, resolved the same way as
`{INPUT_1_AGE}` / `USER_ID` / `STORY_ID` (a flat string replace from the job's
placeholder map — see `imagegen/prompts/README.md` "Runtime placeholder
substitution" and `model.py`), **not** a `character.json` token. The runtime must
supply a value for it; an adventure story is not renderable until `{IMAGE_STYLE}`
is wired into the worker's placeholder map. Leave the token verbatim in the JSON.

## Added checklist (on top of the parent's)

For every prompt, in addition to the parent per-panel checklist:

- [ ] Panel 1 shows the protagonist's **ordinary world / origin** with a small
      everyday action — no quest yet — under a fresh canonical anchor.
- [ ] Panel 2 makes the protagonist's **personal connection** to the adventure
      explicit (they react to / accept the call), not a generic "adventure starts".
- [ ] The **last** panel returns to panel 1's world (anchor reused verbatim) and
      ties the **outcome back to this person's life** (changed, rewarded, home).
- [ ] **Every** prompt ends its camera/style clause with **`{IMAGE_STYLE}`**
      (identical token everywhere) before `{INPUT_IMAGE_IDENTITY}` — no literal
      style phrase anywhere in the story.

## Validate before done (adds to the parent gate)

Everything in the parent's "Validate before done" still applies. Additionally:

- [ ] File is `imagegen/prompts/2_<id>.json` and `type` is `2` (adventure) per the
      README registry.
- [ ] `{IMAGE_STYLE}` appears once per panel and is the **same** token in all
      panels; grep confirms no leftover literal style phrase
      (`grep -c IMAGE_STYLE` == panel count; no `illustration style` literals).
- [ ] First / second / last panels satisfy the three fixed roles above; run
      `story-prompts-eval` for the parent-rule + gist alignment pass.
