---
name: comfyui-workflow
description: Convert a ComfyUI workflow export into the worker's three-file asset set — workflows/<id>/workflow.json (the API-format skeleton), workflows/<id>/config.json (which nodes a template may override), and templates/<id>/config.json (the values that override them). Use when asked to add/port a ComfyUI graph, create a new workflow or render template, flatten a subgraph export to API format, or wire a downloaded .json from ComfyUI into the image-gen worker.
---

# comfyui-workflow

Turn a ComfyUI graph into the worker's asset triplet. Read `imagegen/workflow.py`
(the renderer) and the existing `imagegen/workflows/1` + `imagegen/templates/1`
first — those are the contract this skill produces against. `workflows/2` +
`templates/2` (Qwen-Image-Edit-2511) are the worked example below.

## The three files and how they relate

```
imagegen/
├── workflows/<id>/workflow.json   # SKELETON: full ComfyUI graph, API format, with default values
├── workflows/<id>/config.json     # OVERRIDE LIST: which nodes a template is allowed to fill, positionally
└── templates/<id>/config.json     # OVERRIDE VALUES: per-panel {field: value} dicts written into those nodes
```

- **`workflow.json` is the skeleton.** A ComfyUI graph in **API format**: a flat
  dict keyed by node id, each value `{ "class_type", "inputs", "_meta" }`. Wires
  are `inputs.<name>: ["<source_node_id>", <output_slot>]`; widget values are
  literals. Every default baked in here is a placeholder the template can replace
  (or leave as-is).
- **`workflows/<id>/config.json` says what may be overridden.** `{ "id", "name",
  "nodes": [ {"id", "type"}, … ] }` — an **ordered** list of the nodes a template
  may customize. Order is the contract: the template's per-panel entries line up
  with this list **by position**, not by id.
- **`templates/<id>/config.json` is the values.** `{ "id", "name", "workflow_id",
  "panels": [ … ] }`. Each **panel** is a list parallel to the workflow config's
  `nodes`; entry *i* is a `{field_name: value}` dict merged into node *i*'s
  `inputs`. **One panel == one ComfyUI run == one output image.** A template's
  `workflow_id` points back at the workflow it renders.

### How `WorkflowBuilder` consumes them (the runtime contract)

`prepare(template_id, story_ref?)`:
- loads the template, follows `workflow_id` to the workflow's `config.json` +
  `workflow.json`;
- **validates positional alignment**: every panel must have exactly
  `len(config.nodes)` entries, else `UnsupportedTemplateError`;
- collects `image_slots` = the distinct values of every `{"image": …}` field
  across panels, first-seen order — one input photo per slot, uploaded under the
  substituted filename (so no separate remap);
- if a `story_ref` (job `type`/`id`, e.g. `"1_1"`) or the template's own
  `"story"` field is set, replaces each panel's **`{PROMPT}` sentinel** with that
  panel's prompt from `prompts/<story_ref>.json` (resolving `{TOKEN}` characters
  first). The prompt lands in whatever field holds `{PROMPT}` — the field name
  (`text`, `prompt`, …) is the template's choice, not baked into `workflow.py`.

`render(prepared, panel, placeholders)`:
- deep-copies the skeleton, then for each `(config_node, panel_entry)` pair writes
  every `panel_entry` field into that node's `inputs` **verbatim** (the field
  **must already exist** in `inputs`, else `UnsupportedTemplateError`) — there is
  no field-name special-casing here;
- substitutes `USER_ID` / `STORY_ID` (and `{INPUT_<n>_AGE}`) in string values.
  Each panel keeps its own seed / `filename_prefix` from the template.

`output_prefixes(workflow)` = every `SaveImage.filename_prefix`, sorted by a
trailing `_V<n>` (prefixes without one sort first as variant 0). The model emits
one `PanelResult` per prefix, so **N SaveImage nodes = N variants per panel**.
`final_output_prefix` requires a prefix ending `_V2` (the face-restored output);
only relevant if the graph has the ReActor V1/V2 face-swap stage.

## Converting a ComfyUI export

A raw editor export (what you download / "Save") is **UI format** — `nodes` +
`links` arrays with positions, plus a `definitions.subgraphs` block. The worker
needs **API format**. Two ways to get there:

1. **Easiest:** in ComfyUI enable dev mode and use **Save (API Format)** — it
   emits the flat `{id: {class_type, inputs}}` dict directly, subgraphs already
   flattened. Drop that in as `workflow.json`.
2. **By hand from a UI export** (when you only have the editor file). Procedure:

   **a. Drop non-executing nodes.** `MarkdownNote` / `Note` carry no `inputs`/
   outputs — omit them.

   **b. Flatten each subgraph.** A subgraph instance is a top-level node whose
   `type` is a uuid matching `definitions.subgraphs[].id`. For instance node `I`:
   - emit every inner node as `"I:<inner_id>"` (matches ComfyUI's own API export
     naming — see workflow 1's `68:…` ids and workflow 2's `170:…`);
   - rewrite inner→inner wires to the prefixed ids;
   - the subgraph's **input** pins map the instance's external inputs to inner
     consumers: trace `inputNode` (negative id, e.g. `-10`) link → if the
     instance's matching input is connected to an external node, point the inner
     consumer at that external node id; if it's **unconnected**, drop the inner
     input (optional `shape:7` inputs) or let the inner node keep its widget value;
   - the subgraph's **output** (`outputNode`, e.g. `-20`) is fed by some inner
     node — wire the *external* consumer (the instance's downstream) straight to
     that inner node's `"I:<inner_id>"`;
   - **proxyWidgets** on the instance just expose inner widgets; unless that pin
     is externally linked, the inner node keeps its own `widgets_values`.

   **c. Translate `widgets_values` → `inputs` literals.** Map each node's
   positional `widgets_values` to its named inputs in node-definition order. A
   widget that's driven by a link (it appears as an `inputs[]` entry with a
   `widget` key in the UI export) becomes a connection, **not** a literal — e.g.
   workflow 2's KSampler gets `steps`/`cfg` from switch nodes, so only
   `seed`/`sampler_name`/`scheduler`/`denoise` stay as literals. Omit UI-only
   widgets like `control_after_generate`.

   **d. Verify no dangling links.** Every `["id", slot]` must reference a node
   that exists in the dict (the test below enforces this).

## Picking the override nodes + writing the template

1. **Choose the nodes a template should drive** and list them, in a deliberate
   order, in `workflows/<id>/config.json`. Typical picks: the `LoadImage`(s) (the
   input photo slots), the prompt encoder, the seed source, the `SaveImage`
   prefix. Mirror workflow 1's ordering habit: images, prompt, steps/seed, saves.
2. **Write `templates/<id>/config.json` panels** parallel to that list. Use the
   `USER_ID_STORY_ID_INPUT_<n>.png` convention for image slots (the model uploads
   the job's photos under those names; fewer photos than slots → the last is
   reused). Give each `SaveImage` a `USER_ID_STORY_ID_P<panel>` prefix and each
   panel its own seed. For a **story-bound** template, set the prompt field's
   value to `"{PROMPT}"` (the sentinel `prepare` fills per panel).

### Field-name gotchas (these bite)

- The field key in a panel entry **must be the node's real input name.** It
  differs by node: prompt is `text` on `CLIPTextEncode` but `prompt` on
  `TextEncodeQwenImageEditPlus`; seed is `noise_seed` on `RandomNoise` but `seed`
  on `KSampler`; steps is `value` on a `PrimitiveInt`. `render` writes the panel's
  fields verbatim, so the key just has to match — no name is special-cased in
  `.py`.
- **Story prompts inject at the `{PROMPT}` sentinel**, not a fixed field name.
  Put `"{PROMPT}"` as the value of whichever field is the prompt input (`text`,
  `prompt`, …); `prepare` replaces it per panel with the story's prompt. A
  story-bound panel that carries **no** `{PROMPT}` raises `UnsupportedTemplateError`.
  (A non-story template just bakes a literal prompt instead.)
- The live worker hardcodes `_RENDER_TEMPLATE_ID = "2"` (`imagegen/model.py`), so
  a new `templates/<id>` is **not** automatically used in production — it ships in
  the asset library until the model is pointed at it. Call that out to the user.
- **Face-swap variants (V1/V2):** to return both a pre- and post-face-swap image
  (as workflows 1 and 2 do), append the ReActor chain — `ReActorOptions` →
  `ReActorFaceSwapOpt` (`input_image` = the decode output, `source_image` = the
  input-photo `LoadImage`) → `ReActorRestoreFace` → a second `SaveImage`. Suffix
  the two prefixes `_V1` / `_V2`; `output_prefixes` orders them and the model
  emits one variant per `SaveImage`.

## Test it

Add `tests/unit/test_workflow_<name>.py` modelled on `test_workflow_qwen_edit.py`:
load the real assets through `WorkflowBuilder`, assert `prepare` aligns panels ↔
config, `render` substitutes panel values + `USER_ID`/`STORY_ID`, and the
rendered graph has **no dangling node links**. Run:

```bash
~/python_env/torch-env/bin/python -m pytest tests/unit/test_workflow_<name>.py -q
./tests/run_tests.sh      # full suite + 100% coverage gate before committing
```

Assets are shipped via `pyproject.toml`'s `package-data` glob
(`workflows/**/*.json`, `templates/**/*.json`) — a new numbered dir needs no
packaging change. No contract/SHA bump either: these are package data, not the
`image-gen-contract` wire schema.

## Worked example — workflows/2 + templates/2 (Qwen-Image-Edit-2511)

Source: a ComfyUI UI export with one subgraph (`Image Edit (Qwen-Image 2511)`,
instance node `170`) between two `LoadImage` nodes (`41` edit target, `83`
reference) and a `SaveImage` (`9`).

- **Flatten:** every inner node became `170:<id>` (`170:169` KSampler,
  `170:151`/`170:149` positive/negative `TextEncodeQwenImageEditPlus`,
  `170:160` `FluxKontextImageScale`, the loaders, the turbo switch chain…). The
  subgraph's `image` input (external `41`) was rewired to `170:160`; `image2`
  (external `83`) to both text encoders; the unconnected `image3`/`value` pins
  were dropped, leaving `170:168`'s `value:false`. The subgraph output
  (`170:158` VAEDecode) was wired straight into `SaveImage 9`.
- **Face-swap parity:** the ReActor chain from workflow 1 was added — node `9`
  saves the raw Qwen edit as `_V1`, and `121` (`ReActorFaceSwapOpt`,
  `source_image` = the input photo `41`) → `120` (`ReActorRestoreFace`) → `119`
  saves the face-restored `_V2`. So a panel yields two variants, like workflow 1.
- **config.json overrides** (positional): `41` LoadImage, `83` LoadImage,
  `170:151` TextEncodeQwenImageEditPlus, `170:169` KSampler, `9` SaveImage (V1),
  `119` SaveImage (V2).
- **template panels** (6, story parity) fill
  `image`/`image`/`prompt`/`seed`/`filename_prefix`×2. The prompt field is named
  `prompt`, and it still receives story prompts because its value is the
  `{PROMPT}` sentinel — proof the injection is field-name-agnostic.
- Built to full parity and now **live**: `_RENDER_TEMPLATE_ID = "2"`, so the worker
  renders every story through Qwen-Image-Edit-2511 (`workflows/2` + `templates/2`).
