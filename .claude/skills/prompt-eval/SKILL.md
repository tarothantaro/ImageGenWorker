---
name: prompt-eval
description: Evaluate a story's image prompts by judging the generated panel images that already sit in the Application local stack's GCS (fake-gcs bucket tarostory-local-images). Use when asked to evaluate/grade/review a story's prompts, check whether a story's generated outputs match their prompts, or verify that the protagonist is left-most, the image is realistic, each person performs the action/interaction the prompt asks, each person is a reasonable size for their depth in the camera, the scene/setting matches the one the panel's prompt describes, that it matches the prompt (composition, clothes, age), and that the image satisfies the panel's authored gist (its intended narrative beat). Judges each panel's V2 (face-restored) output with the vision model and writes a per-panel + per-story markdown report. Pairs with the `story-prompts` and `prompt-lint` skills.
---

# prompt-eval

Judge the **already-generated** panel images for a story against the prompts that
produced them, and write a markdown report. This is the read-and-grade half of
prompt iteration: the worker has already run a story through ComfyUI and written
its outputs to the **Application local stack's** GCS; this skill pulls those
images back and scores each one with the vision model against its panel prompt.

It does **not** generate images. (To produce outputs first, run the worker dev
stack — `deploy/stages/dev/` — or `scripts/smoke_real_comfyui.py`, then come
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
- The live render template (`templates/2`, Qwen-Image-Edit-2511) saves **2
  variants per panel**: `_V1` (pre-face-swap) and `_V2` (face-restored). They are
  flattened to sequential indices, so for index *i*:
  `panel = i // 2`, `variant = i % 2` (0 → V1, 1 → V2). A 6-panel story = 12
  images, indices 0–11. The helper computes this from the live workflow so it
  stays correct if the variant count changes.
  **This skill judges the V2 (face-restored) image of every panel** — V2 is the
  delivered output, so every criterion is graded on it. (V1 is still downloaded
  for reference but not scored.)

### Local mode (no Application stack)

`fetch_outputs.py` reads from the Application's fake-gcs by default, but the
output source is **configurable**. When the worker was driven directly from this
repo — `scripts/generate_stories.py` writes the same `<user>/<story>/outputs/<i>.png`
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
    .claude/skills/prompt-eval/fetch_outputs.py --list
```

If the user already named a `--user-id`/`--story-id` (or a story to evaluate),
skip discovery. If `--list` shows nothing, the stack isn't up or no story has
been generated yet — tell the user; don't fabricate a verdict.

### 2. Fetch the images + build the manifest

Pick the prompt stem (`--story`, e.g. `1_1`) and the GCS components for the set:

```bash
PYTHONPATH=. ~/python_env/torch-env/bin/python \
    .claude/skills/prompt-eval/fetch_outputs.py \
    --story 1_1 --user-id <uid> --story-id <sid>
```

This downloads the PNGs to `/tmp/prompt_eval/<story>__<story_id>/` (override with
`--out`) and writes `manifest.json` there. The manifest joins each downloaded
file to the panel prompt that produced it. For `resolved_prompt` it prefers the
**actual prompt the worker logged** for this run — the dev worker writes one
record per panel of the exact prompt + workflow it submitted to ComfyUI under
`PROMPT_LOG_DIR` (host-mounted `prompt_logs/<story_id>/panel_NN.json`; see
`imagegen/prompt_log.py` + `deploy/stages/dev`). When no log is present it falls
back to a **reconstruction** (`{TOKEN}` characters expanded from
`character.json`, `{INPUT_n_AGE}` dropped). Each entry records which it used via
`prompt_source` (`worker_log` | `reconstructed`), plus `comfyui_prompt_id` and a
`prompt_log` path for debugging; the manifest's top-level `prompt_source` is
`worker_log` / `reconstructed` / `mixed`. It also carries `title`, `lesson`, the
resolved `characters` map, and `variants_per_panel`. Read `manifest.json` before
judging.

Each entry also carries a **`gist`** — the story author's one-sentence statement
of what *this panel must show* (setting, who is present + protagonist far-left
when multi-person, the key action/interaction, the narrative beat), stripped of
style/camera/identity boilerplate. It is the panel's intent, parallel to the
prompt, authored by the `story-prompts` skill (the JSON `gists` array). The
manifest's `has_gists` says whether the story carries them. Grade the image
against the gist as its own rubric row (below); when `gist` is `null` (a pre-gist
story) score that row **NA** and say so.

> The actual-prompt log only exists if the worker generated the story with
> `PROMPT_LOG_DIR` set (the dev stack sets it by default). Point `--log-dir` at a
> different host dir if yours is non-standard. A `mixed`/`reconstructed`
> `prompt_source` means you are grading against a re-derived prompt — note that
> in the report rather than asserting it's exactly what ran.

### 3. Judge each panel (vision)

Read `manifest.json`, then for **each panel** Read its **V2** PNG — the entries
where `variant_label == "V2"` (the face-restored, delivered image) — and score it
against that panel's `resolved_prompt` and the rubric below. Judge every panel on
V2. Cite concrete visual evidence ("two figures, protagonist is on the *right*")
— never grade from the prompt text alone.

**Ground every panel in the pixels before you score it.** First write a one-line
literal description of what the V2 image *actually shows*: how many people are in
it and their **left-to-right order naming who is left-most** (e.g. "L→R: child,
elderly woman"). Score the spatial criteria (Far-left, Scale) from *that*
description, not from what the prompt intended. The prompt says where people
*should* be; only the pixels say where they *are* — when they disagree, grade the
pixels. **Do not record a `partial`/`fail` on any criterion without quoting the
specific thing in the image that fails it** ("the protagonist is second from the
left, not left-most"); if you can't point to it, it passes. These spatial calls
are the easiest to hallucinate from the prompt — re-look at the image before
writing `fail`. The `resolved_prompt` is the **actual
prompt sent to ComfyUI** when `prompt_source == "worker_log"` — grade the image
against exactly that text (it already has the real age word, e.g. "4-year-old",
substituted in). When debugging a defect, the entry's `prompt_log` file holds the
full rendered workflow that produced the image.

**Per-panel rubric** (judged on the V2 image):

| Criterion | What to check |
|---|---|
| **Protagonist far-left** | The input-photo person is the **left-most** figure in any **multi-person** panel — "left-most" = nearest the **left edge of the image as you view it** (viewer's left, not the subject's). NA for a solo panel. A non-left protagonist is a real defect, not a nitpick. |
| **Realism** | Reads as a real photograph — plausible anatomy (hands, limbs, faces), natural lighting/shadows/perspective, correct count of fingers/people. No cartoon/CGI/uncanny render, warps, duplicated or fused bodies, or garbled text. |
| **Scale & depth** | Each person's size is consistent with their depth in the scene — figures nearer the camera are larger, those farther back are smaller, following perspective. No figure rendered giant or miniature for its position, and the protagonist isn't shrunk/enlarged relative to the rest of the cast. |
| **Scene & setting** | The image's **setting** matches what *this panel's* `resolved_prompt` describes — the **location type** (living room / classroom / garden / playground …), the named **setting anchors** (the key furniture/landmarks, e.g. a wooden shelf, a toy box, a low fence), the **time of day / lighting**, and any described **change of state** (a shattered vase, a now-tidy room). Each panel is graded **against its own prompt only** — do **not** grade cross-panel scene consistency (whether two panels render the *same* room) here; that is out of scope for now. Call out a wrong location (e.g. "prompt says classroom, image is a living room") or a missing/contradicted anchor. |
| **Prompt match** | The image matches the `resolved_prompt` — **composition/shot**, **clothes/wardrobe**, **age**, props, expression. (Setting/location is scored under **Scene & setting**.) Call out each mismatch specifically. |
| **Action & interaction** | Each person performs the **action** the prompt asks, and people **interacting** do so coherently — the prompt's verbs read in the image (e.g. handshake, hug, pointing, sharing a toy), with bodies/gazes/hands oriented toward one another. No disconnected, contradictory, or idle poses where the prompt calls for an action or exchange. |
| **Cast & identity** | Each `{TOKEN}` present matches its `characters[TOKEN]` description (gender, age, build, hair, wardrobe); the protagonist looks like the **same person** across all panels. The protagonist's face does **not** have to face the camera — natural three-quarter, profile, and downward-glancing poses are fine. Only flag it when the face is *fully* hidden (a pure back-of-head shot) such that the face-swap could not land. |
| **Gist satisfied** | The image conveys the panel's **`gist`** — the author's intended beat: the right setting, the right people doing the right thing, and the narrative point landing (e.g. "child *offers* a block and the friend *accepts*"; "the vase is *shattered* and the child is *shocked*"). This is the *meaning* check, above the literal-prompt match: an image can match the prompt's words yet miss the beat (the offer reads as the child keeping the block; the "shattered vase" still shows a whole vase). Judge whether someone seeing only the gist would accept this image. NA when `gist` is `null`. |

Score each criterion **pass / partial / fail** (NA where it doesn't apply) with a
one-line evidence note.

### 4. Write the report

Derive the **Summary** counts and the **Verdict** mechanically from the per-panel
lines you just wrote — do not re-judge from memory. Every issue named in the
Verdict or "Top issues" must trace to a panel line you scored `partial`/`fail` for
that exact criterion: never cite a panel as failing a check its own line passed
(e.g. don't say "drifts off-left in P6" if Panel 6's Far-left line is `pass`). If
the per-panel lines show no `fail` and only minor `partial`s, the verdict is
`✅ ship`, not `⚠️ revise`.

Write markdown to the `report_path` from the manifest
(`<out_dir>/report.md`). Structure:

```markdown
# Prompt eval — <title> (<story>)

- **Lesson:** <lesson>
- **Source:** gs://<bucket>/<user>/<story>/outputs/  (V2 of <P> panels)
- **Verdict:** ✅ ship / ⚠️ revise prompts / ❌ regenerate — one-line rationale

## Summary
- Protagonist far-left: <X/Y> multi-person panels
- Realism: <X/Y> panels
- Scale & depth: <X/Y> panels
- Scene & setting: <X/Y> panels
- Prompt match: <count> pass / <count> partial / <count> fail
- Action & interaction: <X/Y> panels
- Cast & identity: <consistent?>
- Gist satisfied: <X/Y> panels
- Top issues (ranked): 1) … 2) … 3) …

## Panels (V2)
### Panel 1
- Resolved prompt: "<resolved_prompt>"
- Gist: "<gist>"
- Frame: <what the pixels show — #people, L→R order naming who is left-most, anything over the protagonist's face>
- Far-left: NA (solo) | pass | **fail** — <evidence>
- Realism: pass | **fail** — <evidence>
- Scale & depth: NA (solo) | pass | **fail** — <evidence>
- Scene & setting: pass | partial | **fail** — location/anchors/lighting vs this panel's prompt <evidence>
- Prompt match: pass | partial — composition/clothes/age/… <evidence>
- Action & interaction: NA (no action) | pass | partial — <evidence>
- Cast & identity: NA | pass | partial — <evidence>
- Gist satisfied: NA (no gist) | pass | partial | **fail** — does the intended beat land? <evidence>
… (every panel, on its V2 image) …

## Recommended prompt fixes
- Panel N: <specific edit to the prompt string that would fix the observed defect>
```

Tie each recommended fix to a `story-prompts` rule (e.g. "state 'on the far
left' so the protagonist is left-most", "name a 'medium shot, photorealistic'
to push realism", "the cast age/clothes drift means the panel re-describes
appearance — remove it, the `{TOKEN}` carries it"). Keep fixes concrete enough
to paste into the prompt.

### 5. Report back

Tell the user the verdict, the headline numbers (far-left, realism, scale &
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
