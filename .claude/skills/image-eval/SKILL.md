---
name: image-eval
description: Evaluate a story's generated panel images with the vision model — the final, image-side eval for pictures the worker already produced in the Application local stack's GCS, fake-gcs bucket tarostory-local-images, or a local run dir. Use when asked to evaluate/grade/review generated story images or outputs, check whether outputs match their prompts, or verify realism, person actions/interactions, depth-relative person size, scene/setting, composition, clothes, age, and whether each panel satisfies its authored gist. Writes a per-panel + per-story markdown report. The image-side counterpart of `story-prompts-eval` (which grades prompt text without rendering). Pairs with the `story-prompts` and `story-prompts-eval` skills.
---

# image-eval

Judge the **already-generated** panel images for a story against the prompts that
produced them, and write a markdown report. This is the read-and-grade half of
prompt iteration: the worker has already run a story through ComfyUI and written
its outputs to the **Application local stack's** GCS; this skill pulls those
images back and scores each one with the vision model against its panel prompt.

It does **not** generate images. (To produce outputs first, run the worker dev
stack — `deploy/stages/dev/` — or `tests/smoke/smoke_real_comfyui.py`, then come
back here.) Read `imagegen/prompts/README.md` and the `story-prompts` skill for
the prompt schema and the rules a good prompt follows — this skill grades against
those same rules.

## Where the results live

The outputs are written to the fake-gcs-server the **Application local stack
owns** (`../Application/server/deploy/stages/local/`), shared with the worker:

```
gs://tarostory-local-images/<user_id>/<story_id>/outputs/<index>.png
```

- Host reaches fake-gcs at `http://localhost:4443` (the stack must be **up** —
  `../Application/server/deploy/stages/local/up.sh`).
- `<story_id>` here is the **job/story id** the Application assigned, **not** the
  prompt-file stem. The prompt set is selected separately by `type`/`id`.
- The live render template (`templates/2`, Qwen-Image-Edit-2511) saves **one
  image per panel** at a flat output index, so for index *i*: `panel = i` (a
  6-panel story = 6 images, indices 0–5). There is no longer a pre-face-swap /
  face-restored split — the single image is the delivered output. The helper
  derives the per-panel image count from the live workflow, so the
  `panel = i // variants` math stays correct if a template is ever changed to
  emit more than one image per panel.
  **This skill judges that one delivered image of every panel** (the manifest
  entry with `is_delivered: true`; with one image per panel that is simply the
  panel's image), grading every criterion on it.

### Local mode (no Application stack)

`fetch_outputs.py` reads from the Application's fake-gcs by default, but the
output source is **configurable**. When the worker was driven directly from this
repo — `.claude/skills/local-batch-eval/generate_stories.py` writes the same `<user>/<story>/outputs/<i>.png`
layout to a local run dir — pass `--local-root <run>/outputs` (or set
`LOCAL_OUTPUT_ROOT`) and it reads the PNGs off disk instead, with no GCS / no
emulator / no Application stack in the loop. Everything downstream (the
index→panel/variant math, the prompt-log join, the manifest, the rubric) is
identical. The `local-batch-eval` skill wraps this generate-then-grade loop and
adds a review web UI; use that when asked to generate **and** evaluate the catalog
locally.

## Workflow

### 1. Locate the output set

Confirm the local stack is up, then discover which `<user>/<story>` sets exist:

```bash
PYTHONPATH=. ~/python_env/torch-env/bin/python \
    .claude/skills/image-eval/fetch_outputs.py --list
```

If the user already named a `--user-id`/`--story-id` (or a story to evaluate),
skip discovery. If `--list` shows nothing, the stack isn't up or no story has
been generated yet — tell the user; don't fabricate a verdict.

### 2. Fetch the images + build the manifest

Pick the prompt stem (`--story`, e.g. `1_1`) and the GCS components for the set:

```bash
PYTHONPATH=. ~/python_env/torch-env/bin/python \
    .claude/skills/image-eval/fetch_outputs.py \
    --story 1_1 --user-id <uid> --story-id <sid>
```

In local mode, if `--story`, `--user-id`, and `--story-id` are all omitted, the
helper discovers every generated output set under `--local-root`, assumes each
output `story_id` is also the prompt stem, and writes one manifest per story. Use
`--out <eval-dir>` as the eval root when you need a non-default location;
otherwise results land under `eval_runs/latest/eval/`, with each manifest at
`eval_runs/latest/eval/<story>__<story>/manifest.json`.

This writes `manifest.json` to
`eval_runs/latest/eval/<story>__<story_id>/` by default (override with `--out`;
in batch mode `--out` is a root directory). In GCS mode it also downloads the
PNGs there. In local mode, it leaves the PNGs under `--local-root` and the
manifest's image `file` fields reference those canonical output files directly.
The manifest joins each image file to the panel prompt that produced it. For
`resolved_prompt` it prefers the
**actual prompt the worker logged** for this run — the dev worker writes one
record per panel of the exact prompt + workflow it submitted to ComfyUI under
`PROMPT_LOG_DIR` (host-mounted `prompt_logs/<story_id>/panel_NN.json`; see
`imagegen/prompt_log.py` + `deploy/stages/dev`). When no log is present it falls
back to a **reconstruction** (`{TOKEN}` characters expanded from
`character.json`, `{INPUT_n_AGE}` dropped). Each entry records which it used via
`prompt_source` (`worker_log` | `reconstructed`), plus `comfyui_prompt_id` and a
`prompt_log` path for debugging; the manifest's top-level `prompt_source` is
`worker_log` / `reconstructed` / `mixed`. It also carries `title`, `lesson`, the
resolved `characters` map, per-panel `panel_dialog` from the story `texts`, and
`variants_per_panel`. Read `manifest.json` before judging.

Each entry also carries a **`gist`** — the story author's one-sentence statement
of what *this panel must show* (setting, who is present, the key
action/interaction, the narrative beat), stripped of
style/camera/identity boilerplate. It is the panel's intent, parallel to the
prompt, authored by the `story-text` skill (the JSON `gists` array). The
manifest's `has_gists` says whether the story carries them. Grade the image
against the gist as its own rubric row (below); when `gist` is `null` (a pre-gist
story) score that row **NA** and say so.

> The actual-prompt log only exists if the worker generated the story with
> `PROMPT_LOG_DIR` set (the dev stack sets it by default). Point `--log-dir` at a
> different host dir if yours is non-standard. A `mixed`/`reconstructed`
> `prompt_source` means you are grading against a re-derived prompt — note that
> in the report rather than asserting it's exactly what ran.

### 3. Judge each panel (vision)

Read `manifest.json`, then for **each panel** Read its delivered PNG — the entry
where `is_delivered` is `true` (with one image per panel that is simply the
panel's image) — and score it
against that panel's `resolved_prompt` and the rubric below. Judge every panel on
its delivered image. Cite concrete visual evidence ("two figures, protagonist is
on the *right*") — never grade from the prompt text alone.

**Ground every panel in the pixels before you score it.** First write a one-line
literal description of what the image *actually shows*: how many people are in
it and their **left-to-right order** (e.g. "L→R: child, elderly woman"), plus
their depth in frame. Score the spatial criterion (Scale & depth) from *that*
description, not from what the prompt intended. The prompt says where people
*should* be; only the pixels say where they *are* — when they disagree, grade the
pixels. **Do not record a `partial`/`fail` on any criterion without quoting the
specific thing in the image that fails it** ("the child in the background is
rendered larger than the adult in front"); if you can't point to it, it passes.
These spatial calls are the easiest to hallucinate from the prompt — re-look at
the image before writing `fail`. The `resolved_prompt` is the **actual
prompt sent to ComfyUI** when the manifest entry's `prompt_source` is
`worker_log`; grade the image against exactly that text (it already has the real
age word, e.g. "4-year-old", substituted in). Do not add a separate per-panel
`prompt source` line to the report, because it duplicates the resolved prompt's
role. When debugging a defect, the entry's `prompt_log` file holds the full
rendered workflow that produced the image.

**Per-panel rubric** (judged on the panel's delivered image):

| Criterion | What to check |
|---|---|
| **Realism** | Reads as a real photograph — plausible anatomy (hands, limbs, faces), natural lighting/shadows/perspective, correct count of fingers/people. No cartoon/CGI/uncanny render, warps, duplicated or fused bodies, or garbled text. |
| **Scale & depth** | Each person's size is consistent with their depth in the scene — figures nearer the camera are larger, those farther back are smaller, following perspective. No figure rendered giant or miniature for its position, and the protagonist isn't shrunk/enlarged relative to the rest of the cast. |
| **Scene & setting** | The image's **setting** matches what *this panel's* `resolved_prompt` describes — the **location type** (living room / classroom / garden / playground …), the named **setting anchors** (the key furniture/landmarks, e.g. a wooden shelf, a toy box, a low fence), the **time of day / lighting**, and any described **change of state** (a shattered vase, a now-tidy room). Each panel is graded **against its own prompt only** — do **not** grade cross-panel scene consistency (whether two panels render the *same* room) here; that is out of scope for now. Call out a wrong location (e.g. "prompt says classroom, image is a living room") or a missing/contradicted anchor. |
| **Prompt match** | The image matches the `resolved_prompt` — **composition/shot**, **clothes/wardrobe**, **age**, props, expression. (Setting/location is scored under **Scene & setting**.) Call out each mismatch specifically. |
| **Action & interaction** | Each person performs the **action** the prompt asks, and people **interacting** do so coherently — the prompt's verbs read in the image (e.g. handshake, hug, pointing, sharing a toy), with bodies/gazes/hands oriented toward one another. No disconnected, contradictory, or idle poses where the prompt calls for an action or exchange. |
| **Cast & identity** | Each `{TOKEN}` present matches its `characters[TOKEN]` description (gender, age, build, hair, wardrobe); the protagonist looks like the **same person** across all panels. The protagonist's face does **not** have to face the camera — natural three-quarter, profile, and downward-glancing poses are fine. Only flag it when the face is *fully* hidden (a pure back-of-head shot) such that the edit could not carry the input identity. |
| **Gist satisfied** | The image conveys the panel's **`gist`** — the author's intended beat: the right setting, the right people doing the right thing, and the narrative point landing (e.g. "child *offers* a block and the friend *accepts*"; "the vase is *shattered* and the child is *shocked*"). This is the *meaning* check, above the literal-prompt match: an image can match the prompt's words yet miss the beat (the offer reads as the child keeping the block; the "shattered vase" still shows a whole vase). Judge whether someone seeing only the gist would accept this image. NA when `gist` is `null`. |

Score each criterion **pass / partial / fail** (NA where it doesn't apply) with a
one-line evidence note.

### 4. Write the report

Derive the **Summary** counts and the **Verdict** mechanically from the per-panel
lines you just wrote — do not re-judge from memory. Every issue named in the
Verdict or "Top issues" must trace to a panel line you scored `partial`/`fail` for
that exact criterion: never cite a panel as failing a check its own line passed
(e.g. don't say "the cast looks wrong in P6" if Panel 6's Cast & identity line is
`pass`). If
the per-panel lines show no `fail` and only minor `partial`s, the verdict is
`✅ ship`, not `⚠️ revise`.

Write markdown to the `report_path` from the manifest
(`<out_dir>/report.md`). Structure:

```markdown
# Image eval — <title> (<story>)

- **Lesson:** <lesson>
- **Source:** gs://<bucket>/<user>/<story>/outputs/  (<P> panels, one image each)
- **Verdict:** ✅ ship / ⚠️ revise prompts / ❌ regenerate — one-line rationale

## Summary
- Realism: <X/Y> panels
- Scale & depth: <X/Y> panels
- Scene & setting: <X/Y> panels
- Prompt match: <count> pass / <count> partial / <count> fail
- Action & interaction: <X/Y> panels
- Cast & identity: <consistent?>
- Gist satisfied: <X/Y> panels
- Top issues (ranked): 1) … 2) … 3) …

## Panels
### Panel 1
- Resolved prompt: "<resolved_prompt>"
- Panel dialog: "<panel_dialog>"
- Gist: "<gist>"
- Frame: <what the pixels show — #people, L→R order, depth, anything over the protagonist's face>
- Realism: pass | **fail** — <evidence>
- Scale & depth: NA (solo) | pass | **fail** — <evidence>
- Scene & setting: pass | partial | **fail** — location/anchors/lighting vs this panel's prompt <evidence>
- Prompt match: pass | partial — composition/clothes/age/… <evidence>
- Action & interaction: NA (no action) | pass | partial — <evidence>
- Cast & identity: NA | pass | partial — <evidence>
- Gist satisfied: NA (no gist) | pass | partial | **fail** — does the intended beat land? <evidence>
… (every panel, on its delivered image) …

## Recommended prompt fixes
- Panel N: <specific edit to the prompt string that would fix the observed defect>
```

Tie each recommended fix to a `story-prompts` rule (e.g. "name a 'medium shot,
photorealistic' to push realism", "the cast age/clothes drift means the panel
re-describes appearance — remove it, the `{TOKEN}` carries it", "add a placement
cue only if the beat needs a specific arrangement"). Keep fixes concrete enough
to paste into the prompt.

### 5. Report back

Tell the user the verdict, the headline numbers (realism, scale &
depth, scene & setting, prompt match, action & interaction, gist satisfied), and
the `report.md` path. If outputs were missing or the set was
partial, say so plainly.

## Notes

- This grades against the same rules the `story-prompts` skill writes to. When a
  fix is warranted, the natural next step is editing
  `imagegen/prompts/<story>.json` via that skill, regenerating, and re-running
  this eval.
- The helper needs no real GCP creds — it talks to the local emulator with
  anonymous credentials (`STORAGE_EMULATOR_HOST=http://localhost:4443`). Override
  the endpoint/bucket/project with `--bucket` / `--project` / that env var if the
  local stack uses non-defaults.
