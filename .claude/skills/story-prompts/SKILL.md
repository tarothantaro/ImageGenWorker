---
name: story-prompts
description: Generate Qwen-Image-Edit-2511 story prompt sets for the image-gen worker. Use when asked to write, add, or edit a story's image prompts, create a new life-lesson story, or produce the per-panel prompts that put the input-photo person into a multi-image narrative. Writes imagegen/prompts/<type>_<id>.json. Pairs with the `character-config` skill.
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
gist refers to the supporting cast by **role**, never by `{TOKEN}` — it carries
no placeholders.

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

Let the model compose unless the beat needs placement: a specific exchange,
front/back depth, or a left-to-right row to prevent child duplication (rule 11).
Otherwise give each person a concrete action and expression and leave placement
open. When placement matters, use direct spatial words (`to the left`, `beside
them`, `in the foreground`, `in a single row`) and restate them in every panel
that needs them.

Let the protagonist engage naturally. Do not force a front-facing camera pose;
they can look at another character, the action, or the camera. Three-quarter,
profile, over-the-shoulder, and downward-glancing poses are fine. The only
identity floor is a visible face: avoid pure back-of-head shots. There is **no**
"face ≥70% visible" rule.

## Prompt-writing rules (Qwen-Image-Edit-2511)

Derived from the model's prompt guidance — instruction-style, specific, spatial.

1. **Lead with the scene, then introduce each person — once.** Open every panel
   with the canonical setting-anchor clause (rule 10) and keep **all people out of
   that clause**. Then introduce the protagonist **exactly once**, in a **single
   uninterrupted block** carrying position *and* action *and* facing-camera *and*
   expression together. Never name the person, insert the scene (or another
   character), then name them again: each panel is a memory-less edit, so two
   separated mentions of the same person read as **two different people** and the
   model renders a duplicate child. Short and specific beats long and flowery.
   Don't open with a command that places the person into a scene ahead of the
   setting, or with an instruction to transform an existing scene — the input is
   the person photo, not a prior panel, so there is no existing scene to transform.
   See Example 1.
2. **Use spatial words when you need to place people** — `to the left`,
   `to the right`, `beside them`, `in the background`, `foreground`, `in a row`.
   Reach for them only when the beat needs a specific arrangement (see
   "Composition & position"); otherwise let the model compose. The model
   positions by these words, not by image indices.
3. **Give every person an explicit action.** Each person in the panel — the
   protagonist **and** every supporting character — must be **doing something
   concrete**: a physical verb or a specific gesture/pose anchored to a prop, the
   other character, or their own body. **Never leave a person merely
   *standing / sitting + [expression]*** — an expression is
   *not* an action. With nothing to do, the model invents a pose for the idle
   body, and invented poses usually look weird (floating or clipping hands,
   awkward limbs, random gestures). State the expression *in addition to* the
   action, never instead of it. Even a solo panel with no prop or partner must
   give the hands/body a job rather than just being present. A bare verb of being
   present (`stands`, `sits`, `is in the frame`) does not count; physical actions,
   gestures, and prop handling do. Keep each gesture physically drawable in one
   still image: one pointing or indicating hand can aim at **one concrete target**
   only, not two separated places or objects (`points to the pot and the sky`,
   `points to the food bowl and the water bowl`). If the beat needs multiple
   things, choose the primary visible target for the gesture and mention the other
   object as nearby context, or give different people different single actions.
   Do not ask one person to perform two incompatible hand actions at once (`claps
   and points`).
   Because `character.json` is appearance-only, facial expression and mood must
   be authored here in the panel prompt. Every person block, including each
   supporting `{TOKEN}`, should include the expression that fits this specific
   beat (`with an anxious expression`, `smiling proudly`, `looking relieved`,
   etc.); do not rely on a character feature like "smile" to supply it.
4. **Make interactions physically connected — by naming the exact people and the
   exact shared thing.** When the gist is about people playing, helping, offering,
   receiving, teaching, comforting, or approaching, don't leave the people as
   parallel isolated figures: give the interaction a visible physical link where
   each person's gaze, hands, and body angle point to the one concrete thing that
   connects them. **Name who and what.** Write the connection from *this* panel's
   cast and prop — `the mother's pointing hand and the child's gaze both aim at
   the food bowl`, `the child's and the girl's hands both set blocks onto the one
   growing tower between them`. Never use a generic, reusable interaction
   sentence such as *"each person faces the other person or the shared object,
   with hands and gaze directed to one connected action."* A line that says
   **"each person", "the other person", "both people", "each character", "the
   others", or "the shared object"** without naming the actual people and object
   tells the model there are unspecified extra people to render, and it invents
   them (an extra child, a duplicate parent). Always substitute the role nouns of
   the people actually in this panel (the child, the mother, the boy, the two
   friends — refer to a supporting character by a role noun here, not a second
   `{TOKEN}`, per rule 5) and the specific object/action of this beat (the one
   red ball, the one clay pot, the joined hands, the single shared hug). Write
   each panel's connection line fresh; do not paste one canned sentence across
   panels. For a posed row or group (a celebratory line-up, a circle holding
   hands), describe the literal physical link of that pose (arms linked along the
   row, hands clasped into one ring) rather than a generic "facing each other".
   For greetings, introductions, and "saying hello" beats, avoid a front-facing
   row of people waving; it often reads as everyone waving at the camera instead
   of at each other. Compose them as an inward-facing pair or small semicircle,
   and state reciprocal direction explicitly: the protagonist's gaze, wave,
   shoulders, and feet point toward the named classmate(s), while the
   classmate(s)' gazes and waves point back toward the protagonist. If you need a
   count guard for a group, keep it, but do not let the count/row wording
   override the interaction.
5. **Reference the protagonist as "the person from the input image"** (consistent,
   unambiguous). Reference supporting cast **only** by their `{TOKEN}` placeholder
   from `character.json`, and use each character's `{TOKEN}` **at most once per
   prompt**. The token expands to that character's **entire appearance
   description**, so a second `{TOKEN}` for the same character in one prompt
   injects the whole description **twice** — bloating the prompt and pushing the
   model toward a duplicate person. When a character is mentioned more than once in
   a panel (e.g. the protagonist's action targets them and then they get their own
   action block), introduce the `{TOKEN}` **once** — normally in that character's
   own contiguous block — and refer to them elsewhere with a short role noun or
   pronoun (`a girl`, `the boy`, `a waiting parent`, `him`, `her`), never a second
   `{TOKEN}`. See Example 1.
6. **Pin identity at the end of every prompt with `{INPUT_IMAGE_IDENTITY}`.**
   This placeholder resolves through `character.json` to the shared instruction
   "Preserve the facial features, skin tone and hairstyle of the person from the
   input image." The edit + swap will otherwise drift the protagonist's look.
   Keep the prompt-ending text centralized in that placeholder; do not paste the
   literal sentence into story prompts.
7. **Never describe a generated character's fixed appearance.** Their age,
   ethnicity, build, hair and clothing live in `character.json` and arrive via
   the placeholder. In the prompt, give them only **position, action, and
   expression**. Re-describing
   appearance fights the resolved description and breaks consistency. (Exception:
   a deliberate, story-driven change added *after* the placeholder.)
8. **Keep style consistent across the story.** Pick one visual register in panel 1
   and repeat the same phrase in every panel so the set reads as one book.
9. **Keep each prompt self-contained — repeat one verbatim setting anchor per
   scene.** Each panel is an independent edit with **no memory of any other
   panel**, so re-establish the setting, style, protagonist, and supporting cast
   every time. For each distinct scene, write one canonical setting-anchor clause
   and paste it word-for-word into every panel in that scene, leading the prompt
   with it and keeping all people out of the anchor. The leading
   article/preposition may differ, but the anchor's adjectives, location,
   landmarks, time of day, and light must stay identical. New location means new
   anchor; returning location means reuse the earlier anchor. See Example 2.

   Layer story-driven changes after the fixed anchor instead of editing the
   anchor. If a changed state recurs, repeat that changed-state clause verbatim
   too. Keep light identical unless a lighting shift is the story beat.

   Describe broken/damaged props by positive, concrete fragment geometry: name
   the object once, say it is smashed/broken into pieces, spell out the fragment
   form, and add guards that no piece is whole/upright and no intact object is in
   frame. Avoid vague debris, wrong-shape negatives, and distinctive whole-object
   parts.

   Continuity comes from repeated description, not back-references. Do not use
   cross-panel reference words (`the same`, `again`, `back at`, `as before`, or
   continuity `now`), bare references to unseen things, or "Transform the scene"
   openers. Lead with the anchor and describe the scene from scratch. If a person
   is named inside or before the anchor and then named again later, the model may
   render a duplicate; keep each person in one contiguous block. See Example 1.
10. **Negatives sparingly.** This pipeline's prompt is positive-only; if a negative
   is supported, use it for artifacts, not concept changes.
11. **Guard tight close-ups and dense groups against child duplication.** A
   *separate* failure from the split reference (rule 10): even a single-block,
   scene-first prompt can make the **base model** invent an extra child — a
   prompt problem to fix here, not a render/setting artifact. Two
   patterns trigger it, with two fixes:
   - **Symmetric solo close-ups** (a child bent over a sink with both hands
     together, centered and mirrored) invent a companion. Frame the solo subject
     **upright, turned slightly to one side, hands/props off to one side**, and
     add a targeted one-child guard. A bare count alone does **not** reliably
     hold. See Example 3.
   - **Tight group hugs / crowds (3+ children)** fragment a `{TOKEN}` into two kids
     when a single token's resolved appearance details land on different
     children. Pose them in an **explicit left-to-right row**, naming each child
     once in order, with a total-count and no-duplicate guard, instead of an
     overlapping hug. See Example 3.
12. **End every prompt with an exact person-count guard.** Immediately before the
   closing `{INPUT_IMAGE_IDENTITY}` pin — and, for adventure stories, **after**
   the `{IMAGE_STYLE}.` clause — add one sentence stating the exact number of
   people in the panel, by category noun: the protagonist is always **one
   child**, plus each supporting `{TOKEN}` present mapped to its category noun
   (`boy`/`girl` for a child token, `man`/`woman` for an adult token, `elderly
   woman`/`elderly man` for an age-70 token, and a named cast member by their
   role's noun). Aggregate repeats (`two girls`). Write it verbatim as **`Exactly
   <list> in the frame, and no other people.`** — e.g. `Exactly one child and one
   man in the frame, and no other people.`, or for a solo panel `Exactly one
   child in the frame, and no other people.` The stated headcount must equal the
   cast the prompt actually names: **one protagonist + one per distinct `{TOKEN}`**
   (each token is counted once — rule 5 already forbids a repeated token). Animals
   and magical creatures (a glowing fox, a talking bird) are **not** people, are
   **not** counted, and the `no other people` clause does not remove them. This
   standing count guard is **mandatory on every panel**, on top of any targeted
   anti-twin guard from rule 11. `story-prompts-eval`'s linter FAILs a panel whose
   guard is missing, misplaced, or whose headcount disagrees with the named cast.

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
- [ ] **Every person** (protagonist + each supporting character) is given a
      **concrete action/gesture**, not just a placement and an expression.
- [ ] Every gesture has one drawable physical target/action: no pointing to two
      separated places or objects, and no simultaneous incompatible hand actions
      such as clapping and pointing by the same person.
- [ ] Every person block includes a scene-specific expression or mood cue; any
      supporting `{TOKEN}` gets only position/action/expression in this prompt,
      while fixed appearance stays in `character.json`.
- [ ] **Every interaction is physically connected**: shared props are one visible
      object between the people, handoffs/games name both sides of the action,
      and approach/invitation beats name the target person directly rather than
      only a bench, doorway, or general area.
- [ ] **The connection line names the exact people and the exact shared
      thing** — role nouns of the people in this panel plus the specific
      object/action of this beat. No generic, reusable template ("each person",
      "the other person", "both people", "each character", "the others", "the
      shared object"); a generic cast reference makes the model add unspecified
      extra people (rule 4).
- [ ] Supporting cast referenced **only** by `{TOKEN}` — placement/action/
      expression only, no appearance, no re-described clothing. Add the
      expression in the prompt for the current scene; never expect the
      `{TOKEN}`'s `features` to include a smile, frown, or mood.
- [ ] **Each character's `{TOKEN}` appears at most once in the prompt.** A
      character mentioned twice in one panel is introduced by `{TOKEN}` once and
      referred to elsewhere by a role noun/pronoun — never a second `{TOKEN}` (a
      repeat injects the whole appearance description twice).
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
- [ ] **Ends with the exact person-count guard** (rule 12): the last sentence
      before `{INPUT_IMAGE_IDENTITY}` is `Exactly <list> in the frame, and no other
      people.`, with the headcount = one protagonist + one per distinct `{TOKEN}`
      named in the panel.
- [ ] Ends with the preserve-identity sentence.

## Writing a story

1. **Confirm the story selector and panel count.** The user must provide the
   numeric `type` and the intended number of panels. If either is missing, ask
   for it before authoring prompts. Use the README story-type registry to verify
   the `type`; do not assume type `1` or any fixed story shape.
2. **Start from the gists.** The `story-text` skill has already written the
   `title`, `lesson`, and the per-panel `gists` (and `texts`). Read them — each
   gist is the beat your prompt must land. (If they don't exist yet, write them
   with `story-text` first.) The number of prompts you write must equal the user
   provided panel count and `len(gists)`.
3. **Decide the cast.** Protagonist = input person (implicit). Choose generated
   characters for the roles the gists name; ensure each token exists in
   `character.json` — if not, add it with the `character-config` skill *before*
   referencing it. **Default to race-free tokens** (`GENDER_F_AGE_70`,
   `GENDER_M_AGE_06`) so each job draws a random race; **only add `_RACE_<r>`**
   (`GENDER_F_AGE_70_RACE_ASIAN`) when the gist or the user explicitly calls for
   a specific race. Two same-demographic characters in one story stay distinct
   via a role suffix (`GENDER_M_AGE_06_FRIEND1` / `..._FRIEND2`).
4. **Plan the arc across the requested panel count.** Let the supplied story type
   and gists determine the structure: a life lesson may use a simple
   choice/consequence/resolution arc, while an adventure may need a
   quest/problem/rescue arc. **For adventure (type 2) stories, also apply the
   `story-prompts-adventure` sub-skill (`adventure/SKILL.md`)** — it inherits
   every rule here and adds the fixed first/second/last panel roles (origin →
   personal call → outcome tied back to the person's life) and the runtime
   `{IMAGE_STYLE}` placeholder. Keep location/time continuity unless the story
   moves on. List the story's distinct scenes up front and write **one canonical
   setting-anchor clause for each** (rule 10); every panel in a scene pastes that
   exact clause, so continuity is locked before you write the per-panel action.
5. **Write each panel** with the rules above. Alternate solo and multi-person
   panels naturally; **every person in every panel is given a concrete action**
   (rule 4) — never left standing/sitting with only an expression. State a
   person's position only when the beat needs it (see "Composition & position").
6. **Keep prompt ↔ gist aligned.** Re-read each gist and confirm your prompt
   instructs its setting, cast, and key action/beat. If a gist itself reads wrong,
   fix it via `story-text` — don't silently diverge from it.
7. **Leave `negative_prompts` empty by default.** The field is optional; omit it
   or set `"negative_prompts": []` unless evaluation keeps failing or the user
   explicitly asks for a targeted negative guard. If you do fill it, make it a
   per-panel array with the same length/order as `prompts`, and use it only for
   artifacts (for example unwanted extra wheels), not to rewrite the story beat.
8. **Fill `characters`** = every `{TOKEN}` used, plus the structural `type` / `id`
   / `version` if the file is new. The panel count is just `len(prompts)` — no
   `panel_count` field. For a new file, start from
   `.claude/skills/story-prompts/story_template.json` and expand each array to
   the requested panel count. (`title`, `lesson`, `gists`, and `texts` belong to
   the `story-text` skill.)

## Examples

These three examples cover the recurring prompt patterns: person blocks and
connected interactions; setting anchors with changed scene state; and duplication
guards for solo or crowded frames.

**Example 1: interaction with each person described once, and each `{TOKEN}` used once.**
Use a scene-first prompt where the protagonist's action, target, expression, and
gesture stay in one block, then the supporting character gets one separate block.
The supporting character's `{TOKEN}` appears **exactly once** (in their own
block); the protagonist's block refers to them by a short role noun (`a boy`):
*"In a sunny outdoor playground — a slide and swings, a wooden bench, soft grass,
warm sunlight — the {INPUT_1_AGE} person from the input image walks directly from
the open grass toward a boy seated by the wooden bench, body angled toward
him, with a friendly, encouraging smile and one hand raised in a clear
open-palm wave at shoulder height, elbow bent, the other hand relaxed at their
side. {GENDER_M_AGE_06} sits on the wooden bench facing the approaching child,
looking up hopefully, with both hands resting in his lap. Exactly one child and
one boy in the frame, and no other people. {INPUT_IMAGE_IDENTITY}"*
Note the two mandatory endings (rule 12 then rule 6): the exact person-count
guard, then the identity pin.
Do **not** write `{GENDER_M_AGE_06}` in both the protagonist's block and the
supporting character's block — the token expands to the full appearance string, so
a second copy injects the whole description twice and risks a duplicate child.
Do not put the supporting character's full placement in both blocks either. For
shared props, name one visible prop between the people and point both people's
gaze, hands, and body angle toward that same prop.

**Example 2: repeated anchor plus changed state.**
Choose one anchor per scene and paste it verbatim wherever that scene appears:
*"In a cozy living room in the afternoon — a wooden shelf on the wall, a wooden
floor, warm afternoon light — ..."*
When a prop changes, keep the anchor unchanged and add the changed-state clause
after it: *"the blue ceramic vase lies smashed to pieces on the wooden floor —
broken into thin-walled, curved, jagged-edged blue pottery fragments of various
sizes, all lying scattered on the floor with no piece whole or upright (no intact
or standing vase anywhere in frame)."*
Repeat that changed-state clause verbatim in every panel where the change is
visible. Do not use cross-panel references such as `the same`, `again`, `back at`,
or `now` for continuity.

**Example 3: duplication guards.**
For a solo close-up that risks a duplicate child, use an asymmetric pose plus a
targeted guard, then the canonical rule-12 count guard and the identity pin:
*"the {INPUT_1_AGE} person from the input image stands upright,
turned slightly to one side, holding the soap bottle off to their right with a
focused expression; only this one child is in the frame, alone, with no second
child, twin, sibling, or reflection. Exactly one child in the frame, and no other
people. {INPUT_IMAGE_IDENTITY}"*
For three or more children, avoid overlapping hugs. Use a left-to-right row and
name each child once in order, and still close with the rule-12 count guard:
*"From left to right: first the {INPUT_1_AGE}
person from the input image, then {GENDER_F_AGE_06}, then {GENDER_M_AGE_06}.
Each a single distinct child — no fourth child, no duplicate or extra child, no
twin. Exactly one child, one girl, and one boy in the frame, and no other people.
{INPUT_IMAGE_IDENTITY}"*
The targeted rule-11 guard and the standing rule-12 count guard stack: keep both.

## Validate before done

- [ ] File is `imagegen/prompts/<type>_<id>.json`, valid JSON, schema per README.
- [ ] User supplied `type` and panel count; `type` matches the README registry.
- [ ] `len(prompts)` matches the user-provided panel count and the render
      template being used.
- [ ] `negative_prompts` is omitted/`[]` unless explicitly needed; if non-empty,
      `len(negative_prompts) == len(prompts)` and each entry targets only a
      persistent artifact.
- [ ] `len(gists) == len(prompts)` (the `gists` come from `story-text`); each
      prompt instructs the same beat as its gist (setting + cast/placement +
      action + point). Run `story-prompts-eval` to grade prompt↔gist alignment +
      rule compliance before generating any images.
- [ ] Every `{TOKEN}` used exists in `character.json` and is listed in
      `characters` (`python3 -c "import json …"` or grep to confirm).
- [ ] Every prompt ends with the exact person-count guard (rule 12) right before
      `{INPUT_IMAGE_IDENTITY}`, and its headcount matches the named cast (one
      protagonist + one per distinct `{TOKEN}`). `story-prompts-eval`'s linter
      enforces this.
- [ ] Re-run the per-panel checklist on each prompt.

## Settings reference (informational)

The workflow controls sampler settings, not the prompt — but for context,
Qwen-Image-Edit-2511 runs best around **CFG/true_cfg ≈ 4.0–4.5** and
**~28–40 steps**. Don't put these in the prompt text.
