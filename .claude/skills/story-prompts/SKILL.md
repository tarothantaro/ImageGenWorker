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
9. **Keep each prompt self-contained — re-describe the shared scene every panel.**
   Because each panel is an independent edit with **no memory of any other panel**,
   re-establish the protagonist (left + face), the supporting cast tokens present,
   the style, **and the setting** every time. For panels that share a location,
   re-state the concrete **setting anchors** — the key furniture/landmarks, the
   time of day, and the light — in a short phrase (reasonable detail, not an
   exhaustive list) so the location reads as the same place across the story. Two
   failure modes to avoid, both caused by the no-memory pipeline:
   - **Bare back-references** — *"Same living room"*, *"the same garden"*, *"the
     broken vase"* — name something the model cannot see; they carry no
     description, so the setting drifts. Spell the anchors out instead: *"the same
     cozy living room — a wooden shelf on the wall, a wooden floor, warm afternoon
     light"*.
   - **"Transform the scene" openers.** The input image is the *person photo*, not
     the previous panel, so there is no prior scene to transform — the instruction
     acts on nothing and the intended setting is lost. Use *"Place the person from
     the input image into [the fully described scene] …"* and describe the scene
     from scratch, including any change of state (e.g. the now-shattered vase, the
     now-tidy room).
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
- [ ] Panels sharing a location **re-describe the setting anchors** (key
      furniture/landmarks + time of day + light); no bare "same room"
      back-reference and no "Transform the scene" opener.
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
consistent storybook style, identity preserved each panel.

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
