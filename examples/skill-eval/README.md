# Wind Tunnel Skill-Eval Example

This example benches whether Wind Tunnel's generated agent skill helps a
terminal agent use Wind Tunnel itself. The three arms run identical scenarios
and differ only by workspace documentation:

- `skill`: root `AGENTS.md` plus `.agents/skills/windtunnel/`
- `agents-md`: root `AGENTS.md` only
- `bare`: no documentation

The scenarios score deterministic workspace artifacts through a
`StateProbe`; no LLM judge is used.

## Prepare Templates

Run from the repository root:

```bash
python3 examples/skill-eval/prepare.py
```

`prepare.py` rebuilds `examples/skill-eval/templates/` from the committed
`base/` fixture, copies the current generated `skills/windtunnel/` into the
`skill` arm, and bootstraps each template with a local `.venv`. The `.venv`
contains a `wt` console script plus a `.pth` pointer to this checkout, so agent
commands such as `uv run wt validate ...` use the Wind Tunnel code under test
without downloading the package.

## Run Each Arm

Use generic endpoint/model placeholders appropriate for your Terminus setup.
The local pack is loaded with `--pack-source examples/skill-eval/pack.py:PACK`
and selected with `--pack skill_eval`.

```bash
WT_TERMINUS_MODEL=<provider>/<model> \
WT_TERMINUS_API_BASE=https://llm-gateway.example.invalid/v1 \
WT_TERMINUS_WORKSPACE_TEMPLATE=examples/skill-eval/templates/skill \
uv run --python 3.12 --extra terminus wt run \
  --runtime terminus \
  --pack-source examples/skill-eval/pack.py:PACK \
  --pack skill_eval \
  --label skill \
  --runs 1 \
  --runs-dir examples/skill-eval/runs
```

```bash
WT_TERMINUS_MODEL=<provider>/<model> \
WT_TERMINUS_API_BASE=https://llm-gateway.example.invalid/v1 \
WT_TERMINUS_WORKSPACE_TEMPLATE=examples/skill-eval/templates/agents-md \
uv run --python 3.12 --extra terminus wt run \
  --runtime terminus \
  --pack-source examples/skill-eval/pack.py:PACK \
  --pack skill_eval \
  --label agents-md \
  --runs 1 \
  --runs-dir examples/skill-eval/runs
```

```bash
WT_TERMINUS_MODEL=<provider>/<model> \
WT_TERMINUS_API_BASE=https://llm-gateway.example.invalid/v1 \
WT_TERMINUS_WORKSPACE_TEMPLATE=examples/skill-eval/templates/bare \
uv run --python 3.12 --extra terminus wt run \
  --runtime terminus \
  --pack-source examples/skill-eval/pack.py:PACK \
  --pack skill_eval \
  --label bare \
  --runs 1 \
  --runs-dir examples/skill-eval/runs
```

## Compare And Iterate

```bash
uv run wt compare --runs examples/skill-eval/runs --labels skill agents-md bare
```

Re-score saved traces after editing scenario scoring:

```bash
uv run wt rescore \
  --runs examples/skill-eval/runs \
  --pack-source examples/skill-eval/pack.py:PACK \
  --pack skill_eval
```

Add `--write` to refresh `.score.json` sidecars after scorer changes.

## Scenario Verification

- `cli-lookup`: `test -f answer.txt && grep -q 'wt rescore' answer.txt`
- `build-envelope`: `uv run wt validate --strict out.wtin.json`
- `import-and-author`: checks the `wt import` artifacts, verifies the generated
  `scenario.py` no longer contains the placeholder, and runs
  `uv run wt validate incident.wtin.json`

Trajectory scoring also annotates whether terminal commands read `AGENTS.md` or
`.agents/skills/`, but that annotation never fails a run.
