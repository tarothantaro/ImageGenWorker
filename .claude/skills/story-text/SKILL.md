---
name: story-text
description: Write a story's narrative spine — the per-panel gist (the intended beat each panel must land) and the read-aloud storybook text (narration + conversational dialog) that sits beside each generated image. Use when asked to write, add, or edit a story's text/narration/story-book copy/dialog, write the gist or beats of a story, add the words for a story, or fill in the `gists`/`texts` of a `<type>_<id>.json`. Writes the `gists`, `texts`, `title`, and `lesson` of imagegen/prompts/<type>_<id>.json. First skill in the pipeline; pairs with `story-prompts` (turns gists into image prompts) and `story-text-eval` (grades the result).
---

# story-text

Write the **narrative spine** of a story: for each panel, the **gist** (one
sentence stating the beat the panel must land) and the **read-aloud text** (the
storybook narration + dialog a parent reads aloud while the child looks at that
panel's picture). For an N-panel story that is N gists and N texts, stored as the
`gists` and `texts` arrays of `imagegen/prompts/<type>_<id>.json` — both
**parallel to `prompts`**, so `gists[i]` / `texts[i]` belong to panel *i*.

This skill is **first** in the authoring pipeline. It decides *what each panel is
about* (gist) and *the words on the page* (text); the `story-prompts` skill then
turns each gist into the Qwen-Image-Edit image prompt, and `story-text-eval`
grades the gist + text. Read `imagegen/prompts/README.md` first for the file
schema.

**Ownership.** This skill owns `title`, `lesson`, `gists`, and `texts` — the
story's beats, its moral, and its words. The `story-prompts` skill owns the image
`prompts` and `characters` (it reads your gists as its spec); `character-config`
owns `character.json`. Grade your output with `story-text-eval`.

## The two arrays this skill writes

### 1. `gists` — the per-panel intended beat (the eval spec)

A gist is a **single sentence** capturing what *that panel must show*: its
setting, who is present, the key action/interaction, and the narrative beat —
**with all style, camera and identity boilerplate stripped out**. It is the
panel's testable *intent*: the shared spec the downstream skills grade against.
`story-prompts` turns each gist into a prompt; `story-prompts-eval` (text-only)
checks "would this prompt, rendered faithfully, satisfy the gist?"; `image-eval`
(vision) checks "does the generated image satisfy the gist?".

- Write a gist for **every** panel, same length/order as `texts` (and `prompts`).
- Refer to the protagonist as **"the child"** and the supporting cast by **role**
  ("the elderly woman", "a friend") — a gist carries **no** placeholders at all
  (no `{NAME}`, no `{TOKEN}`). It is an internal spec, never read aloud.
- Keep it boilerplate-free: no "person from the input image", no camera/shot
  words, no "preserve the facial features" tail, no style register.

### 2. `texts` — the read-aloud storybook words

The **book copy**: storybook narration + conversational dialog the reader sees
beside the picture, one short passage per panel. The API catalog serves it as
`story_text` (synced by `operation/sync_story_catalog.py`). It is **never read by
the image pipeline** — the model only sees `prompts` — so it is free of every
image-prompt mechanic: no camera/shot directions, no "person from the input
image", no "preserve the facial features" tail, and **no character `{TOKEN}`**.

## The voice: third person, the hero named by `{NAME}`

The protagonist is the user's own child — their photo appears in every panel. We
do not bake in a name; instead, **name the hero with the `{NAME}` placeholder**.
At runtime `../Application` replaces `{NAME}` with the role's name (e.g. "Leo")
before showing the page (the role's `name`, the subject of the photo). Write
classic **third-person, past-tense** picture-book narration:

> *One sunny morning, {NAME} set off down the street.*

- **`{NAME}` is the only placeholder allowed in `texts`** — and it is the one the
  hero is referred to by. Use it wherever you would have written "you"/"your"
  (possessive is `{NAME}'s` — *"{NAME}'s kindness came back around"*). After the
  first mention in a passage you may use a pronoun (he/she — match the photo only
  if known, otherwise keep using `{NAME}` or "they") to avoid repeating the name
  in every clause.
- **Past-tense narration** (*"{NAME} knelt down…", "Dad smiled…"*) reads like a
  classic picture book; keep the tense consistent across all panels of a story.
- **No second person.** Don't address the reader as "you" — the hero is a named
  character in the story, not the listener.

## Dialogue: name the speaker, and make it a real conversation

Dialog is the heart of the read-aloud book — don't settle for one short line.
**When two characters are together, give them a genuine exchange** (aim for
**2–4 short spoken turns**) so the page feels alive, and **attribute each line to
its speaker by name**:

> *"Oh no — your oranges!" cried {NAME}, rushing over.*
> *"They're rolling everywhere!" said the old lady.*
> *"Don't worry, I'll catch them," said {NAME}, kneeling down to scoop them up.*
> *"Thank you, dear — you're so kind," she said with a warm smile.*

- **Attribute the hero's lines with `{NAME}`** (*said {NAME}* / *{NAME} said* /
  *cried {NAME}*); attribute everyone else by their **warm role** (*said the old
  lady*, *Dad laughed*, *"Wheee!" giggled the puppy's owner*). Every spoken line
  in a conversation should make clear **who is speaking**.
- **Vary the speech verbs** (said, asked, whispered, laughed, cried, called) and
  add a tiny stage action where it helps (*…, kneeling down*, *…, with a grin*).
- **Let the dialog carry the beat.** The conversation should advance the same
  thing the gist describes (the offer, the apology, the thank-you), not just
  decorate it.

## Naming the supporting cast (warm role, never `{TOKEN}`)

The image prompts reference generated characters by `{TOKEN}` (which expands to a
long appearance string — *"an elderly East Asian woman with grey hair…"*). That
is wrong for read-aloud text. In the **text** (and **gist**), name each character
with a short, warm, scene-appropriate role that matches what that `{TOKEN}` is:

| In the prompt | In the text/gist (pick what fits the scene) |
|---|---|
| `…_AGE_70_…` (elderly) | "an old lady", "Grandma" |
| `…_PARENT` (the F/M adult guide) | "Mum" / "Dad", or "a grown-up" |
| a teacher-role adult | "the teacher" |
| `…_AGE_06…` / child peer | "a friend", "a boy" / "a girl", "a new friend" |
| an animal in the scene | "the puppy", "the little kitten" |

Keep the role **consistent within a story** (the same friend stays "the friend").
Match the character's gender to the token (F→Mum/she, M→Dad/he).

## Writing rules

1. **One gist + one text per panel, parallel to `prompts`.**
   `len(gists) == len(texts) == len(prompts)` (6 for a `templates/1`/`templates/2`
   story), same order. The gist and the text narrate the **same beat** — location,
   who is there, what happens.
2. **Short and simple text.** Aim for **2–4 sentences (~20–45 words)** per panel —
   enough room for a small conversation, still readable to a 3–6-year-old. Plain
   words, concrete nouns, gentle rhythm. Sound effects welcome ("crash!",
   "wheee!", "trot, trot, trot").
3. **Narrate, then converse.** Most panels open with a line of narration and then
   let the characters **talk** — a named, multi-turn exchange wherever two people
   share the scene (see "Dialogue"). A purely solo panel can be narration with a
   single spoken thought (*"I can do this!" said {NAME}.*).
4. **Follow the arc the panels tell:** establish → problem/choice → trying → turn
   → resolution. Carry feelings across panels (worried → brave → proud) so it
   reads as one journey, not six captions.
5. **Land the lesson on the last panel.** Close with a warm line that states or
   gently implies the `lesson` field — *"{NAME}'s kindness had come back around."*
   / *"A tidy room is a happy room."* Make it feel earned, not preachy.
6. **`{NAME}` only; no image-prompt machinery.** The only `{` allowed in a text is
   `{NAME}`. No character `{TOKEN}`, no "input image", no camera/shot words, no
   style or face-preservation text. Gists carry **no** `{` at all.

## Per-panel checklist

For every panel, confirm:

- [ ] **Gist** is one boilerplate-free sentence (setting + who + key action/beat),
      protagonist as "the child", cast by role, **no `{`**.
- [ ] **Text** narrates the **same beat** as the gist (and the matching `prompts[i]`
      if it exists).
- [ ] Third person; the hero is named **`{NAME}`** (never "you"); tense consistent
      with the rest of the story.
- [ ] Where two characters share the scene, a **named, multi-turn conversation**
      (2–4 turns), every spoken line attributed to its speaker.
- [ ] Supporting cast named by a warm role (no `{TOKEN}`, no appearance dump).
- [ ] Only `{NAME}` appears as a `{…}` in the text; **no other placeholder**.
- [ ] (Final panel only) lands the `lesson`.

## Worked reference

See `imagegen/prompts/1_1.json` ("Kindness Comes Back Around"): its `gists` state
each panel's beat boilerplate-free, and its `texts` narrate those six beats in
third person with `{NAME}` and named, multi-turn dialogue, landing the lesson on
the last line.

## Validate before done

- [ ] File is valid JSON; `gists` and `texts` are lists of non-empty strings.
- [ ] `len(gists) == len(texts) == len(prompts)` (quick check:
      `python3 -c "import json;d=json.load(open('imagegen/prompts/1_1.json'));assert len(d['texts'])==len(d['gists'])==len(d['prompts'])"`).
- [ ] No `{` other than `{NAME}` appears in any `texts` entry, and **no `{`** in any
      `gists` entry (grep to confirm).
- [ ] Run `story-text-eval` to grade gist↔text alignment, voice, and dialogue.
- [ ] Re-run the per-panel checklist on each panel.

## Propagate to the catalog

After editing `gists`/`texts`, run the appropriate per-stage wrapper so the API
server serves the new `story_text` (it maps the JSON's `texts` → the catalog's
`story_text`):

```bash
operation/stages/dev/sync_story_catalog.sh [--template <type>_<id>] [--dry-run]
operation/stages/preprod/sync_story_catalog.sh [--template <type>_<id>] [--dry-run]
operation/stages/prod/sync_story_catalog.sh [--template <type>_<id>] [--dry-run]
```
