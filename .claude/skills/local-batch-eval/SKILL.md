---
name: local-batch-eval
description: Generate every story locally by driving the image-gen worker directly against a live ComfyUI (no Pub/Sub, no GCS, no Application stack), then evaluate the outputs with the prompt-eval rubric and open a local web UI to review them. Use when asked to "generate all stories and evaluate", "run the local batch eval", "render every story from <photo> and review", regenerate-and-grade the whole catalog, or eyeball a story's outputs (input photo + actual prompts + output image + eval) in a browser. Defaults to tests/assets/leo.jpg at age 4. Pairs with the `prompt-eval`, `story-prompts`, and `character-config` skills.
---

# local-batch-eval

Run the **whole story catalog** through the real model on this machine and review
the results, end to end, without any of the production plumbing:

```
scripts/generate_stories.py   →  outputs + prompt logs on local disk
   (drives imagegen.model.ComfyUIModel directly against live ComfyUI :8188)
prompt-eval/fetch_outputs.py  →  per-story manifest.json   (--local-root mode)
prompt-eval rubric (vision)   →  per-story report.md
tools/review_app/server.py    →  one web page to review every story
```

This is the generate-**and**-grade counterpart to `prompt-eval` (which only
grades images that the Application stack already produced into GCS). Here the
worker is driven directly from *this* repo, outputs land in a local run dir, and
`fetch_outputs.py` reads them with `--local-root` instead of GCS. The judging
itself **reuses the `prompt-eval` skill** — same rubric, same report format.

## Preconditions

- A **live ComfyUI** on `http://localhost:8188` (same requirement as
  `scripts/smoke_real_comfyui.py`). Generation is real and slow — each panel is
  one ComfyUI run of seconds-to-minutes; the full 21-story catalog is 21 × 6
  panels = 126 images (one per panel). Generate a subset with `--stories` while iterating.
- Run everything from the repo root with `PYTHONPATH=.` and the project Python
  (`~/python_env/torch-env/bin/python`).

## Workflow

### 1. Generate the stories

Default run: every `imagegen/prompts/1_*.json` from `tests/assets/leo.jpg` at age
4, into `eval_runs/latest/`.

```bash
PYTHONPATH=. ~/python_env/torch-env/bin/python scripts/generate_stories.py \
    --input tests/assets/leo.jpg --age "4-year-old" \
    --run-dir eval_runs/latest
```

While iterating, scope it down: `--stories 1_1,1_2`. Other knobs: `--url` (remote
ComfyUI), `--user-id` (defaults to the input filename stem, e.g. `leo`),
`--timeout`, `--age` (the phrase substituted for `{INPUT_1_AGE}` — keep it
natural before the word "person", e.g. `"4-year-old"` or `"23-month-old"`).

This writes (run dir layout):

```
eval_runs/latest/
├── run.json                                  # batch metadata + per-story status
├── input.jpg                                 # copy of the input photo (for the UI)
├── outputs/<user>/<story>/outputs/<i>.png    # GCS-mirror layout → fetch with --local-root
└── prompt_logs/<story>/panel_<NN>.json       # actual prompt + workflow per panel
```

`story_id` == the prompt-file stem (e.g. `1_1`), so the eval step's `--story`,
`--story-id` and prompt-log dir all line up. The script keeps going if one story
fails and records the failure in `run.json` (it exits non-zero if any failed).
Read `run.json` to see which stories produced images.

### 2. Build the per-story manifests (local source)

For **each** story that generated OK, run `prompt-eval`'s `fetch_outputs.py` in
local mode — it reads the PNGs off disk (no GCS / no Application stack) and joins
each to its **actual logged prompt**:

```bash
PYTHONPATH=. ~/python_env/torch-env/bin/python \
    .claude/skills/prompt-eval/fetch_outputs.py \
    --local-root eval_runs/latest/outputs \
    --log-dir   eval_runs/latest/prompt_logs \
    --story 1_1 --user-id leo --story-id 1_1 \
    --out eval_runs/latest/eval/1_1__1_1
```

`--local-root` (or `LOCAL_OUTPUT_ROOT`) is the switch that makes the otherwise
GCS-bound script read locally. With the prompt logs present, the manifest's
`prompt_source` is `worker_log` — i.e. you grade against the exact text ComfyUI
received, with the real age word substituted in. Put each story's `--out` under
`eval_runs/<run>/eval/<story>__<story_id>/` so the review app finds it.

> Tip: loop over the OK stories from `run.json`, substituting `--story` /
> `--user-id` / `--story-id` / `--out` per story.

### 3. Judge each story (vision) — via `prompt-eval`

Now grade exactly as the **`prompt-eval` skill** describes — read
`.claude/skills/prompt-eval/SKILL.md` §3 (per-panel rubric) and §4 (report
structure) and follow them verbatim for each story:

- Read the story's `manifest.json`, then Read each panel's delivered PNG (the
  entries where `is_delivered` is `true` — one image per panel on the live
  template) and score it
  against that panel's `resolved_prompt` and the rubric (realism, scale & depth,
  scene & setting, prompt match, action & interaction, cast & identity).
- Cite concrete visual evidence from the image; never grade from prompt text
  alone.
- Write the markdown report to the manifest's `report_path`
  (`eval_runs/<run>/eval/<story>__<story_id>/report.md`) in the §4 format
  (the review app keys off `**Verdict:**`, `## Summary`, `### Panel N`, and the
  trailing `## Recommended prompt fixes`).

Do this for every generated story so the review page is complete.

### 4. Open the review web UI

**Kill any stale server first, then start one fresh for the new report.** A
review server from a previous run is likely still running (left backgrounded);
reusing it risks a port clash (`OSError: [Errno 98] Address already in use`) or,
worse, silently serving an old run dir. Kill the old one and start a single new
server pinned to *this* run:

```bash
pkill -f "review_app/server.py"                 # drop any server from a prior run
sleep 1
nohup ~/python_env/torch-env/bin/python tools/review_app/server.py \
    --run-dir eval_runs/latest --port 8000 > eval_runs/review_app.log 2>&1 &
disown
sleep 2 && curl -s -o /dev/null -w "review app HTTP %{http_code}\n" http://127.0.0.1:8000/
```

Notes:
- Use `nohup … & disown` (not a bare `&`) so the server survives the turn; health-check
  with the `curl` above (expect `HTTP 200`) before handing the URL over.
- If the bind still fails with "Address already in use", the just-killed socket is in
  `TIME_WAIT` — wait a couple of seconds and retry, or pick another port (`--port 8001`).
- The server re-reads the run dir on every request, so once it's up you can re-judge or
  re-generate and just refresh — no restart needed *within* the same run. The kill/restart
  is only for starting a **new** report (new run dir, or after killing an orphaned server).

Open `http://127.0.0.1:8000/`. The index lists every story with a verdict badge;
each story page shows the **input photo**, and per panel the **actual prompt**
sent to ComfyUI, the **output image** for that panel, and the judge's
**eval notes** for that panel, plus the summary and recommended fixes. Stdlib-only
— no install step. Leave it running (backgrounded) and give the user the URL.

> **Re-evaluating an existing run?** The `eval/<story>__<sid>/` dirs hold *copies*
> of the PNGs (made by `fetch_outputs.py`) plus the `manifest.json` and `report.md`.
> If the outputs were re-generated since the last eval, those copies and reports are
> **stale** — re-run step 2 (`fetch_outputs.py`) for every story to refresh the copied
> PNGs + manifests against the latest outputs, then re-judge (step 3) before serving.
> Compare PNG mtimes under `outputs/<user>/<story>/outputs/` against the `eval/` copies
> if unsure which is newer.

### 5. Report back

Give the user: the per-story verdicts (ship / revise / regenerate), the headline
issues, any stories that **failed to generate** (from `run.json`), and the review
URL.

## Notes

- **Skill boundaries.** This skill drives generation + orchestration and opens
  the UI. The **rubric and report format are owned by `prompt-eval`** — don't
  fork them here. When a fix is warranted, edit `imagegen/prompts/<story>.json`
  via `story-prompts` (or `character.json` via `character-config`), regenerate
  that story (step 1 with `--stories`), and re-judge.
- `eval_runs/` is gitignored (generated artifacts: images, manifests, reports).
- No GCP creds, no emulator, no Application stack are involved in local mode —
  only a live ComfyUI. The same `fetch_outputs.py` still serves the GCS-backed
  `prompt-eval` flow when `--local-root` is omitted.
