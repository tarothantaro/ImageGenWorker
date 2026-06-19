---
name: prompt-eval
description: Evaluate a story's image prompts by judging the generated panel images that already sit in the Application local stack's GCS (fake-gcs bucket tarostory-local-images). Use when asked to evaluate/grade/review a story's prompts, check whether a story's generated outputs match their prompts, or verify that the protagonist is left-most, the image is realistic, and it matches the prompt (composition, clothes, age). Judges each panel's V2 (face-restored) output with the vision model and writes a per-panel + per-story markdown report. Pairs with the `story-prompts` skill.
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
file to its **resolved** panel prompt — `{TOKEN}` characters expanded from
`character.json`, `{INPUT_n_AGE}` dropped — i.e. the effective sentence the model
rendered. It also carries `title`, `lesson`, the resolved `characters` map, and
`variants_per_panel`. Read `manifest.json` before judging.

### 3. Judge each panel (vision)

Read `manifest.json`, then for **each panel** Read its **V2** PNG — the entries
where `variant_label == "V2"` (the face-restored, delivered image) — and score it
against that panel's `resolved_prompt` and the rubric below. Judge every panel on
V2. Cite concrete visual evidence ("two figures, protagonist is on the *right*")
— never grade from the prompt text alone.

**Per-panel rubric** (judged on the V2 image):

| Criterion | What to check |
|---|---|
| **Protagonist far-left** | The input-photo person is the **left-most** figure in any **multi-person** panel. NA for a solo panel. A non-left protagonist is a real defect, not a nitpick. |
| **Realism** | Reads as a real photograph — plausible anatomy (hands, limbs, faces), natural lighting/shadows/perspective, correct count of fingers/people. No cartoon/CGI/uncanny render, warps, duplicated or fused bodies, or garbled text. |
| **Prompt match** | The image matches the `resolved_prompt` — **composition/shot**, **clothes/wardrobe**, **age**, setting, action, props, expression. Call out each mismatch specifically. |
| **Cast & identity** | Each `{TOKEN}` present matches its `characters[TOKEN]` description (gender, age, build, hair, wardrobe); the protagonist looks like the **same person** across all panels. |

Score each criterion **pass / partial / fail** (NA where it doesn't apply) with a
one-line evidence note.

### 4. Write the report

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
- Prompt match: <count> pass / <count> partial / <count> fail
- Cast & identity: <consistent?>
- Top issues (ranked): 1) … 2) … 3) …

## Panels (V2)
### Panel 1
- Resolved prompt: "<resolved_prompt>"
- Far-left: NA (solo) | pass | **fail** — <evidence>
- Realism: pass | **fail** — <evidence>
- Prompt match: pass | partial — composition/clothes/age/… <evidence>
- Cast & identity: NA | pass | partial — <evidence>
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

Tell the user the verdict, the headline numbers (far-left, realism, prompt
match), and the `report.md` path. If outputs were missing or the set was partial,
say so plainly.

## Notes

- This grades against the same rules the `story-prompts` skill writes to. When a
  fix is warranted, the natural next step is editing
  `imagegen/prompts/<story>.json` via that skill, regenerating, and re-running
  this eval.
- The helper needs no real GCP creds — it talks to the local emulator with
  anonymous credentials (`STORAGE_EMULATOR_HOST=http://localhost:4443`). Override
  the endpoint/bucket/project with `--bucket` / `--project` / that env var if the
  local stack uses non-defaults.
