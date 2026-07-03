"""Tests for the static report CLI.

TDD red phase — these tests define the contract for:
  1. CLI entry point: `wt report` command exists and is invokable
  2. HTML generation: scenario matrix, header, drill-down cells, failure_cost
  3. Diff view: regression detection between two label sets
  4. Markdown format: terminal-readable summary via --format markdown
  5. failure_cost annotations: severity ordering visible in output
  6. No HTML-in-table-cell artifacts (no <br><sub> inside td elements)

Design calls made autonomously:
  - Diff view is an interactive toggle in the single HTML file (not a separate page).
    Two <select> dropdowns let the user pick labels A and B; JS highlights changed cells.
  - Data is embedded as a JSON island: <script type="application/json" id="bench-data">
  - Jinja2 is NOT used — the HTML is generated via stdlib string building + a single
    template string. Keeps the package dep-free.
  - argparse is used (not click) — stdlib only, consistent with the rest of the package.
  - `runs/` directory layout consumed: runs/<scenario_id>/<agent_id>/<variant_id>/.../*.json
    The report groups by (scenario_id, variant_id) and emits one column per variant.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any

from windtunnel.api.score import FailureCost, LayerResult, Score
from windtunnel.api.trace import Trace, Turn, compute_hash, save_trace

# ─── Fixture helpers ─────────────────────────────────────────────────────────


def _ts(s: str = "2026-05-27T12:00:00+00:00") -> datetime:
    return datetime.fromisoformat(s)


def _turn(
    role: str = "assistant",
    content: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    tool_results: list[dict[str, Any]] | None = None,
    latency_ms: float = 50.0,
) -> Turn:
    return Turn(
        role=role,
        content=content,
        tool_calls=tool_calls or [],
        tool_results=tool_results or [],
        latency_ms=latency_ms,
    )


def _make_trace(
    scenario_id: str = "sc_alpha",
    variant_id: str = "baseline",
    agent_id: str = "agent-test",
    started_at: datetime | None = None,
) -> Trace:
    return Trace(
        scenario_id=scenario_id,
        agent_id=agent_id,
        variant_id=variant_id,
        model="test-model",
        quant="q4",
        sampler={},
        started_at=started_at or _ts(),
        finished_at=_ts("2026-05-27T12:00:30+00:00"),
        turns=[
            _turn(role="user", content="question"),
            _turn(
                role="assistant",
                content="",
                tool_calls=[{
                    "id": "call_0",
                    "type": "function",
                    "function": {"name": "client_lookup", "arguments": "{}"},
                }],
            ),
            _turn(role="tool", content='{"result": "ok"}'),
            _turn(role="assistant", content="The answer is 42."),
        ],
        tool_schema_hash=compute_hash("[]"),
        worker_warnings=[],
    )


def _make_score(
    outcome_pass: bool = True,
    trajectory_pass: bool = True,
    constraint_pass: bool = True,
    robustness_pass: bool = True,
    severity: str = "low",
    customer_visible: bool = False,
    reversible: bool = True,
) -> Score:
    return Score(
        outcome=LayerResult(passed=outcome_pass, detail="outcome detail"),
        trajectory=LayerResult(passed=trajectory_pass, detail="trajectory detail"),
        constraint=LayerResult(passed=constraint_pass, detail="constraint detail"),
        robustness=LayerResult(passed=robustness_pass, detail="robustness detail"),
        failure_cost=FailureCost(
            severity=severity,  # type: ignore[arg-type]
            customer_visible=customer_visible,
            reversible=reversible,
        ),
    )


def _write_run(
    tmp_path: Path,
    trace: Trace,
    score: Score,
    *,
    base_runs: Path | None = None,
) -> Path:
    """Write a trace JSON + sidecar score JSON into the runs/ layout.

    Layout: <runs>/<scenario_id>/<agent_id>/<variant_id>/<model>/<quant>/<ts>_<id>.json
    Score lives alongside as <ts>_<id>.score.json.
    """
    from windtunnel.api.trace import storage_path

    runs_dir = base_runs or (tmp_path / "runs")
    p = storage_path(trace, base_dir=runs_dir)
    save_trace(trace, p)
    # Write score sidecar
    score_path = p.with_suffix(".score.json")
    score_path.write_text(
        json.dumps({
            "outcome": {"passed": score.outcome.passed, "detail": score.outcome.detail},
            "trajectory": {"passed": score.trajectory.passed, "detail": score.trajectory.detail},
            "constraint": {"passed": score.constraint.passed, "detail": score.constraint.detail},
            "robustness": {"passed": score.robustness.passed, "detail": score.robustness.detail},
            "failure_cost": {
                "severity": score.failure_cost.severity,
                "customer_visible": score.failure_cost.customer_visible,
                "reversible": score.failure_cost.reversible,
                "side_effect_performed": score.failure_cost.side_effect_performed,
            },
        }, indent=2),
        encoding="utf-8",
    )
    return p


def _build_synthetic_runs(tmp_path: Path) -> Path:
    """Build a synthetic runs/ dir: 3 variants × 4 scenarios.

    Variants: baseline, variant_b, variant_c
    Scenarios: sc_alpha, sc_beta, sc_gamma, sc_delta

    sc_alpha:  baseline=PASS, variant_b=PASS,  variant_c=PASS
    sc_beta:   baseline=PASS, variant_b=FAIL,  variant_c=PASS
    sc_gamma:  baseline=FAIL, variant_b=FAIL,  variant_c=PASS
    sc_delta:  baseline=PASS, variant_b=PASS,  variant_c=FAIL  (regression in c)
    """
    runs_dir = tmp_path / "runs"

    matrix = {
        "sc_alpha":  {"baseline": True,  "variant_b": True,  "variant_c": True},
        "sc_beta":   {"baseline": True,  "variant_b": False, "variant_c": True},
        "sc_gamma":  {"baseline": False, "variant_b": False, "variant_c": True},
        "sc_delta":  {"baseline": True,  "variant_b": True,  "variant_c": False},
    }

    for scenario_id, variants in matrix.items():
        for variant_id, passes in variants.items():
            trace = _make_trace(scenario_id=scenario_id, variant_id=variant_id)
            score = _make_score(outcome_pass=passes)
            _write_run(tmp_path, trace, score, base_runs=runs_dir)

    return runs_dir


# ─── Import targets ───────────────────────────────────────────────────────────

def _import_report():
    """Import the report module — will fail until implemented."""
    from windtunnel import report  # noqa: PLC0415
    return report


def _import_cli():
    """Import the CLI module — will fail until implemented."""
    from windtunnel import cli  # noqa: PLC0415
    return cli


# ─── 1. CLI entry point ───────────────────────────────────────────────────────


class TestCLIEntryPoint:
    """The `wt report` subcommand must exist and be invokable."""

    def test_cli_module_importable(self):
        cli = _import_cli()
        assert cli is not None

    def test_cli_has_main(self):
        cli = _import_cli()
        assert callable(getattr(cli, "main", None))

    def test_report_subcommand_help(self, tmp_path: Path):
        """agent-bench report --help exits 0 without error."""
        result = subprocess.run(
            [sys.executable, "-m", "windtunnel.cli", "report", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "report" in result.stdout.lower()

    def test_report_produces_html(self, tmp_path: Path):
        """agent-bench report --runs <dir> --out <file> produces an HTML file."""
        runs_dir = _build_synthetic_runs(tmp_path)
        out_file = tmp_path / "report.html"
        result = subprocess.run(
            [
                sys.executable, "-m", "windtunnel.cli",
                "report",
                "--runs", str(runs_dir),
                "--out", str(out_file),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert out_file.exists(), "report.html was not created"

    def test_report_markdown_to_stdout(self, tmp_path: Path):
        """agent-bench report --format markdown prints to stdout."""
        runs_dir = _build_synthetic_runs(tmp_path)
        result = subprocess.run(
            [
                sys.executable, "-m", "windtunnel.cli",
                "report",
                "--runs", str(runs_dir),
                "--format", "markdown",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert len(result.stdout) > 0


# ─── 2. HTML generation — structure ───────────────────────────────────────────


class TestHTMLStructure:
    """The generated HTML must have the required structural elements."""

    def _gen_html(self, tmp_path: Path) -> str:
        runs_dir = _build_synthetic_runs(tmp_path)
        out_file = tmp_path / "report.html"
        report = _import_report()
        report.generate_html(runs_dir=runs_dir, out_path=out_file)
        return out_file.read_text(encoding="utf-8")

    def test_html_is_valid_html5_doctype(self, tmp_path: Path):
        content = self._gen_html(tmp_path)
        assert content.strip().startswith("<!DOCTYPE html>") or content.strip().startswith("<!doctype html>")

    def test_html_has_json_island(self, tmp_path: Path):
        """Data must be embedded as a JSON island for client-side rendering."""
        content = self._gen_html(tmp_path)
        assert 'id="bench-data"' in content
        assert 'type="application/json"' in content

    def test_json_island_is_valid_json(self, tmp_path: Path):
        content = self._gen_html(tmp_path)
        # Extract JSON island
        m = re.search(
            r'<script[^>]+id="bench-data"[^>]*>(.*?)</script>',
            content,
            re.DOTALL,
        )
        assert m is not None, "bench-data script tag not found"
        data = json.loads(m.group(1))
        assert isinstance(data, dict)

    def test_json_island_has_scenarios(self, tmp_path: Path):
        content = self._gen_html(tmp_path)
        m = re.search(
            r'<script[^>]+id="bench-data"[^>]*>(.*?)</script>',
            content,
            re.DOTALL,
        )
        data = json.loads(m.group(1))
        assert "scenarios" in data
        assert len(data["scenarios"]) == 4  # sc_alpha, sc_beta, sc_gamma, sc_delta

    def test_json_island_has_variants(self, tmp_path: Path):
        content = self._gen_html(tmp_path)
        m = re.search(
            r'<script[^>]+id="bench-data"[^>]*>(.*?)</script>',
            content,
            re.DOTALL,
        )
        data = json.loads(m.group(1))
        assert "variants" in data
        assert set(data["variants"]) == {"baseline", "variant_b", "variant_c"}

    def test_json_island_has_run_timestamp(self, tmp_path: Path):
        content = self._gen_html(tmp_path)
        m = re.search(
            r'<script[^>]+id="bench-data"[^>]*>(.*?)</script>',
            content,
            re.DOTALL,
        )
        data = json.loads(m.group(1))
        assert "latest_run_ts" in data

    def test_html_has_inline_css(self, tmp_path: Path):
        """Report must be self-contained — no external stylesheet links."""
        content = self._gen_html(tmp_path)
        assert "<style>" in content
        # Must NOT have external CSS links
        assert 'rel="stylesheet"' not in content

    def test_html_has_inline_js(self, tmp_path: Path):
        """Report must be self-contained — no external JS links."""
        content = self._gen_html(tmp_path)
        assert "<script" in content
        # Must NOT load from a CDN or external URL
        assert "https://" not in content.replace('"https://', '"').replace("'https://", "'")
        # (allow https in data content but not as a src= attribute)
        assert not re.search(r'<script[^>]+src=["\']https?://', content)

    def test_no_external_resources(self, tmp_path: Path):
        """No src= or href= pointing to external URLs."""
        content = self._gen_html(tmp_path)
        # Check that no tags load external resources
        assert not re.search(r'src=["\']https?://', content)
        assert not re.search(r'href=["\']https?://', content)


# ─── 3. HTML — header section ─────────────────────────────────────────────────


class TestHTMLHeader:
    def _gen_html(self, tmp_path: Path) -> str:
        runs_dir = _build_synthetic_runs(tmp_path)
        out_file = tmp_path / "report.html"
        report = _import_report()
        report.generate_html(runs_dir=runs_dir, out_path=out_file)
        return out_file.read_text(encoding="utf-8")

    def test_header_has_scenario_count(self, tmp_path: Path):
        content = self._gen_html(tmp_path)
        # 4 scenarios in our fixture
        assert "4" in content

    def test_header_has_pass_rate_mention(self, tmp_path: Path):
        content = self._gen_html(tmp_path)
        # Some form of pass rate / percent
        assert "pass" in content.lower() or "%" in content

    def test_header_mentions_four_layers(self, tmp_path: Path):
        """Header or summary must mention the four scoring layers."""
        content = self._gen_html(tmp_path)
        lower = content.lower()
        assert "outcome" in lower
        assert "trajectory" in lower
        assert "constraint" in lower
        assert "robustness" in lower


# ─── 4. HTML — scenario matrix cells ──────────────────────────────────────────


class TestHTMLScenarioMatrix:
    def _gen_data(self, tmp_path: Path) -> dict[str, Any]:
        runs_dir = _build_synthetic_runs(tmp_path)
        out_file = tmp_path / "report.html"
        report = _import_report()
        report.generate_html(runs_dir=runs_dir, out_path=out_file)
        content = out_file.read_text(encoding="utf-8")
        m = re.search(
            r'<script[^>]+id="bench-data"[^>]*>(.*?)</script>',
            content,
            re.DOTALL,
        )
        return json.loads(m.group(1))

    def test_all_scenario_ids_in_data(self, tmp_path: Path):
        data = self._gen_data(tmp_path)
        scenario_ids = {s["scenario_id"] for s in data["scenarios"]}
        assert "sc_alpha" in scenario_ids
        assert "sc_beta" in scenario_ids
        assert "sc_gamma" in scenario_ids
        assert "sc_delta" in scenario_ids

    def test_cell_has_verdict(self, tmp_path: Path):
        data = self._gen_data(tmp_path)
        # Find sc_alpha / baseline cell
        sc = next(s for s in data["scenarios"] if s["scenario_id"] == "sc_alpha")
        cell = sc["cells"]["baseline"]
        assert "verdict" in cell
        assert cell["verdict"] in ("PASS", "FAIL", "PASS_WITH_VARIANCE")

    def test_cell_has_tool_call_count(self, tmp_path: Path):
        data = self._gen_data(tmp_path)
        sc = next(s for s in data["scenarios"] if s["scenario_id"] == "sc_alpha")
        cell = sc["cells"]["baseline"]
        assert "tool_call_count" in cell
        assert isinstance(cell["tool_call_count"], int)

    def test_cell_has_four_layer_breakdown(self, tmp_path: Path):
        """Each cell must carry per-layer pass/fail info."""
        data = self._gen_data(tmp_path)
        sc = next(s for s in data["scenarios"] if s["scenario_id"] == "sc_alpha")
        cell = sc["cells"]["baseline"]
        assert "layers" in cell
        layers = cell["layers"]
        assert "outcome" in layers
        assert "trajectory" in layers
        assert "constraint" in layers
        assert "robustness" in layers

    def test_cell_known_pass(self, tmp_path: Path):
        """sc_alpha/baseline is PASS in our fixture."""
        data = self._gen_data(tmp_path)
        sc = next(s for s in data["scenarios"] if s["scenario_id"] == "sc_alpha")
        assert sc["cells"]["baseline"]["verdict"] == "PASS"

    def test_cell_known_fail(self, tmp_path: Path):
        """sc_beta/variant_b is FAIL in our fixture."""
        data = self._gen_data(tmp_path)
        sc = next(s for s in data["scenarios"] if s["scenario_id"] == "sc_beta")
        assert sc["cells"]["variant_b"]["verdict"] == "FAIL"

    def test_no_html_in_table_cell_text(self, tmp_path: Path):
        """Cells must NOT contain <br> or <sub> tags inside <td> elements.

        This guards against a known prototype artifact — an earlier report
        generator used <br><sub> inside table cells for tags/chain-count.
        The new report must not do this: use proper table structure or JS rendering.
        """
        runs_dir = _build_synthetic_runs(tmp_path)
        out_file = tmp_path / "report.html"
        report = _import_report()
        report.generate_html(runs_dir=runs_dir, out_path=out_file)
        content = out_file.read_text(encoding="utf-8")

        # Extract all <td>...</td> blocks and check for <br> or <sub>
        td_blocks = re.findall(r"<td[^>]*>(.*?)</td>", content, re.DOTALL | re.IGNORECASE)
        for block in td_blocks:
            assert "<br>" not in block.lower(), f"<br> found inside <td>: {block[:100]}"
            assert "<sub>" not in block.lower(), f"<sub> found inside <td>: {block[:100]}"

    def test_failure_cost_in_cell_data(self, tmp_path: Path):
        """Each cell must carry failure_cost so the UI can weight regressions."""
        data = self._gen_data(tmp_path)
        sc = next(s for s in data["scenarios"] if s["scenario_id"] == "sc_alpha")
        cell = sc["cells"]["baseline"]
        assert "failure_cost" in cell
        fc = cell["failure_cost"]
        assert "severity" in fc
        assert fc["severity"] in ("low", "medium", "high", "critical")


# ─── 5. HTML — failure_cost annotations ──────────────────────────────────────


class TestFailureCostAnnotations:
    """failure_cost severity must be visible with ordering: critical > high > medium > low."""

    def _build_severity_runs(self, tmp_path: Path) -> Path:
        """Build runs with different severity levels per scenario."""
        runs_dir = tmp_path / "runs"
        severities = {
            "sc_low":      ("low",      False, True),
            "sc_medium":   ("medium",   False, True),
            "sc_high":     ("high",     True,  False),
            "sc_critical": ("critical", True,  False),
        }
        for scenario_id, (sev, cv, rev) in severities.items():
            trace = _make_trace(scenario_id=scenario_id, variant_id="baseline")
            # critical/high scenarios fail to make them visible as regressions
            passes = sev in ("low", "medium")
            score = _make_score(
                outcome_pass=passes,
                severity=sev,
                customer_visible=cv,
                reversible=rev,
            )
            _write_run(tmp_path, trace, score, base_runs=runs_dir)
        return runs_dir

    def _gen_data(self, tmp_path: Path) -> dict[str, Any]:
        runs_dir = self._build_severity_runs(tmp_path)
        out_file = tmp_path / "report.html"
        report = _import_report()
        report.generate_html(runs_dir=runs_dir, out_path=out_file)
        content = out_file.read_text(encoding="utf-8")
        m = re.search(
            r'<script[^>]+id="bench-data"[^>]*>(.*?)</script>',
            content,
            re.DOTALL,
        )
        return json.loads(m.group(1))

    def test_severity_preserved_in_data(self, tmp_path: Path):
        data = self._gen_data(tmp_path)
        severities = {}
        for sc in data["scenarios"]:
            for variant, cell in sc["cells"].items():
                severities[sc["scenario_id"]] = cell["failure_cost"]["severity"]

        assert severities["sc_low"] == "low"
        assert severities["sc_medium"] == "medium"
        assert severities["sc_high"] == "high"
        assert severities["sc_critical"] == "critical"

    def test_customer_visible_in_data(self, tmp_path: Path):
        data = self._gen_data(tmp_path)
        for sc in data["scenarios"]:
            for variant, cell in sc["cells"].items():
                if sc["scenario_id"] in ("sc_high", "sc_critical"):
                    assert cell["failure_cost"]["customer_visible"] is True
                else:
                    assert cell["failure_cost"]["customer_visible"] is False

    def test_reversible_in_data(self, tmp_path: Path):
        data = self._gen_data(tmp_path)
        for sc in data["scenarios"]:
            for variant, cell in sc["cells"].items():
                if sc["scenario_id"] in ("sc_high", "sc_critical"):
                    assert cell["failure_cost"]["reversible"] is False
                else:
                    assert cell["failure_cost"]["reversible"] is True

    def test_html_marks_critical_distinctly(self, tmp_path: Path):
        """Critical failures must be visually distinct from low in the HTML."""
        runs_dir = self._build_severity_runs(tmp_path)
        out_file = tmp_path / "report.html"
        report = _import_report()
        report.generate_html(runs_dir=runs_dir, out_path=out_file)
        content = out_file.read_text(encoding="utf-8")
        # The word "critical" must appear in the HTML (in data or in a class/label)
        assert "critical" in content.lower()


# ─── 6. HTML — diff view ─────────────────────────────────────────────────────


class TestDiffView:
    """The diff view surfaces regressions and improvements between two variants."""

    def _build_regression_runs(self, tmp_path: Path) -> tuple[Path, str, str]:
        """Build runs with one known regression from baseline → variant_b.

        sc_alpha: baseline=PASS, variant_b=PASS  (no change)
        sc_beta:  baseline=PASS, variant_b=FAIL   (regression)
        sc_gamma: baseline=FAIL, variant_b=PASS   (improvement)
        """
        runs_dir = tmp_path / "runs"

        cases = [
            ("sc_alpha", "baseline", True),
            ("sc_alpha", "variant_b", True),
            ("sc_beta",  "baseline", True),
            ("sc_beta",  "variant_b", False),   # regression
            ("sc_gamma", "baseline", False),
            ("sc_gamma", "variant_b", True),    # improvement
        ]
        for scenario_id, variant_id, passes in cases:
            trace = _make_trace(scenario_id=scenario_id, variant_id=variant_id)
            score = _make_score(outcome_pass=passes)
            _write_run(tmp_path, trace, score, base_runs=runs_dir)

        return runs_dir, "baseline", "variant_b"

    def _get_diff(
        self,
        tmp_path: Path,
        runs_dir: Path,
        label_a: str,
        label_b: str,
    ) -> list[dict[str, Any]]:
        report = _import_report()
        return report.compute_diff(runs_dir=runs_dir, label_a=label_a, label_b=label_b)

    def test_diff_detects_regression(self, tmp_path: Path):
        runs_dir, label_a, label_b = self._build_regression_runs(tmp_path)
        diff = self._get_diff(tmp_path, runs_dir, label_a, label_b)
        regressions = [d for d in diff if d["direction"] == "regression"]
        assert len(regressions) == 1
        assert regressions[0]["scenario_id"] == "sc_beta"

    def test_diff_detects_improvement(self, tmp_path: Path):
        runs_dir, label_a, label_b = self._build_regression_runs(tmp_path)
        diff = self._get_diff(tmp_path, runs_dir, label_a, label_b)
        improvements = [d for d in diff if d["direction"] == "improvement"]
        assert len(improvements) == 1
        assert improvements[0]["scenario_id"] == "sc_gamma"

    def test_diff_no_change_excluded(self, tmp_path: Path):
        """sc_alpha is PASS→PASS — it must NOT appear in the diff."""
        runs_dir, label_a, label_b = self._build_regression_runs(tmp_path)
        diff = self._get_diff(tmp_path, runs_dir, label_a, label_b)
        scenario_ids = {d["scenario_id"] for d in diff}
        assert "sc_alpha" not in scenario_ids

    def test_diff_result_has_required_fields(self, tmp_path: Path):
        runs_dir, label_a, label_b = self._build_regression_runs(tmp_path)
        diff = self._get_diff(tmp_path, runs_dir, label_a, label_b)
        for item in diff:
            assert "scenario_id" in item
            assert "direction" in item  # "regression" | "improvement"
            assert "verdict_a" in item
            assert "verdict_b" in item

    def test_diff_json_island_present_in_html(self, tmp_path: Path):
        """The HTML must contain enough JS data to drive the diff view client-side."""
        runs_dir, _, _ = self._build_regression_runs(tmp_path)
        out_file = tmp_path / "report.html"
        report = _import_report()
        report.generate_html(runs_dir=runs_dir, out_path=out_file)
        content = out_file.read_text(encoding="utf-8")
        # The JSON island must have per-cell verdict so JS can compute diff
        m = re.search(
            r'<script[^>]+id="bench-data"[^>]*>(.*?)</script>',
            content,
            re.DOTALL,
        )
        data = json.loads(m.group(1))
        # Every scenario must have cell verdicts for JS diff computation
        for sc in data["scenarios"]:
            for variant_id in sc["cells"]:
                assert "verdict" in sc["cells"][variant_id]


# ─── 7. Markdown format ───────────────────────────────────────────────────────


class TestMarkdownFormat:
    def _gen_md(self, tmp_path: Path) -> str:
        runs_dir = _build_synthetic_runs(tmp_path)
        report = _import_report()
        buf = StringIO()
        report.generate_markdown(runs_dir=runs_dir, out=buf)
        return buf.getvalue()

    def test_markdown_has_table(self, tmp_path: Path):
        md = self._gen_md(tmp_path)
        # Markdown table: rows with | separators
        assert "|" in md

    def test_markdown_has_all_scenarios(self, tmp_path: Path):
        md = self._gen_md(tmp_path)
        assert "sc_alpha" in md
        assert "sc_beta" in md
        assert "sc_gamma" in md
        assert "sc_delta" in md

    def test_markdown_has_all_variants(self, tmp_path: Path):
        md = self._gen_md(tmp_path)
        assert "baseline" in md
        assert "variant_b" in md
        assert "variant_c" in md

    def test_markdown_shows_pass_fail_icons_or_words(self, tmp_path: Path):
        md = self._gen_md(tmp_path)
        # Either icon-based or text-based verdicts
        has_verdicts = (
            "PASS" in md or "FAIL" in md
            or "✅" in md or "❌" in md
        )
        assert has_verdicts

    def test_markdown_four_layer_scores(self, tmp_path: Path):
        """Markdown summary must show 4-layer pass rates (outcome/trajectory/etc)."""
        md = self._gen_md(tmp_path)
        lower = md.lower()
        assert "outcome" in lower
        assert "trajectory" in lower
        assert "constraint" in lower
        assert "robustness" in lower

    def test_markdown_no_html_tags(self, tmp_path: Path):
        """Markdown output must not contain raw HTML tags."""
        md = self._gen_md(tmp_path)
        # No HTML tags in plain markdown
        assert "<br>" not in md
        assert "<sub>" not in md
        assert "<td>" not in md
        assert "<tr>" not in md

    def test_markdown_summary_pass_counts(self, tmp_path: Path):
        """Markdown must include numeric pass counts per variant."""
        md = self._gen_md(tmp_path)
        # baseline: sc_alpha✓ sc_beta✓ sc_delta✓ → 3/4
        # variant_b: sc_alpha✓ sc_delta✓ → 2/4
        # variant_c: all pass → 4/4 (except sc_delta which fails)
        # At minimum, fractions or counts should appear
        assert "/" in md or "%" in md

    def test_markdown_stdout_via_cli(self, tmp_path: Path):
        """CLI --format markdown produces valid non-empty markdown to stdout."""
        runs_dir = _build_synthetic_runs(tmp_path)
        result = subprocess.run(
            [
                sys.executable, "-m", "windtunnel.cli",
                "report",
                "--runs", str(runs_dir),
                "--format", "markdown",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "|" in result.stdout  # markdown table
        assert "sc_alpha" in result.stdout


# ─── 7b. JSON format ─────────────────────────────────────────────────────────


class TestJsonFormat:
    def _html_data(self, runs_dir: Path, tmp_path: Path) -> dict[str, Any]:
        out_file = tmp_path / "report.html"
        report = _import_report()
        report.generate_html(runs_dir=runs_dir, out_path=out_file)
        content = out_file.read_text(encoding="utf-8")
        m = re.search(
            r'<script[^>]+id="bench-data"[^>]*>(.*?)</script>',
            content,
            re.DOTALL,
        )
        assert m is not None
        return json.loads(m.group(1))

    def test_report_json_stdout_matches_html_json_island(self, tmp_path: Path) -> None:
        runs_dir = _build_synthetic_runs(tmp_path)
        result = subprocess.run(
            [
                sys.executable, "-m", "windtunnel.cli",
                "report",
                "--runs", str(runs_dir),
                "--format", "json",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == self._html_data(runs_dir, tmp_path)

    def test_report_json_out_writes_parseable_artifact(self, tmp_path: Path) -> None:
        runs_dir = _build_synthetic_runs(tmp_path)
        out_file = tmp_path / "report.json"
        result = subprocess.run(
            [
                sys.executable, "-m", "windtunnel.cli",
                "report",
                "--runs", str(runs_dir),
                "--format", "json",
                "--out", str(out_file),
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert json.loads(out_file.read_text(encoding="utf-8")) == self._html_data(runs_dir, tmp_path)


# ─── 8. Per-scenario drill-down data ──────────────────────────────────────────


class TestDrillDown:
    """Each cell must carry trace data for the drill-down panel."""

    def _gen_data(self, tmp_path: Path) -> dict[str, Any]:
        runs_dir = _build_synthetic_runs(tmp_path)
        out_file = tmp_path / "report.html"
        report = _import_report()
        report.generate_html(runs_dir=runs_dir, out_path=out_file)
        content = out_file.read_text(encoding="utf-8")
        m = re.search(
            r'<script[^>]+id="bench-data"[^>]*>(.*?)</script>',
            content,
            re.DOTALL,
        )
        return json.loads(m.group(1))

    def test_cell_has_trace_turns(self, tmp_path: Path):
        """Cell must include trace turns for the drill-down panel."""
        data = self._gen_data(tmp_path)
        sc = next(s for s in data["scenarios"] if s["scenario_id"] == "sc_alpha")
        cell = sc["cells"]["baseline"]
        assert "trace" in cell
        assert "turns" in cell["trace"]
        assert len(cell["trace"]["turns"]) > 0

    def test_trace_turn_has_role_and_content(self, tmp_path: Path):
        data = self._gen_data(tmp_path)
        sc = next(s for s in data["scenarios"] if s["scenario_id"] == "sc_alpha")
        cell = sc["cells"]["baseline"]
        for turn in cell["trace"]["turns"]:
            assert "role" in turn
            assert "content" in turn

    def test_trace_has_tool_calls(self, tmp_path: Path):
        """Tool call turns must have tool_calls in the drill-down data."""
        data = self._gen_data(tmp_path)
        sc = next(s for s in data["scenarios"] if s["scenario_id"] == "sc_alpha")
        cell = sc["cells"]["baseline"]
        # Our fixture has 1 tool call
        tool_call_turns = [t for t in cell["trace"]["turns"] if t.get("tool_calls")]
        assert len(tool_call_turns) >= 1

    def test_cell_has_score_detail(self, tmp_path: Path):
        """Each cell must carry score detail strings for all 4 layers."""
        data = self._gen_data(tmp_path)
        sc = next(s for s in data["scenarios"] if s["scenario_id"] == "sc_alpha")
        cell = sc["cells"]["baseline"]
        for layer in ("outcome", "trajectory", "constraint", "robustness"):
            assert layer in cell["layers"]
            assert "passed" in cell["layers"][layer]
            assert "detail" in cell["layers"][layer]


# ─── 9. Prototype scenarios integration smoke test ────────────────────────────


class TestPrototypeScenariosSmoke:
    """Verify report renders with the 11 prototype scenarios via synthetic traces."""

    def _build_prototype_runs(self, tmp_path: Path) -> Path:
        """Build synthetic runs for all 11 prototype scenarios."""
        from windtunnel.scenarios.prototype import PROTOTYPE_SCENARIOS

        runs_dir = tmp_path / "runs"
        for sc in PROTOTYPE_SCENARIOS:
            trace = _make_trace(scenario_id=sc.name, variant_id="baseline")
            score = _make_score(outcome_pass=True)
            _write_run(tmp_path, trace, score, base_runs=runs_dir)
        return runs_dir

    def test_all_11_prototype_scenarios_render(self, tmp_path: Path):
        runs_dir = self._build_prototype_runs(tmp_path)
        out_file = tmp_path / "report.html"
        report = _import_report()
        # Must not raise
        report.generate_html(runs_dir=runs_dir, out_path=out_file)
        content = out_file.read_text(encoding="utf-8")

        from windtunnel.scenarios.prototype import PROTOTYPE_SCENARIOS
        for sc in PROTOTYPE_SCENARIOS:
            assert sc.name in content, f"Scenario {sc.name!r} missing from report"

    def test_prototype_scenarios_in_markdown(self, tmp_path: Path):
        runs_dir = self._build_prototype_runs(tmp_path)
        report = _import_report()
        buf = StringIO()
        report.generate_markdown(runs_dir=runs_dir, out=buf)
        md = buf.getvalue()

        from windtunnel.scenarios.prototype import PROTOTYPE_SCENARIOS
        for sc in PROTOTYPE_SCENARIOS:
            assert sc.name in md, f"Scenario {sc.name!r} missing from markdown"


# ─── 10. Load runs from directory ─────────────────────────────────────────────


class TestLoadRuns:
    """The report loader must correctly parse the runs/ directory layout."""

    def test_load_runs_returns_cells(self, tmp_path: Path):
        runs_dir = _build_synthetic_runs(tmp_path)
        report = _import_report()
        cells = report.load_runs(runs_dir=runs_dir)
        assert len(cells) > 0

    def test_load_runs_cell_keys(self, tmp_path: Path):
        runs_dir = _build_synthetic_runs(tmp_path)
        report = _import_report()
        cells = report.load_runs(runs_dir=runs_dir)
        # Each cell is keyed by (scenario_id, variant_id)
        keys = list(cells.keys())
        assert all(isinstance(k, tuple) and len(k) == 2 for k in keys)

    def test_load_runs_correct_scenario_count(self, tmp_path: Path):
        runs_dir = _build_synthetic_runs(tmp_path)
        report = _import_report()
        cells = report.load_runs(runs_dir=runs_dir)
        scenario_ids = {k[0] for k in cells}
        assert scenario_ids == {"sc_alpha", "sc_beta", "sc_gamma", "sc_delta"}

    def test_load_runs_correct_variant_count(self, tmp_path: Path):
        runs_dir = _build_synthetic_runs(tmp_path)
        report = _import_report()
        cells = report.load_runs(runs_dir=runs_dir)
        variant_ids = {k[1] for k in cells}
        assert variant_ids == {"baseline", "variant_b", "variant_c"}

    def test_load_runs_cell_has_trace_and_score(self, tmp_path: Path):
        runs_dir = _build_synthetic_runs(tmp_path)
        report = _import_report()
        cells = report.load_runs(runs_dir=runs_dir)
        for key, cell in cells.items():
            assert "trace" in cell
            assert "score" in cell

    def test_load_runs_empty_dir_returns_empty(self, tmp_path: Path):
        runs_dir = tmp_path / "empty_runs"
        runs_dir.mkdir()
        report = _import_report()
        cells = report.load_runs(runs_dir=runs_dir)
        assert cells == {}
