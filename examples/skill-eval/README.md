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
`skill` arm, and bootstraps each template with a local `.venv` for explicit
host mode. Each template also includes `.windtunnel/terminus-bootstrap.sh`,
which docker mode runs inside the container to rebuild a Linux `.venv` and
point it at the read-only repo mount. Agent commands such as
`uv run wt validate ...` use the Wind Tunnel code under test without
downloading the package.

## Run Each Arm

Use generic endpoint/model placeholders appropriate for your Terminus setup.
Docker isolation is the Terminus default; the commands below set it explicitly
for readability. The local pack is loaded with
`--pack-source examples/skill-eval/pack.py:PACK` and selected with
`--pack skill_eval`.

```bash
WT_TERMINUS_ISOLATION=docker \
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
WT_TERMINUS_ISOLATION=docker \
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
WT_TERMINUS_ISOLATION=docker \
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

For local debugging without Docker, opt into host execution explicitly:

```bash
WT_TERMINUS_ISOLATION=host \
WT_TERMINUS_MODEL=<provider>/<model> \
WT_TERMINUS_API_BASE=https://llm-gateway.example.invalid/v1 \
WT_TERMINUS_WORKSPACE_TEMPLATE=examples/skill-eval/templates/skill \
uv run --python 3.12 --extra terminus wt run \
  --runtime terminus \
  --pack-source examples/skill-eval/pack.py:PACK \
  --pack skill_eval \
  --label skill-host \
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

## First results (2026-07-06, v0.6.0)

One live run of the full matrix, executed the day this pack shipped:
**qwen3.6:35b** (a3b MoE, Q4_K_M) served by Ollama through its
OpenAI-compatible endpoint, docker isolation, one run per cell. n=1 —
treat everything below as observations from single runs, not conclusions;
the pack exists so you can grow the sample.

| scenario | skill | agents-md | bare |
|---|---|---|---|
| cli-lookup | PASS · 21 cmds · 85s | PASS · 14 cmds · 53s | PASS · 38 cmds · 149s |
| build-envelope | PASS · 18 cmds · 94s | PASS · 27 cmds · 115s | PASS · 12 cmds · 61s |
| import-and-author | PASS · 41 cmds · 13m · self-stopped | work complete, **never self-terminated** — externally stopped at ~65m | PASS · 45 cmds · 20m · self-stopped |

What single runs were able to show:

- **Outcomes mostly tie.** A discoverable CLI plus a strict validator make
  these tasks completable without any documentation: the agents-md arm
  passed `build-envelope` with **zero** documentation reads by iterating
  against `wt validate --strict`'s error messages until the envelope was
  correct. Well-written error messages function as interactive
  documentation, whether you meant them to or not.
- **The catastrophic cost divergence was about termination, not task
  knowledge.** The agents-md `import-and-author` run authored the outcome
  fact correctly early, then spent the rest of an hour re-verifying — an
  imported scenario is a deliberately failing stub, and without the
  reference that says so, no verification it could run would ever look
  "done". Its preserved trajectory: 51 model calls, **618k prompt tokens /
  21k completion**, terminated only by an external budget. The bare arm
  self-terminated on the same task, so with n=1 this cannot be attributed
  to the arm — what it does demonstrate is that agents signal their own
  completion, so a model's confidence calibration is part of your cost
  model, and external budgets are not optional.
- **Where the skill visibly paid:** the skill arm consulted its references
  in every scenario (`docs_read=True` in the trajectory annotations) and
  had the fastest completion of the heavyweight workflow (13m vs 20m);
  the bare arm paid ~3x on the pure-lookup task (38 vs 13–21 commands)
  rediscovering tooling by trial. The consultation annotation is what
  distinguishes "had docs and used them" from "succeeded anyway" — that
  difference is invisible in pass rates.

Cost accounting for all cells (not just the externally-stopped one) needs
runtime-reported token usage in the trace itself — the run above is why
that, along with budget-based termination and a recorded termination
reason, is queued as follow-up work.
