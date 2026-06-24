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
narrative spine of the file — `title`, `lesson`, the per-panel `gists`, and the
read-aloud `texts` — is owned by the `story-text` skill, which runs **first**;
this skill writes the image `prompts` and the `characters` list from those gists.

The **gist** is your spec. Authored by `story-text` (the JSON `gists` array,
parallel to `prompts`), each is a single sentence capturing what *that panel must
show*: its setting, who is present, the key action/interaction, and the narrative
beat — with style, camera and identity-preservation boilerplate stripped out.
**Your job is to write a prompt that, rendered faithfully, satisfies its gist.**
The gist is the spec both eval skills grade against: `image-eval` (vision) checks
"does the image satisfy the gist?" and `story-prompts-eval` (text-only, no image
gen) checks "would this prompt, rendered faithfully, satisfy the gist?". Keep
prompt and gist in sync — if a prompt must diverge from its gist to render well,
fix the gist via `story-text` so the beat stays the single source of truth. The
gist refers to the supporting cast by **role** ("the elderly woman", "a friend"),
never by `{TOKEN}` — it carries no placeholders (see `imagegen/prompts/1_1.json`).

## How the pipeline shapes every prompt

This is not generic text-to-image. The worker (DESIGN.md §7.2) runs **one input
photo** through a Qwen-Image-Edit-2511 image-edit graph, once per panel:

- There is exactly **one input image** — the user's photo. The person in it is
  the **protagonist**. Every other character is *conjured by the prompt text*,
  not supplied as a second image.
- The edit transfers the input person's identity into the panel. It needs the
  protagonist's **face visible** to carry that identity, but the position is
  otherwise free (see "Composition & position" below).
- Panels do **not** share generated pixels. Each panel is an independent edit
  from the same photo. So cross-panel consistency of the supporting cast comes
  **only** from reusing the identical `{TOKEN}` placeholder (which expands to the
  identical appearance string) — never from one panel "remembering" another.

## Composition & position

**Let the model compose the scene. Mention a person's position only when the
narrative beat needs it** — an exchange that reads a particular way across the
frame, who is in front vs behind, or an explicit left-to-right row that keeps a
group of children from fusing (rule 12). Otherwise give each person a concrete
action and an expression and let the model place them; do **not** pin the
protagonist (or anyone) to a fixed spot out of habit. When you *do* need
placement, spatial words are the lever (rule 2): *to the left*, *beside them*,
*in the foreground*, *in a single row*.

Whatever position you state must be **restated in every panel that needs it** —
panels are independent edits with no memory of each other, and the base model has
strong built-in preferences, so an unstated cue will be ignored.

**Let the protagonist engage naturally.** Do *not* force the input person to face
the camera or keep the face front-on — that mandate produced stiff, posed-looking
compositions and unnatural interactions. Let them look at the other character, at
the action, or at the camera, whatever the moment calls for; three-quarter,
profile, over-the-shoulder, and downward-glancing poses are all welcome. The only
floor is identity: don't bury the protagonist in a pure back-of-head shot where no
face is visible, or the edit can't carry the input face. There is **no** "face
≥70% visible" rule.

## Prompt-writing rules (Qwen-Image-Edit-2511)

Derived from the model's prompt guidance — instruction-style, specific, spatial.

1. **Lead with the scene, then introduce each person — once.** Open every panel
   with the canonical setting-anchor clause (rule 10) and keep **all people out of
   that clause**: *"In a cozy living room — a wooden shelf, a wooden floor, warm
   afternoon light — the person from the input image …"*. Then introduce the
   protagonist **exactly once**, in a **single uninterrupted block** carrying
   position *and* action *and* facing-camera *and* expression together. Never name
   the person, insert the scene (or another character), then name them again
   (*"Place the person into `<scene>` … the person smiling …"*): each panel
   is a memory-less edit, so two separated mentions of the same person read as **two
   different people** and the model renders a duplicate child (see rule 10, "split
   reference"). Short and specific beats long and flowery — *"a torn grocery bag,
   oranges rolling out"* over a paragraph of adjectives. Don't open with *"Place the
   person into …"* ahead of the setting, or with *"Transform the scene"* — the input
   is the person photo, not a prior panel, so there is no existing scene to transform.
2. **Use spatial words when you need to place people** — `to the left`,
   `to the right`, `beside them`, `in the background`, `foreground`, `in a row`.
   Reach for them only when the beat needs a specific arrangement (see
   "Composition & position"); otherwise let the model compose. The model
   positions by these words, not by image indices.
3. **Name a camera/shot to control framing** — `medium shot`,
   `medium-wide shot`, `eye-level`, `three-quarter view`. Sets how much of the
   scene and the people the frame includes, and the viewing angle. The model
   obeys camera/angle cues reliably.
4. **Give every person an explicit action.** Each person in the panel — the
   protagonist **and** every supporting character — must be **doing something
   concrete**: a physical verb or a specific gesture/pose anchored to a prop, the
   other character, or their own body (*"kneeling to gather the oranges"*,
   *"holding out a block"*, *"one hand pressed to their chest"*, *"wrapping their
   arms around themselves against the cold"*). **Never leave a person merely
   *standing / sitting + [expression]*** — an expression is
   *not* an action. With nothing to do, the model invents a pose for the idle
   body, and invented poses usually look weird (floating or clipping hands,
   awkward limbs, random gestures). State the expression *in addition to* the
   action, never instead of it. Even a solo panel with no prop or partner must
   give the hands/body a job (*"hugging their teddy bear"*, *"hands on hips,
   looking around"*, *"shielding their head from the rain"*) rather than just
   "standing there". A bare verb of being present (`stands`, `sits`, `is in the
   frame`) does not count; `kneeling`, `walking`, `holding`, `reaching`,
   `clapping`, `pointing`, `hands on hips` do.
   Because `character.json` is appearance-only, facial expression and mood must
   be authored here in the panel prompt. Every person block, including each
   supporting `{TOKEN}`, should include the expression that fits this specific
   beat (`with an anxious expression`, `smiling proudly`, `looking relieved`,
   etc.); do not rely on a character feature like "smile" to supply it.
5. **Make interactions physically connected.** When the gist is about people
   playing, helping, offering, receiving, teaching, comforting, or approaching,
   don't leave the people as parallel isolated figures. Give the interaction a
   visible physical link: each person's gaze, hands, and body angle should point
   toward the other person or the shared object. For handoffs and games, say
   there is **one shared prop** in the space between them (*"one colourful ball
   through the air between them"*, *"the friend reaches both hands toward the
   single ball"*, *"one block held between the child's offering hand and the
   friend's reaching hand"*). For approach/invitation beats, name the target
   character directly (*"walks directly toward {TOKEN} seated on the bench, body
   angled toward him"*), not just the furniture or general area. This avoids the
   model rendering two separate props or two unrelated children who happen to be
   near each other.
   For greetings, introductions, and "saying hello" beats, avoid a front-facing
   row of people waving; it often reads as everyone waving at the camera instead
   of at each other. Compose them as an inward-facing pair or small semicircle,
   and state reciprocal direction explicitly: the protagonist's gaze, wave,
   shoulders, and feet point toward the named classmate(s), while the
   classmate(s)' gazes and waves point back toward the protagonist. If you need a
   count guard for a group, keep it, but do not let the count/row wording
   override the interaction.
6. **Reference the protagonist as "the person from the input image"** (consistent,
   unambiguous). Reference supporting cast **only** by their `{TOKEN}` placeholder
   from `character.json`.
7. **Pin identity at the end of every prompt with `{INPUT_IMAGE_IDENTITY}`.**
   This placeholder resolves through `character.json` to the shared instruction
   "Preserve the facial features, skin tone and hairstyle of the person from the
   input image." The edit + swap will otherwise drift the protagonist's look.
   Keep the prompt-ending text centralized in that placeholder; do not paste the
   literal sentence into story prompts.
8. **Never describe a generated character's fixed appearance.** Their age,
   ethnicity, build, hair and clothing live in `character.json` and arrive via
   the placeholder. In the prompt, give them only **position, action, and
   expression** (`stands to the right, watching gratefully`). Re-describing
   appearance fights the resolved description and breaks consistency. (Exception:
   a deliberate, story-driven change — e.g. *"… now wearing a raincoat over their
   usual clothes"* — added *after* the placeholder.)
9. **Keep style consistent across the story.** Pick one visual register in panel 1
   (e.g. *"soft storybook illustration style"*) and repeat the same phrase in
   every panel so the set reads as one book.
10. **Keep each prompt self-contained — repeat one verbatim setting anchor per
   scene.** Because each panel is an independent edit with **no memory of any
   other panel**, re-establish the protagonist, the supporting cast
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
   scene, **leading the panel with it so no person sits inside the anchor** —
   *"In `<anchor>` — the person from the input image …"* in every panel, establishing
   or not (the leading article/preposition may differ, *"In a …"* / *"On a …"* /
   *"At a …"*, but the adjectives/nouns/landmarks/light inside the anchor must be
   identical). When the story moves to a new location, that is a **new scene** with its own
   canonical anchor; if it later returns to an earlier scene, reuse that scene's
   exact anchor again.

   **Layer story-driven change as separate clauses on top of the fixed anchor —
   never by editing the anchor.** A vase that breaks, a room that gets tidied, a
   sandcastle that grows, a night-light that switches on: describe the changed
   state in its own clause after the anchor, and when that changed state recurs
   across panels, word *it* identically too (e.g. *"the blue ceramic vase lies
   smashed to pieces on the wooden floor — broken into thin-walled, curved,
   jagged-edged blue pottery fragments of various sizes, all lying scattered on
   the floor with no piece whole or upright (no intact or standing vase anywhere
   in frame)"* repeated verbatim in every post-break panel). **Light** stays
   identical across the scene as well, except where a lighting shift is itself a
   deliberate story beat (gloom → sun breaking through; a single golden-hour
   resolution) — keep that the only varying token, worded consistently.

   **Render a broken/damaged object as what it physically *is*, by its real
   fragment geometry — not as "small scattered pieces".** Vague debris wording
   backfires in two ways, both seen on the broken-vase panel:
   - *"small scattered shards lying flat"* / *"pieces no larger than a coin"* →
     the model paints **uniform little flat discs that read as toy plates or
     coasters**, not a smashed vase.
   - *"some larger curved shards"* (size-only, no clear parent shape) → a "larger
     curved" piece renders as an **intact bowl or cup**, leaving a whole vessel in
     a frame that is supposed to show only debris.

   Fix both by describing the fragments **positively, with concrete broken
   geometry**: name the object once, say it is *smashed/broken into pieces*, and
   spell out the shard form — *"broken into thin-walled, curved, jagged-edged blue
   pottery fragments of various sizes, all lying scattered on the floor"*. Add
   *"with no piece whole or upright"* to kill the intact-vessel render, plus the
   "no intact/standing vase in frame" guard. Two more tripwires, both observed:
   - **Do not name the wrong shape even inside a negative.** *"not flat round
     plates or discs"* tends to *summon* plates in Qwen (it keys on the nouns, not
     the "not"); steer with the positive shard description instead.
   - **Do not name a distinctive *whole-object part*** (*"the tall vase"*, *"the
     snapped-off vase neck"*). A vase's neck/silhouette is its most recognizable
     feature, so naming it makes the model draw the **whole intact vase standing
     upright** beside the debris — exactly the thing the panel must not contain.
     Describe the pieces only as generic curved/jagged *fragments*, never as
     named anatomical parts of the object.

   The same logic applies to any broken prop (a cracked plate, a snapped crayon,
   torn paper): describe the real broken form as generic fragments, not "small
   bits" and not by the object's signature whole-part.

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
     acts on nothing and the intended setting is lost. Lead with the anchor and
     describe the scene from scratch, including any change of state (e.g. the
     shattered vase, the tidied room).
   - **A person inside the anchor, named again later (split reference).** Writing
     *"Place the person into `<anchor>` … `<action>`. … the person smiling
     …"* names the protagonist, drops the scene (or another character) in between,
     then names them again. With no memory across the sentence the model treats the
     two mentions as **two separate children** and renders a duplicate. Lead with the
     anchor (no person in it), then describe each person **once**, in a single
     contiguous block (action + expression, plus position only if the beat needs
     it): *"In `<anchor>` — the person from the input image kneels, gathering the
     blocks with a kind smile."*
11. **Negatives sparingly.** This pipeline's prompt is positive-only; if a negative
   is supported, use it for artifacts (*"no extra fingers"*), not concept changes.
12. **Guard tight close-ups and dense groups against child duplication.** A
   *separate* failure from the split reference (rule 10): even a single-block,
   scene-first prompt can make the **base model** invent an extra child — a
   prompt problem to fix here, not a render/setting artifact. Two
   patterns trigger it, with two fixes:
   - **Symmetric solo close-ups** (a child bent over a sink with both hands
     together, centered and mirrored) invent a companion. Frame the solo subject
     **upright, turned slightly to one side, hands/props off to one side** (the
     pose that already works elsewhere), and add a targeted *"only this one child,
     alone in the frame, no second child, twin, sibling or reflection"*. A bare
     *"Exactly one child"* count alone does **not** reliably hold.
   - **Tight group hugs / crowds (3+ children)** fragment a `{TOKEN}` into two kids
     (e.g. the "red dungarees" and the "puff buns" of one girl land on different
     children). Pose them in an **explicit left-to-right row**, naming each child
     once in order, with *"exactly N children in total, each a single distinct
     child — no extra, duplicate or twin"*, instead of an overlapping hug.

## Per-panel checklist

For every prompt in the array, confirm:

- [ ] **Opens with the scene** (the canonical setting-anchor clause), with **no
      person named inside that clause**.
- [ ] **Each person is described exactly once**, in one contiguous block (action +
      expression, plus position only when the beat needs it) — the protagonist is
      never named near the scene and then again later (a "split reference" makes the
      model draw a duplicate child).
- [ ] **Position is stated only when the beat needs it** (an exchange, front/back
      depth, or an explicit left-to-right row to stop child duplication); otherwise
      each person is given an action and the model composes the placement.
- [ ] A **camera/shot cue** is present (`medium shot`, `eye-level`, `three-quarter
      view`, …) to set framing — the protagonist engages naturally and is **not**
      forced to face the camera.
- [ ] **Every person** (protagonist + each supporting character) is given a
      **concrete action/gesture**, not just a placement and an expression.
- [ ] Every person block includes a scene-specific expression or mood cue; any
      supporting `{TOKEN}` gets only position/action/expression in this prompt,
      while fixed appearance stays in `character.json`.
- [ ] **Every interaction is physically connected**: shared props are one visible
      object between the people, handoffs/games name both sides of the action,
      and approach/invitation beats name the target person directly rather than
      only a bench, doorway, or general area.
- [ ] Supporting cast referenced **only** by `{TOKEN}` — placement/action/
      expression only, no appearance, no re-described clothing. Add the
      expression in the prompt for the current scene; never expect the
      `{TOKEN}`'s `features` to include a smile, frown, or mood.
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

1. **Start from the gists.** The `story-text` skill has already written the
   `title`, `lesson`, and the per-panel `gists` (and `texts`). Read them — each
   gist is the beat your prompt must land. (If they don't exist yet, write them
   with `story-text` first.)
2. **Decide the cast.** Protagonist = input person (implicit). Choose generated
   characters for the roles the gists name; ensure each token exists in
   `character.json` — if not, add it with the `character-config` skill *before*
   referencing it. **Default to race-free tokens** (`GENDER_F_AGE_70`,
   `GENDER_M_AGE_06`) so each job draws a random race; **only add `_RACE_<r>`**
   (`GENDER_F_AGE_70_RACE_ASIAN`) when the gist or the user explicitly calls for
   a specific race. Two same-demographic characters in one story stay distinct
   via a role suffix (`GENDER_M_AGE_06_FRIEND1` / `..._FRIEND2`).
3. **Plan the arc across 6 panels** (the render template `templates/1` has 6
   panels, so a story has exactly 6 prompts). A clean life-lesson arc:
   *establish → encounter/choice → action → consequence → turn → resolution that
   lands the lesson.* Keep location/time continuity unless the story moves on.
   List the story's distinct scenes up front and write **one canonical
   setting-anchor clause for each** (rule 10); every panel in a scene pastes that
   exact clause, so continuity is locked before you write the per-panel action.
4. **Write each panel** with the rules above. Alternate solo and multi-person
   panels naturally; **every person in every panel is given a concrete action**
   (rule 4) — never left standing/sitting with only an expression. State a
   person's position only when the beat needs it (see "Composition & position").
5. **Keep prompt ↔ gist aligned.** Re-read each gist and confirm your prompt
   instructs its setting, cast, and key action/beat. If a gist itself reads wrong,
   fix it via `story-text` — don't silently diverge from it.
6. **Fill `characters`** = every `{TOKEN}` used, plus the structural `type` / `id`
   / `version` if the file is new. The panel count is just `len(prompts)` — no
   `panel_count` field. (`title`, `lesson`, `gists`, and `texts` belong to the
   `story-text` skill.)

## Worked reference

See `imagegen/prompts/1_1.json` — a 6-panel life-lesson story
("Kindness Comes Back Around") that follows every rule here: people composed
naturally (position called out only where the beat needs it), supporting cast via
tokens only, consistent storybook style, identity preserved each panel. Every
panel **leads with
the scene** and names the protagonist exactly once. It also shows rule 10's
anchor reuse across a two-scene story — panels 1–4 repeat the verbatim clause
*"a tree-lined suburban pavement in the morning sunlight"*, then panels 5–6 switch
to *"a roadside bus stop with wet reflective pavement, rain falling"* (with only
the light shifting from grey to sun-breaking-through as the deliberate story beat).
`1_12.json` shows the same anchor held verbatim through a state change (a vase that
breaks then is cleaned up).

## Validate before done

- [ ] File is `imagegen/prompts/<type>_<id>.json`, valid JSON, schema per README.
- [ ] `len(prompts) == 6` (matches the render template `templates/1`).
- [ ] `len(gists) == len(prompts)` (the `gists` come from `story-text`); each
      prompt instructs the same beat as its gist (setting + cast/placement +
      action + point). Run `story-prompts-eval` to grade prompt↔gist alignment +
      rule compliance before generating any images.
- [ ] Every `{TOKEN}` used exists in `character.json` and is listed in
      `characters` (`python3 -c "import json …"` or grep to confirm).
- [ ] Re-run the per-panel checklist on each prompt.
- [ ] `type` matches the README registry (its display name lives in code,
      `sync_story_catalog._TYPE_NAMES`).

## Settings reference (informational)

The workflow controls sampler settings, not the prompt — but for context,
Qwen-Image-Edit-2511 runs best around **CFG/true_cfg ≈ 4.0–4.5** and
**~28–40 steps**. Don't put these in the prompt text.
