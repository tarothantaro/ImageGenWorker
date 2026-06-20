---
name: story-prompts
description: Generate Qwen-Image-Edit-2511 story prompt sets for the image-gen worker. Use when asked to write, add, or edit a story's image prompts, create a new story (e.g. "life lesson story 2"), or produce the per-panel prompts that put the input-photo person into a multi-image narrative. Writes imagegen/prompts/<type>_<id>.json. Pairs with the `character-config` skill.
---

# story-prompts

Write the per-panel text prompts that drive the **Qwen-Image-Edit-2511** edit for
one story. Each story is an array of prompts stored at
`imagegen/prompts/<type>_<id>.json`; each prompt is run against
the **same single input photo** to produce one panel image. Read
`imagegen/prompts/README.md` first — it defines the file schema, naming, and
story-type registry. Generated supporting characters come from
`imagegen/prompts/character.json` (owned by the `character-config` skill). The
read-aloud `texts` array in the same file is owned by the `story-text` skill,
not this one — this skill writes the image `prompts` and metadata.

## How the pipeline shapes every prompt

This is not generic text-to-image. The worker (DESIGN.md §7.2) runs **one input
photo** through a Flux/Qwen image-edit + ReActor face-swap graph, once per panel:

- There is exactly **one input image** — the user's photo. The person in it is
  the **protagonist**. Every other character is *conjured by the prompt text*,
  not supplied as a second image.
- The face-swap stage maps the input face onto a **detected face in the output**.
  In this pipeline it targets the **left-most** face — so the protagonist must be
  composed left-most, front-facing, for the swap to land cleanly.
- Panels do **not** share generated pixels. Each panel is an independent edit
  from the same photo. So cross-panel consistency of the supporting cast comes
  **only** from reusing the identical `{TOKEN}` placeholder (which expands to the
  identical appearance string) — never from one panel "remembering" another.

## The two hard constraints (non-negotiable)

1. **Input person on the far left.** Any panel with more than one person places
   the protagonist as the **left-most** figure. State it literally:
   *"The person from the input image stands on the far left …"*. (Solo panels:
   placement is free, but still keep the face visible — see #2.)
2. **Input person's face ≥70% visible.** Compose front-on or three-quarter,
   **facing the camera, face unobstructed**. Avoid back views, deep profiles,
   and anything covering the face (hands, hats, masks, other characters, props).
   If the head must tilt or kneel, add *"head turned toward the camera so the
   face stays clearly visible"*. Only drop this when the narrative truly demands
   it (e.g. a deliberate looking-away-at-a-vista shot) — and say so explicitly.

Both must be **restated in every panel that needs them** — panels are independent
edits and the base model has strong built-in preferences, so an unstated
constraint will be ignored.

## Prompt-writing rules (Qwen-Image-Edit-2511)

Derived from the model's prompt guidance — instruction-style, specific, spatial.

1. **Lead with the composition/action, instruction-style.** Open with what to do:
   *"Place the person from the input image into [the fully described scene] …"*.
   Short and specific beats long and flowery — *"a torn grocery bag, oranges
   rolling out"* over a paragraph of adjectives. Don't open with *"Transform the
   scene"* — see rule 9: the input is the person photo, not a prior panel, so
   there is no existing scene to transform.
2. **Use spatial words to place people** — `far left`, `to the right`,
   `beside them`, `in the background`, `foreground`. This is the lever for
   constraint #1. The model positions by these words, not by image indices.
3. **Name a camera/shot to control face visibility** — `medium shot`,
   `medium-wide shot`, `eye-level`, `three-quarter view`. This is the lever for
   constraint #2. The model obeys camera/angle cues reliably.
4. **Give every person an explicit action.** Each person in the panel — the
   protagonist **and** every supporting character — must be **doing something
   concrete**: a physical verb or a specific gesture/pose anchored to a prop, the
   other character, or their own body (*"kneeling to gather the oranges"*,
   *"holding out a block"*, *"one hand pressed to their chest"*, *"wrapping their
   arms around themselves against the cold"*). **Never leave a person merely
   *standing / sitting + facing the camera + [expression]*** — an expression is
   *not* an action. With nothing to do, the model invents a pose for the idle
   body, and invented poses usually look weird (floating or clipping hands,
   awkward limbs, random gestures). State the expression *in addition to* the
   action, never instead of it. Even a solo panel with no prop or partner must
   give the hands/body a job (*"hugging their teddy bear"*, *"hands on hips,
   looking around"*, *"shielding their head from the rain"*) rather than just
   "standing there". A bare verb of being present (`stands`, `sits`, `is in the
   frame`) does not count; `kneeling`, `walking`, `holding`, `reaching`,
   `clapping`, `pointing`, `hands on hips` do.
5. **Reference the protagonist as "the person from the input image"** (consistent,
   unambiguous). Reference supporting cast **only** by their `{TOKEN}` placeholder
   from `character.json`.
6. **Pin identity at the end of every prompt:** *"Preserve the facial features,
   skin tone and hairstyle of the person from the input image."* The edit + swap
   will otherwise drift the protagonist's look.
7. **Never describe a generated character's fixed appearance.** Their age,
   ethnicity, build, hair and clothing live in `character.json` and arrive via
   the placeholder. In the prompt, give them only **position, action, and
   expression** (`stands to the right, watching gratefully`). Re-describing
   appearance fights the resolved description and breaks consistency. (Exception:
   a deliberate, story-driven change — e.g. *"… now wearing a raincoat over their
   usual clothes"* — added *after* the placeholder.)
8. **Keep style consistent across the story.** Pick one visual register in panel 1
   (e.g. *"soft storybook illustration style"*) and repeat the same phrase in
   every panel so the set reads as one book.
9. **Keep each prompt self-contained — repeat one verbatim setting anchor per
   scene.** Because each panel is an independent edit with **no memory of any
   other panel**, re-establish the protagonist (left + face), the supporting cast
   tokens present, the style, **and the setting** every time. Slightly *reworded*
   re-descriptions of the same place ("on a wooden floor" in one panel, "with a
   wooden floor" in the next, the light dropped from a third) make the location
   visibly drift — to the model each wording is a fresh, different room. So:

   **For every distinct scene in the story, fix ONE canonical setting-anchor
   clause and paste it word-for-word into every panel set in that scene.** Build
   it once as: *[indefinite article] + adjectives + location noun + — key
   furniture/landmarks — + time of day + light* — e.g.
   *"a cozy living room in the afternoon — a wooden shelf on the wall, a wooden
   floor, warm afternoon light"* or *"a sunny playground in the green park — a
   sandbox and a set of swings, soft grass, warm sunlight"*. Reasonable detail,
   not an exhaustive list. Then reuse that **exact string** in every panel of the
   scene (the grammatical frame around it may differ — *"Place the person … into
   `<anchor>` …"* in an establishing panel vs *"In `<anchor>` — the person …"*
   later — but the adjectives/nouns/landmarks/light inside it must be identical).
   When the story moves to a new location, that is a **new scene** with its own
   canonical anchor; if it later returns to an earlier scene, reuse that scene's
   exact anchor again.

   **Layer story-driven change as separate clauses on top of the fixed anchor —
   never by editing the anchor.** A vase that breaks, a room that gets tidied, a
   sandcastle that grows, a night-light that switches on: describe the changed
   state in its own clause after the anchor, and when that changed state recurs
   across panels, word *it* identically too (e.g. *"small scattered blue ceramic
   shards lying flat on the wooden floor (no whole or rounded vase anywhere in
   frame)"* repeated verbatim in every post-break panel). **Light** stays
   identical across the scene as well, except where a lighting shift is itself a
   deliberate story beat (gloom → sun breaking through; a single golden-hour
   resolution) — keep that the only varying token, worded consistently.

   Continuity comes from **repeating the anchor verbatim**, never from a word that
   points at another panel. Introduce the scene with an **indefinite article**
   (*"In a cozy living room …"*) exactly as panel 1 would — every panel is panel 1
   to the model. Three failure modes to avoid, all caused by the no-memory
   pipeline:
   - **Cross-panel reference words** — *"the same"*, *"again"*, *"back at"*, *"as
     before"*, and a stray *"now"* that means "changed since the last panel". The
     model has no memory of any other panel, so *"the same garden"* points at
     nothing; at best the word is wasted, at worst it implies a prior image that
     does not exist. **Drop the word** and let the repeated anchor carry the
     continuity: *"In a cozy living room — a wooden shelf on the wall, a wooden
     floor, warm afternoon light …"*, **not** *"In the same cozy living room …"*.
   - **Bare back-references** — *"Same living room"*, *"the broken vase"* — name
     something the model cannot see; they carry no description, so the setting
     drifts. Spell the anchor (and any referenced object) out in full instead.
   - **"Transform the scene" openers.** The input image is the *person photo*, not
     the previous panel, so there is no prior scene to transform — the instruction
     acts on nothing and the intended setting is lost. Use *"Place the person from
     the input image into [the fully described scene] …"* and describe the scene
     from scratch, including any change of state (e.g. the shattered vase, the
     tidied room).
10. **Negatives sparingly.** This pipeline's prompt is positive-only; if a negative
   is supported, use it for artifacts (*"no extra fingers"*), not concept changes.

## Per-panel checklist

For every prompt in the array, confirm:

- [ ] Opens with an instruction-style composition/action line.
- [ ] If >1 person: protagonist is explicitly **far left**.
- [ ] Protagonist is **facing the camera, face clearly visible** (camera/shot cue
      present); any kneel/tilt adds "head turned toward the camera".
- [ ] **Every person** (protagonist + each supporting character) is given a
      **concrete action/gesture**, not just a placement and an expression.
- [ ] Supporting cast referenced **only** by `{TOKEN}` — placement/action/
      expression only, no appearance, no re-described clothing.
- [ ] Same `{TOKEN}` reused for a character that recurs across panels.
- [ ] Panels sharing a location repeat the **one canonical setting-anchor clause
      verbatim** (same adjectives + location + key furniture/landmarks + time of
      day + light, word-for-word), introduced with an **indefinite article** ("In
      a cozy living room …"). Story-driven state changes (broken vase, tidied
      room) and any recurring changed-state object are layered as separate clauses
      and worded identically across the panels they appear in; light is identical
      across the scene unless a lighting shift is itself a story beat. No
      cross-panel reference word ("the same", "again", "back at", continuity
      "now"), no bare "same room" back-reference, no "Transform the scene" opener.
- [ ] Same style phrase as the rest of the story.
- [ ] Ends with the preserve-identity sentence.

## Writing a story (type 1 = life_lesson)

1. **Pick the lesson** and a 1-sentence statement of it (`lesson` field).
2. **Decide the cast.** Protagonist = input person (implicit). Choose generated
   characters; ensure each token exists in `character.json` — if not, add it with
   the `character-config` skill *before* referencing it.
3. **Plan the arc across 6 panels** (the render template `templates/1` has 6
   panels, so a story has exactly 6 prompts). A clean life-lesson arc:
   *establish → encounter/choice → action → consequence → turn → resolution that
   lands the lesson.* Keep location/time continuity unless the story moves on.
   List the story's distinct scenes up front and write **one canonical
   setting-anchor clause for each** (rule 9); every panel in a scene pastes that
   exact clause, so continuity is locked before you write the per-panel action.
4. **Write each panel** with the rules above. Alternate solo and multi-person
   panels naturally, but every multi-person panel obeys constraint #1, and **every
   person in every panel is given a concrete action** (rule 4) — never left
   standing/sitting with only an expression.
5. **Fill the metadata** (`type`, `id`, `title`, `lesson`, `characters` = every
   token used, `version`). The panel count is just `len(prompts)` — no
   `panel_count` field.

## Worked reference

See `imagegen/prompts/1_1.json` — a 6-panel life-lesson story
("Kindness Comes Back Around") that follows every rule here: protagonist left and
face-forward in all multi-person panels, supporting cast via tokens only,
consistent storybook style, identity preserved each panel. It also shows rule 9's
anchor reuse across a two-scene story — panels 1–4 repeat the verbatim clause
*"a tree-lined suburban pavement in the morning sunlight"*, then panels 5–6 switch
to *"a roadside bus stop with wet reflective pavement, rain falling"* (with only
the light shifting from grey to sun-breaking-through as the deliberate story beat).
`1_12.json` shows the same anchor held verbatim through a state change (a vase that
breaks then is cleaned up).

## Validate before done

- [ ] File is `imagegen/prompts/<type>_<id>.json`, valid JSON, schema per README.
- [ ] `len(prompts) == 6` (matches the render template `templates/1`).
- [ ] Every `{TOKEN}` used exists in `character.json` and is listed in
      `characters` (`python3 -c "import json …"` or grep to confirm).
- [ ] Re-run the per-panel checklist on each prompt.
- [ ] `type` matches the README registry (its display name lives in code,
      `sync_story_catalog._TYPE_NAMES`).

## Settings reference (informational)

The workflow controls sampler settings, not the prompt — but for context,
Qwen-Image-Edit-2511 runs best around **CFG/true_cfg ≈ 4.0–4.5** and
**~28–40 steps**. Don't put these in the prompt text.
