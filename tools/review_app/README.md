# Story eval review app

A tiny, dependency-free (Python stdlib `http.server`) web UI for reviewing a
**local story-eval run** — the artifacts produced by `.claude/skills/local-batch-eval/generate_stories.py`
plus the `local-batch-eval` / `image-eval` skills.

For each story it shows, on one scrollable page:

- the **input photo** + run metadata (age, model, when generated),
- every panel's **actual prompt** sent to ComfyUI (from `manifest.json`, which
  prefers the worker's logged prompt),
- the **output image** for each panel (one image per panel on the live template;
  click to zoom),
- the **eval result** — the vision judge's per-panel notes pulled out of
  `report.md` and shown next to that panel, plus the verdict, summary, and
  recommended prompt fixes.

## Run

```bash
~/python_env/torch-env/bin/python tools/review_app/server.py \
    --run-dir eval_runs/latest --port 8000
# open http://127.0.0.1:8000/
```

`--run-dir` is a run directory laid out as:

```
<run-dir>/
├── run.json                       # batch metadata (optional)
├── input.<ext>                    # the input photo
├── outputs/<user>/<story>/outputs/<i>.png
└── eval/<story>__<story_id>/      # one per story
    ├── manifest.json              # from image-eval/fetch_outputs.py
    ├── report.md                  # from the image-eval rubric (vision judge)
```

It re-reads the run dir on every request, so regenerating or re-judging shows up
on refresh — no restart needed. It only serves files under `<run-dir>` (the
input photo and the image paths listed in each story manifest); path traversal
is rejected.

See the `local-batch-eval` skill for the end-to-end generate → eval → review
flow.
