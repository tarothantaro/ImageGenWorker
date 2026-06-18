---
name: character-config
description: Create and update imagegen/prompts/character.json — the modular library of generated supporting characters that story prompts reference as {TOKEN} placeholders (e.g. {GENDER_M_AGE_30_RACE_ASIAN}). Use when asked to add a character, change a character's look/dress, add a wardrobe/hair/age/ethnicity option, or otherwise edit the character config used by the `story-prompts` skill.
---

# character-config

Own `imagegen/prompts/character.json`: the library of **fully-generated supporting
characters** that story prompts conjure via `{TOKEN}` placeholders. The
input-photo protagonist is **never** defined here — only the conjured cast.
Read `imagegen/prompts/README.md` and the current `character.json` first; this
skill maintains the structure that the `story-prompts` skill consumes.

## What the file is for

A story prompt says `{GENDER_F_AGE_70_RACE_ASIAN} stands on the right …`. At
runtime the placeholder is replaced by that character's `description` string, so
the **look and dress stay byte-for-byte identical across every panel** of a story
(panels are independent edits — see the `story-prompts` skill — so a shared,
centralized description is the *only* thing keeping a character consistent).

**Runtime contract (keep it this simple):** strip the `{}`, look up
`characters[<TOKEN>].description`, string-replace it into the prompt — the same
mechanism `workflow.py` uses for `USER_ID` / `STORY_ID`. `description` is the
**only** field read at runtime. Everything else is authoring metadata.

## Structure (modular by design)

```
schema_version    file version
runtime           the placeholder/replace contract (doc only)
_recipe           how a `description` is composed (render_order + template + rules)
dimensions        gender / age / race  — the tokens encoded in a placeholder NAME
hair build wardrobe features   reusable fragment libraries (the modular parts)
age_restrictions  fragment keys reserved to child_only / adult_only age groups
characters        TOKEN -> { refs: {...}, description: "<compiled string>" }
```

### `age_restrictions` (consumed at runtime by the random fallback)

When a `GENDER_..._AGE_..._RACE_...` token has **no** `characters` entry,
`workflow.py` composes a look on the fly and picks the hair/build/wardrobe/
features fragments **at random**. `age_restrictions.child_only` /
`adult_only` list the fragment keys reserved to one age group so that draw stays
plausible — a child (`dimensions.age[a].child == true`) never rolls an
`adult_only` fragment (a business suit, stubble, a grey bun) and an adult never
rolls a `child_only` one (a school uniform, pigtails). A key listed under
**neither** group suits both ages. This is the one runtime-read field besides
`characters[*].description`. When you add a fragment whose look is clearly child-
or adult-specific, list it here too; otherwise leave it out (it stays age-neutral).

Modularity = the fragment libraries (`hair`, `build`, `wardrobe`, `features`) and
`dimensions` are **shared building blocks**. A character is assembled by
*referencing* them in `refs`, then the assembled string is cached in
`description`. Edit a fragment once → regenerate every `description` that
references it → every character (and every story) updates consistently.

## Token naming convention

`GENDER_<g>_AGE_<a>_RACE_<r>` where `<g>`∈`dimensions.gender`,
`<a>`∈`dimensions.age`, `<r>`∈`dimensions.race`
(e.g. `GENDER_M_AGE_30_RACE_ASIAN`). Need two distinct characters of the same
demographic in one story? Append a role suffix to keep them separate, e.g.
`GENDER_M_AGE_30_RACE_ASIAN_MENTOR`. The `characters` key is matched **exactly**;
the prefix is a human-readable convention, not a parser requirement.

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
- Keep fragments reusable and atomic (one hairstyle, one outfit) so they compose.
- Don't define the input-photo protagonist here. This file is the *generated*
  cast only.

## Validate before done

- [ ] `character.json` is valid JSON
      (`python3 -c "import json;json.load(open('imagegen/prompts/character.json'))"`).
- [ ] Every `characters[*].refs` value resolves to an existing key in its library
      / dimension.
- [ ] Every `characters[*].description` is exactly what the recipe produces from
      its `refs` (recompose and compare).
- [ ] No description contains an action/expression/position (appearance-only).
- [ ] Any token already referenced by a story still exists (grep the story files
      under `imagegen/prompts/` for `{` placeholders before renaming/removing).
