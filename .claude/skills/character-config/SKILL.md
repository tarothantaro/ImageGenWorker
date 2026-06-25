---
name: character-config
description: Create and update imagegen/prompts/character.json — the modular library of generated supporting characters that story prompts reference as {TOKEN} placeholders (e.g. {GENDER_M_AGE_30_RACE_ASIAN}). Use when asked to add a character, change a character's look/dress, add a wardrobe/hair/age/ethnicity option, or otherwise edit the character config used by the `story-prompts` skill.
---

# character-config

Own `imagegen/prompts/character.json`: the library of **fully-generated supporting
characters** that story prompts conjure via `{TOKEN}` placeholders, plus shared
non-character prompt snippets such as `{INPUT_IMAGE_IDENTITY}`. The input-photo
protagonist is **never** defined here — only the conjured cast and reusable
prompt text.
Read `imagegen/prompts/README.md` and the current `character.json` first; this
skill maintains the structure that the `story-prompts` skill consumes.

## What the file is for

A story prompt says `{GENDER_F_AGE_70_RACE_ASIAN} stands on the right …`. At
runtime the placeholder is replaced by that character's `description` string, so
the **look and dress stay byte-for-byte identical across every panel** of a story
(panels are independent edits — see the `story-prompts` skill — so a shared,
centralized description is the *only* thing keeping a character consistent).
The special `{INPUT_IMAGE_IDENTITY}` placeholder is not a character; it resolves
to the shared identity-preservation instruction that belongs at the end of every
story prompt.

**Runtime contract:** strip the `{}`, look up `characters[<TOKEN>].description`,
string-replace it into the prompt — the same mechanism `workflow.py` uses for
`USER_ID` / `STORY_ID`. For random fallback tokens with no enumerated
description, runtime also reads `dimensions`, `hair_by_gender`, `restrictions`,
and the fragment tables needed to compose a look.

## Structure (modular by design)

```
schema_version    file version
runtime           the placeholder/replace contract (doc only)
_recipe           how a `description` is composed (render_order + template + rules)
dimensions        gender / age / race  — the tokens encoded in a placeholder NAME
                  (each gender/age value carries an `avoid` list, see below)
hair build wardrobe features   reusable fragment libraries (the modular parts)
hair_by_gender   gender-keyed hair candidate lists used by runtime fallback
restrictions      fragment keys reserved to a demographic group (child_only /
                  adult_only / elderly_only / masc_only / fem_only)
characters        TOKEN -> { refs: {...}, description: "<compiled string>" }
                  or a documented special prompt-snippet token with description only
```

### `hair_by_gender` + `restrictions` + `avoid` (runtime random fallback)

When a `GENDER_..._AGE_...` token (with or without the optional `_RACE_...`) has
**no** `characters` entry, `workflow.py` composes a look on the fly: it picks the
hair/build/wardrobe/features fragments **at random**, plus a **random race** when
the token omits `_RACE_`. Three fields keep the look draw plausible:

- `hair_by_gender.<g>` is the first filter for hair, keyed by the token's
  `GENDER_<g>` value. Keep male options short/cropped because the model is
  unreliable on long-haired men; keep female options longer/tied because it is
  unreliable on short-haired women. Non-binary options should be neutral.
- `restrictions.<group>.<table>` lists the fragment keys reserved to a
  demographic group — `child_only`, `adult_only`, `elderly_only`, `masc_only`,
  `fem_only`.
- each `dimensions.gender[g]` and `dimensions.age[a]` value carries an `avoid`
  list naming the groups that demographic must skip (`M` avoids `fem_only`, `F`
  avoids `masc_only`, `NB` avoids both). **Age is a three-way band, not binary:**
  children (06–16) avoid `adult_only` *and* `elderly_only`; middle adults
  (25–45) avoid `child_only` *and* `elderly_only`; only the elderly (60, 70)
  avoid just `child_only`, so they alone unlock `elderly_only` looks (grey/
  thinning hair, fine wrinkles) on top of `adult_only`.

At compose time hair is first limited to the token gender's `hair_by_gender`
list, then the draw drops every key reserved to any group the character avoids.
So a child never rolls a business suit/stubble, a 25-year-old never a grey bun,
a man never a dress/pigtails, a non-binary character neither. A key listed under
**no** group suits everyone; a key may sit in **several** groups (a beard is
`masc_only` *and* `adult_only`; thinning grey hair is `masc_only` *and*
`elderly_only`). If a whole table is filtered away, the restriction is ignored
for that table rather than yielding nothing. When you add a fragment whose look
is clearly age- or gender-specific, list it under the matching group(s);
otherwise leave it out (it stays neutral and is eligible for everyone).

Modularity = the fragment libraries (`hair`, `build`, `wardrobe`, `features`) and
`dimensions` are **shared building blocks**. A character is assembled by
*referencing* them in `refs`, then the assembled string is cached in
`description`. Edit a fragment once → regenerate every `description` that
references it → every character (and every story) updates consistently.

## Token naming convention

`GENDER_<g>_AGE_<a>` with an **optional** `_RACE_<r>`, where `<g>`∈
`dimensions.gender`, `<a>`∈`dimensions.age`, `<r>`∈`dimensions.race`.

**Race is opt-in — do NOT add `_RACE_<r>` unless the story or the user explicitly
asks for a specific race.** A token that omits race (e.g. `GENDER_F_AGE_70`) has
a race **drawn at random** from `dimensions.race` per job (a fresh, plausible
race each time, identical across that job's panels). Add `_RACE_<r>` (e.g.
`GENDER_M_AGE_30_RACE_ASIAN`) only to **pin** an exact race.

Need two distinct characters of the same demographic in one story? Append a role
suffix to keep them separate — `GENDER_M_AGE_30_MENTOR`, or with a pinned race
`GENDER_M_AGE_30_RACE_ASIAN_MENTOR`. The suffix is ignored at compose time; it
only keeps the tokens distinct. The `characters` key is matched **exactly**; the
prefix is a human-readable convention, not a parser requirement.

## The composition recipe (single source of truth = `_recipe`)

`render_order`: `age → ethnicity → gender_noun → hair → build → wardrobe → features`

`template`:
```
{age} {ethnicity} {gender_noun} with {hair}, {build}, wearing {wardrobe}, with {features}
```

Rules:
- `gender_noun` = `dimensions.gender[g].noun_child` when `dimensions.age[a].child`
  is true, else `.noun`.
- Articles are baked into `dimensions.age[a].phrase` (`"an 8-year-old"`,
  `"a 30-year-old"`, `"an elderly"`).
- Omit the trailing `, with {features}` entirely when there is no `features` ref.
- **Appearance-only.** Never bake actions, expressions, poses, or positions into
  a `description` — those belong in the per-panel story prompt. The description is
  what the character *is*, not what they're *doing*.
- `features` fragments must be fixed physical traits only. Do not add smiles,
  frowns, tears, moods, gaze direction, or expression-coded wording such as
  "friendly", "kind", "warm", "stern", "gentle", "happy", or "sad" to
  `features`; put the scene-specific expression after the `{TOKEN}` in each
  story prompt.

Worked: `refs {gender:M, age:35, race:WHITE, hair:SHORT_BROWN,
build:AVERAGE_MEDIUM, wardrobe:BUSINESS_GREY_SUIT, features:STUBBLE}` →
`"a 35-year-old White man with short brown hair, an average build and medium
height, wearing a charcoal-grey business suit with a white shirt, with light
stubble"`.

## To add or change a character

1. **Pick the token** per the naming convention.
2. **Choose fragments** for hair / build / wardrobe / (optional) features. Reuse
   an existing library key where one fits; only add a new fragment if none does.
3. **Add any missing fragment** to the relevant library (`hair`, `wardrobe`, …)
   or `dimensions` value — give it a clear `UPPER_SNAKE` key and a concise,
   appearance-only phrase.
4. **Write the `characters` entry**: `refs` (the keys you chose) **and**
   `description` (compiled by the recipe, exactly). The two must agree.
5. **If you changed a shared fragment**, regenerate the `description` of **every**
   character whose `refs` point at it — otherwise the cache drifts from the parts.

## Consistency & hygiene rules

- **One token, one look.** A character that recurs across panels/stories uses the
  same token everywhere; the appearance is defined here once. Never re-describe a
  character's clothing/hair in a story prompt — that fights this file.
- `refs` and `description` must stay in sync — `description` is generated from
  `refs` via the recipe, not hand-diverged.
- Exception: special non-character prompt-snippet tokens such as
  `INPUT_IMAGE_IDENTITY` may omit `refs`; keep their `description` as the exact
  reusable prompt text that story prompts should reference.
- Keep fragments reusable and atomic (one hairstyle, one outfit, one fixed
  physical feature) so they compose. Facial features such as dimples, freckles,
  cheek shape, wrinkles, teeth spacing, glasses, and facial hair are fine;
  expressions such as smiles/frowns or mood-coded adjectives are not.
- Don't define the input-photo protagonist here. This file is the *generated*
  cast only.

## Validate before done

- [ ] `character.json` is valid JSON
      (`python3 -c "import json;json.load(open('imagegen/prompts/character.json'))"`).
- [ ] Every generated-character `characters[*].refs` value resolves to an
      existing key in its library / dimension.
- [ ] Every generated-character `characters[*].description` is exactly what the
      recipe produces from its `refs` (recompose and compare). Special
      prompt-snippet tokens such as `INPUT_IMAGE_IDENTITY` may omit `refs`.
- [ ] No description or fragment contains an action/expression/position
      (appearance-only); `features` contains only fixed physical traits.
- [ ] Any token already referenced by a story still exists (grep the story files
      under `imagegen/prompts/` for `{` placeholders before renaming/removing).
