"""Built-in debrief hook."""
from __future__ import annotations

import os
import time
from typing import Any

from windtunnel.spi.hooks import Hook, HookContext


class DebriefHook(Hook):
    """Ask the agent for a post-score self-report and emit it as an artifact."""

    name = "debrief"

    def on_run_scored(self, ctx: HookContext) -> None:
        score = ctx.score
        if score is None:
            return

        verdict = _run_verdict(score, ctx.scenario)
        failed_layers = _failed_layers(score)
        if os.environ.get("WT_DEBRIEF_ON", "").lower() != "all" and not failed_layers:
            return

        reasons = _score_reasons(score)
        prompt = _build_prompt(verdict, reasons)
        reply = ""
        error: str | None = None
        started = time.perf_counter()
        try:
            reply = ctx.converse(prompt)
        except Exception as exc:  # noqa: BLE001 - debriefs report, never gate
            error = str(exc)
        fallback_duration_ms = int((time.perf_counter() - started) * 1000)

        trace = ctx.trace
        agent = ctx.agent
        model = None
        if agent is not None and getattr(agent, "model", None) is not None:
            model = getattr(agent.model, "name", None)
        if model is None and trace is not None:
            model = getattr(trace, "model", None)

        ctx.emit_artifact({
            "schema_version": 2,
            "run_id": ctx.run_id or (getattr(trace, "run_id", "") if trace is not None else ""),
            "scenario_id": _scenario_id(ctx),
            "agent": getattr(agent, "agent_id", "") if agent is not None else "",
            "model": model or "",
            "verdict": verdict,
            "failed_layers": failed_layers,
            "reasons": reasons,
            "prompt": prompt,
            "reply": reply,
            "tools_disabled": False,
            "timed_out": ctx.converse_timed_out,
            "duration_ms": ctx.converse_duration_ms
            if ctx.converse_duration_ms is not None
            else fallback_duration_ms,
            "error": error,
        })


def _run_verdict(score: Any, scenario: Any = None) -> str:
    if not getattr(score, "integrity").passed:
        return "INVALID"
    gate_layers = (
        scenario.resolved_gate_layers()
        if scenario is not None and hasattr(scenario, "resolved_gate_layers")
        else ("outcome",)
    )
    return "PASS" if score.gate_passed(gate_layers) else "FAIL"


def _failed_layers(score: Any) -> list[str]:
    failed: list[str] = []
    for layer_name in ("outcome", "trajectory", "constraint", "integrity"):
        layer = getattr(score, layer_name)
        if not layer.passed:
            failed.append(layer_name)
    return failed


def _score_reasons(score: Any) -> dict[str, str]:
    return {
        layer_name: str(getattr(score, layer_name).detail)
        for layer_name in ("outcome", "trajectory", "constraint", "integrity")
    }


def _scenario_id(ctx: HookContext) -> str:
    if ctx.trace is not None:
        return str(getattr(ctx.trace, "scenario_id", ""))
    if ctx.scenario is not None:
        return str(getattr(ctx.scenario, "name", ""))
    return ""


def _build_prompt(verdict: str, reasons: dict[str, str]) -> str:
    reason_lines = "\n".join(
        f"- {layer}: {detail}" for layer, detail in reasons.items()
    )
    return (
        "Wind Tunnel has finished scoring this run. "
        "Your answer is diagnostic only and will not affect the verdict.\n\n"
        f"Verdict: {verdict}\n"
        "Score layer details:\n"
        f"{reason_lines}\n\n"
        "Answer in this order:\n"
        "1. Environment/tool errors first: did any tool call return an error, "
        "unexpected shape, or missing capability? Quote it.\n"
        "2. Blockers: was anything you needed absent from the environment?\n"
        "3. What would you do differently next time?"
    )
