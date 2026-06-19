---
name: story-text
description: Write the read-aloud storybook text for a story's panels — the per-panel narration and conversational dialog that sits next to the generated images. Use when asked to write, add, or edit a story's text/narration/story-book copy, add the words for a story, or fill in the `texts` for a `<type>_<id>.json`. Writes the `texts` array of imagegen/prompts/<type>_<id>.json. Pairs with the `story-prompts` skill.
---

# story-text

Write the **read-aloud storybook text** for a story: one short line per panel,
the words a parent reads aloud while the child looks at that panel's picture.
For an N-panel story this is N strings, stored as the `texts` array of
`imagegen/prompts/<type>_<id>.json` — **parallel to `prompts`**, so `texts[i]`
is the narration for the panel that `prompts[i]` draws.

Read `imagegen/prompts/README.md` first for the file schema. This skill owns
**only** the `texts` field; the `story-prompts` skill owns the image `prompts`
and all other metadata, and `character-config` owns `character.json`.

## What this text is (and is not)

- It is the **book copy**: storybook narration + conversational dialog that the
  reader sees beside the picture. The API catalog serves it as `story_text`
  (synced by `operation/sync_story_catalog.py`).
- It is **never read by the image pipeline.** The model only sees `prompts`. So
  the text is free of every image-prompt mechanic — no `{TOKEN}` placeholders,
  no camera/shot directions, no "person from the input image", no
  "preserve the facial features" tail.

## The voice: second person, the child is the hero

The protagonist is the user's own child — their photo appears in every panel —
but we never know their name. So **address the child directly as "you."** The
child *is* the hero of the book, which makes "you" both natural and personal:

> *You knelt down and helped pick up every single orange.*

- **Past tense narration** ("You knelt…", "Dad smiled…") reads like a classic
  picture book; keep it consistent across all panels of a story.
- **Dialogue is spoken aloud**, in quotes and present tense:
  *"Thank you, dear," the old lady smiled.* / *"I can do this!" you said.*

## Naming the supporting cast (do NOT use {TOKEN})

The image prompts reference generated characters by `{TOKEN}` (which expands to a
long appearance string — *"an elderly East Asian woman with grey hair…"*). That
is wrong for read-aloud text. In the **text**, name each character with a short,
warm, scene-appropriate role that matches what that `{TOKEN}` is:

| In the prompt | In the text (pick what fits the scene) |
|---|---|
| `…_AGE_70_…` (elderly) | "an old lady", "Grandma" |
| `…_PARENT` (the F/M adult guide) | "Mum" / "Dad", or "a grown-up" |
| a teacher-role adult | "your teacher" |
| `…_AGE_06…`/child peer | "your friend", "a boy"/"a girl", "a new friend" |
| an animal in the scene | "the puppy", "the little kitten" |

Keep the role **consistent within a story** (the same friend stays "your
friend"). Match the character's gender to the token (F→Mum/she, M→Dad/he).

## Writing rules

1. **One line per panel, parallel to `prompts`.** `len(texts) == len(prompts)`
   (6 for a `templates/1`/`templates/2` story), same order. Re-read each panel's
   prompt and narrate **that** scene — the location, who is there, and what
   happens — so the words and the picture agree.
2. **Short and simple.** Aim for **1–2 sentences (~12–30 words)** per panel,
   for a 3–6-year-old listener. Plain words, concrete nouns, gentle rhythm.
   Sound effects are welcome ("crash!", "wheee!", "trot, trot, trot").
3. **Tell the scene, then let someone speak.** Most panels pair a line of
   narration with a short line of dialogue. Don't make every panel pure
   narration — the conversational voice is the point.
4. **Follow the story arc the panels already tell:** establish → problem/choice
   → trying → turn → resolution. Carry feelings across panels (worried → brave →
   proud) so it reads as one journey, not six captions.
5. **Land the lesson on the last panel.** Close with a warm line that states or
   gently implies the `lesson` field — *"Your kindness had come back to you."* /
   *"A tidy room is a happy room."* Make it feel earned, not preachy.
6. **No image-prompt machinery.** No `{TOKEN}`, no "input image", no camera/shot
   words, no style or face-preservation text. (If you catch yourself writing
   `{`, stop.)

## Per-panel checklist

For every entry in `texts`, confirm:

- [ ] Narrates the **same scene** as the matching `prompts[i]`.
- [ ] Second person ("you"); tense consistent with the rest of the story.
- [ ] Short (~1–2 sentences) and simple enough to read aloud to a young child.
- [ ] Supporting cast named by a warm role (no `{TOKEN}`, no appearance dump).
- [ ] Has spoken dialogue where the scene invites it.
- [ ] No image-prompt mechanics anywhere.
- [ ] (Final panel only) lands the `lesson`.

## Worked reference

See `imagegen/prompts/1_1.json` ("Kindness Comes Back Around"): its `texts`
array narrates each of the six prompt panels in second person with dialogue, and
the last line ("Your kindness had come back to you.") lands the lesson.

## Validate before done

- [ ] File is valid JSON; `texts` is a list of non-empty strings placed right
      after `prompts`.
- [ ] `len(texts) == len(prompts)` (quick check:
      `python3 -c "import json;d=json.load(open('imagegen/prompts/1_1.json'));assert len(d['texts'])==len(d['prompts'])"`).
- [ ] No `{` appears in any `texts` entry (grep to confirm).
- [ ] Re-run the per-panel checklist on each line.

## Propagate to the catalog

After editing `texts`, run the appropriate per-stage wrapper so the API server
serves the new `story_text` (it maps the JSON's `texts` → the catalog's
`story_text`):

```bash
operation/stages/dev/sync_story_catalog.sh [--template <type>_<id>] [--dry-run]
operation/stages/preprod/sync_story_catalog.sh [--template <type>_<id>] [--dry-run]
operation/stages/prod/sync_story_catalog.sh [--template <type>_<id>] [--dry-run]
```
