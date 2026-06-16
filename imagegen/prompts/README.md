# Story prompts

Authoring assets for the text prompts that drive the Qwen-Image-Edit-2511 edit
(the `CLIPTextEncode` `text` field of each panel in `imagegen/templates/*`).
A **story** is an ordered set of prompts; each prompt is run against the **same
single input photo** to produce one panel image, so the user's photo appears in
every panel of the story.

Two skills own this directory:

| Skill | Owns | Use it to |
|---|---|---|
| `story-prompts` | `<type>_<n>.json` | Write/edit a story's prompt array |
| `character-config` | `character.json` | Add/edit the generated supporting cast |

## Files

```
imagegen/prompts/
├── character.json     # library of generated supporting characters ({TOKEN} placeholders)
├── 1_1.json           # story: type 1 (life_lesson), number 1
└── README.md          # this file
```

### Story file naming: `<story_type>_<story_number>.json`

`<story_type>` is the numeric story category, `<story_number>` is the 1-based
index within that category. `1_1.json` = first life-lesson story.

**Story type registry**

| type | name | description |
|---|---|---|
| 1 | `life_lesson` | A short visual narrative whose arc teaches one clear lesson. |

(Reserve new numbers here as new categories are added.)

### Story file schema

```jsonc
{
  "story_type": 1,
  "story_type_name": "life_lesson",
  "story_number": 1,
  "title": "Kindness Comes Back Around",
  "lesson": "A small act of kindness returns to you when you least expect it.",
  "characters": ["GENDER_F_AGE_70_RACE_ASIAN", "GENDER_M_AGE_25_RACE_ASIAN"],
  "panel_count": 6,
  "prompts": [
    "…panel 1 prompt…",
    "…panel 2 prompt…"
  ]
}
```

- `prompts` — the array of per-panel prompt strings, **in panel order**. This is
  the payload; `prompts[i]` becomes panel *i*'s `text`.
- `characters` — every `{TOKEN}` the prompts reference, for quick auditing. The
  input-photo protagonist is implicit and never listed.
- `panel_count` — `== len(prompts)`, and must match the chosen template's panel
  count (e.g. template `4` = 6 panels, template `3` = 1 panel).

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
