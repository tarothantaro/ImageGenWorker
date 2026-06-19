# Story prompts

Authoring assets for the text prompts that drive the Qwen-Image-Edit-2511 edit
(the `CLIPTextEncode` `text` field of each panel in `imagegen/templates/*`).
A **story** is an ordered set of prompts; each prompt is run against the **same
single input photo** to produce one panel image, so the user's photo appears in
every panel of the story.

Three skills own this directory:

| Skill | Owns | Use it to |
|---|---|---|
| `story-prompts` | `<type>_<id>.json` (everything but `texts`) | Write/edit a story's prompt array + metadata |
| `story-text` | the `texts` field of `<type>_<id>.json` | Write/edit a story's per-panel read-aloud storybook text |
| `character-config` | `character.json` | Add/edit the generated supporting cast |

## Files

```
imagegen/prompts/
├── character.json     # library of generated supporting characters ({TOKEN} placeholders)
├── 1_1.json           # story: type 1 (life_lesson), id 1
└── README.md          # this file
```

### Story file naming: `<type>_<id>.json`

`<type>` is the numeric story category, `<id>` is the 1-based index within that
category. `1_1.json` = first life-lesson story. The file stem is also the API
catalog doc id (`templates/<type>_<id>`) and the Pub/Sub job's `type`/`id`
selector.

**Story type registry**

| type | name | description |
|---|---|---|
| 1 | `life_lesson` | A short visual narrative whose arc teaches one clear lesson. |

The `name` column is the canonical mapping — it lives in code (the worker's
`sync_story_catalog._TYPE_NAMES`), not in the JSON. Reserve new numbers here as
new categories are added (and add them to `_TYPE_NAMES`).

### Story file schema

```jsonc
{
  "type": 1,
  "id": 1,
  "title": "Kindness Comes Back Around",
  "lesson": "A small act of kindness returns to you when you least expect it.",
  "characters": ["GENDER_F_AGE_70_RACE_ASIAN", "GENDER_M_AGE_25_RACE_ASIAN"],
  "prompts": [
    "…panel 1 prompt…",
    "…panel 2 prompt…"
  ],
  "texts": [
    "…panel 1 storybook line…",
    "…panel 2 storybook line…"
  ],
  "version": 1
}
```

- `type` / `id` — the story selector (replaces the old `story_type` /
  `story_number`); the file is named `<type>_<id>.json`.
- `prompts` — the array of per-panel prompt strings, **in panel order**. This is
  the payload; `prompts[i]` becomes panel *i*'s `text`. The **panel count is
  `len(prompts)`** (there is no separate `panel_count` field), and it must match
  the render template `templates/1`'s panel count (6).
- `texts` — the per-panel **read-aloud storybook narration** (scene + dialog),
  one string per panel and the **same length/order as `prompts`** (`texts[i]`
  is what the reader sees on panel *i*'s page). Authored by the `story-text`
  skill; the image pipeline never reads it — it is synced to the API catalog as
  `story_text` (`operation/sync_story_catalog.py`) and shown alongside the
  generated images. Unlike `prompts`, it carries **no** `{TOKEN}` placeholders.
- `characters` — every `{TOKEN}` the prompts reference, for quick auditing. The
  input-photo protagonist is implicit and never listed.

## The two hard constraints (every prompt must honor)

1. **Input person on the far left.** Whenever a panel shows more than one person,
   the protagonist (from the input photo) is the **left-most** figure. The
   face-swap stage maps the input face onto the left-most detected face, so this
   is a pipeline requirement, not a stylistic one.
2. **Input person's face ≥70% visible.** Compose them front-on or three-quarter,
   facing the camera, face unobstructed — unless the narrative genuinely requires
   otherwise. The same photo is the face-swap source, so a hidden/averted face
   degrades the swap.

## Runtime placeholder substitution

Prompts contain `{TOKEN}` placeholders for the **generated** supporting cast
(e.g. `{GENDER_F_AGE_70_RACE_ASIAN}`). At runtime each is replaced by
`character.json → characters[TOKEN].description` (a flat string replace, the same
mechanism `workflow.py` uses for `USER_ID` / `STORY_ID`). Authoring templates
keep the placeholder verbatim. See `character.json` and the `character-config`
skill for the placeholder/runtime contract.
