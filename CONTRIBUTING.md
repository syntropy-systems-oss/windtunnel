# Contributing to Wind Tunnel

## Dev setup

```bash
git clone https://github.com/syntropy-systems-oss/windtunnel
cd windtunnel
uv sync                  # creates .venv, installs editable + the dev group
```

## Contribution terms

Unless explicitly stated otherwise, contributions intentionally submitted for
inclusion in Wind Tunnel are licensed under the Apache License 2.0, consistent
with section 5 of that license. Contributors retain ownership of their
contributions.

By submitting a contribution, you represent that you have the right to license
it to the project. Do not submit confidential, proprietary, or improperly
licensed material. AI-assisted development tools are permitted, but the
contributor remains responsible for the contribution's correctness, security,
and provenance.

Please read the [Code of Conduct](CODE_OF_CONDUCT.md) and the project's
[governance model](GOVERNANCE.md) before contributing.

## Running checks

```bash
uv run pytest -m "not integration" -q   # the unit suite — fast, no infrastructure
uv run ruff check windtunnel/ tests/
```

The `integration` marker is reserved for tests that need a live agent stack;
the unit suite must always pass with no network, no Docker, no model.

## The one rule that is not negotiable

**Scenarios never import platform-specific types.** If you can't write a
scenario without importing `windtunnel.runtimes.*`, the SPI has leaked — fix
the contract, not the scenario. This is enforced by
`tests/test_import_invariants.py`; a PR that weakens that test needs a very
good story.

Two corollaries:

- Scenario `must_call` / `forbidden_calls` use **canonical bare tool names**
  (`client_lookup`), never platform-decorated ones — the trajectory evaluator
  matches decorations (`mcp_<server>_<prefix>_client_lookup`, dotted forms)
  for you.
- New runtime drivers ship as **separate packages** registering the
  `windtunnel.runtimes` entry-point group (see
  [docs/writing-a-runtime.md](docs/writing-a-runtime.md)); they don't get
  merged into `windtunnel/runtimes/`. The in-tree `in_memory` runtime is the
  only exception — it's the zero-infrastructure reference. Scenario
  dimensions have the same seam: external dims ship as `ScenarioPack`s
  registering the `windtunnel.scenario_packs` group (see
  [docs/writing-a-scenario.md](docs/writing-a-scenario.md#shipping-a-scenario-pack)).

## Code style

- Python 3.11+, ruff (`E,F,W,I,UP`, line length 100), full type hints on
  public API surfaces.
- Dataclasses for data shapes; Protocols for contracts.
- Docstrings explain *why* and record behavioral contracts — the evaluator
  semantics in `windtunnel/api/evaluators.py` are the house style to imitate.
- Tests: one behavior per test, names that read as sentences, and when you
  fix a bug, the regression test's docstring says what broke.

## Adding a scenario or dimension

Read [docs/writing-a-scenario.md](docs/writing-a-scenario.md). A new dim
package needs `scenarios.py`, a mock MCP server (build on
`windtunnel.mcp.fastmcp.server.LoggingFastMCP` — call logging and failure
injection come free), a synthetic DB, and a `PACK` (a
`windtunnel.api.ScenarioPack` in the dim's `__init__.py`, listed in
`windtunnel/scenarios/__init__.py`'s `builtin_packs()`). Keep synthetic data
fictional: fake names, `.example` domains.

## Docs

The docs site is MkDocs Material over `docs/`, deployed to GitHub Pages by
`.github/workflows/docs.yml` on every push to `main`. Preview locally:

```bash
uv run --group docs mkdocs serve
```

CI builds the site with `--strict`, so a broken nav entry or dead internal
link fails the build — treat docs like code.

## Releases

Releases are automated with [release-please](https://github.com/googleapis/release-please)
and **conventional commits** — commit messages drive the version:

- `fix:` → patch, `feat:` → minor, `feat!:`/`BREAKING CHANGE:` → major
  (pre-1.0, majors are taken as minors).
- On every push to `main`, release-please maintains a release PR that
  accumulates the CHANGELOG and the next version (in `pyproject.toml` +
  `.release-please-manifest.json`). **Merging that PR is the release**: in
  one `.github/workflows/release-please.yml` run it tags `vX.Y.Z`, creates
  the GitHub release, and — gated on `release_created` — a chained `publish`
  job builds with `uv build` and uploads to PyPI via Trusted Publishing.
  No API tokens, and no human in the loop.
- Publishing is a *job in the same run*, not a separate workflow keyed off
  the `release: published` event. release-please cuts the release with the
  default `GITHUB_TOKEN`, and GitHub refuses to trigger workflows from
  `GITHUB_TOKEN`-authored events, so an event-driven publish workflow never
  fires for a bot-cut release. (That gap is why v0.4.0 was tagged but never
  reached PyPI — it was the first release published entirely by the bot; the
  earlier ones were cut by a human, which does fire the event.)

Pre-1.0: breaking changes are allowed but must be a `feat!:` so the
changelog calls them out. The config encodes the pre-1.0 stance:

- `bump-minor-pre-major: true` — a `feat!:` bumps 0.x → 0.(x+1), never
  to 1.0.0. Going 1.0 is a deliberate act, not a commit-message accident.
- `prerelease: true` — GitHub releases are flagged "pre-release" while
  we're 0.x. This is display-only; PyPI never sees it.
- We deliberately do NOT use release-please's prerelease *versioning*
  (rc/beta suffixes): it emits semver-style `0.2.0-rc.1`, which is not a
  valid PEP 440 version — PyPI wants `0.2.0rc1`. Plain 0.x versions are
  pip-installable and honest about stability.
- `include-component-in-tag: false` — tags are plain `vX.Y.Z`, not
  `windtunnel-vX.Y.Z`, so the manual first tag and release-please's tags
  agree.

**At 1.0**: remove `bump-minor-pre-major` and `prerelease` from
`release-please-config.json`. That's the whole graduation ceremony.

### One-time repository setup (maintainers)

Things the workflows need that only the GitHub/PyPI UIs can grant:

1. **Pages**: repo Settings → Pages → Source = "GitHub Actions".
2. **Release PRs**: Settings → Actions → General → enable
   "Allow GitHub Actions to create and approve pull requests"
   (release-please needs it).
3. **PyPI Trusted Publisher**: on PyPI, add a publisher for project
   `windtunnel-bench` → owner `syntropy-systems-oss`, repo `windtunnel`,
   workflow `release-please.yml`, environment `pypi`. (For a not-yet-existing
   project, use PyPI's "pending publisher" flow.) Then create the `pypi`
   environment in repo Settings → Environments. The bare name
   `windtunnel` is a squatted empty registration on PyPI; a PEP 541
   transfer request is pending — if granted, the distribution may be
   renamed (the import name `windtunnel` is unaffected either way).
4. **First release**: release-please versions *changes since the last
   release*, so the initial `v0.1.0` is cut by hand once:
   `gh release create v0.1.0 --title "v0.1.0" --generate-notes` — the
   publish workflow takes it from there. Everything after is release PRs.
