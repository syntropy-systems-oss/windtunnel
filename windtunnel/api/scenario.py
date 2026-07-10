"""Scenario — per-scenario authoring of layer expectations.

The Scenario dataclass is the primary authoring surface for bench
scenarios. It carries:
  - Outcome:     target_facts (AND-of-OR) + target_numbers (typed numeric)
  - Trajectory:  must_call, forbidden_calls, order_matters + trajectory_checks
  - Constraint:  policies (predicate list over Trace)
  - Integrity:   perturbations (test conditions verified after the run)
  - Multi-turn:  user_turns (ordered user-turn sequence; empty = single-turn)

Design decisions captured here:

1. Numeric matching uses word-boundary regex (\\b<value>\\b) rather than
   a structured {value, unit} extractor. Rationale: the structured
   extractor requires a number-parser that handles "12 units", "12.0",
   "twelve" etc. — a fragile NLP problem. Word-boundary regex is simple,
   deterministic, and solves the known false-positive ("3" matching B003CCC)
   without over-engineering. When a unit is specified, the matcher requires
   that unit to appear near the number (within 30 chars) without requiring an
   exact ``"3 units"`` phrase.

2. Policy is a named predicate over a Trace. The effect_class field is
   a forward-compat hook for the side-effect-safety dim — it
   declares which effect class the policy guards against, so the
   constraint evaluator can group violations by class in its report.

3. Perturbation is an abstract base dataclass with an apply() method.
   Concrete perturbation types (CorruptPriorAssistantTurn etc.) live in
   perturbations.py. The type is defined here so Scenario can reference
   it without circular imports. PreSendPerturbation (the history-shaping
   subfamily) lives here for the same reason — the runner dispatches on
   the class identity, not on duck-typed attributes.

4. requires_tool_use=True makes the outcome evaluator reject a trace that
   has no tool calls even if target facts happen to be present. This
   closes the "model guessed from training" false-positive.

5. variance_allowed=False is the default; the deploy gate
   treats anything below 100% per-run as a regression unless the
   scenario opts in to a variance budget.

6. TrajectoryCheck is the trajectory layer's extension point — the
   counterpart to Policy (constraint) and Perturbation (integrity).
   The sugar fields (must_call/forbidden_calls/order_matters) compile
   into built-in checks inside evaluate_trajectory; custom checks in
   Scenario.trajectory_checks run after them and are ANDed in.

7. Preconditions are not scoring. They are world-shape assertions checked
   before the runner spends any agent turn. requires_tools and requires_files
   are sugar that compile into ToolAvailable/FileExists preconditions at run time.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from windtunnel.api.preconditions import Precondition
from windtunnel.api.score import GATE_LAYER_ORDER, FailureCost, GateLayer, LayerResult
from windtunnel.api.trace import Trace

# ─── Numeric fact ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class NumberFact:
    """A typed numeric expectation for the outcome layer.

    Uses word-boundary regex (\\b<value>\\b) to match — prevents false
    positives where the digit appears embedded in IDs like B003CCC or
    BATCH-2026.

    unit: when specified, evaluator also checks that the number appears
    within 30 characters of the unit string in the answer. This tightens the
    check without requiring exact "3 units" phrasing.
    """
    value: int
    unit: str | None = None


# ─── Policy ───────────────────────────────────────────────────────────────────

@dataclass
class Policy:
    """A named predicate over a Trace for the constraint layer.

    predicate: Callable[[Trace], bool] — returns True if the constraint
        is satisfied, False if violated.
    effect_class: forward-compat hook for the side-effect-safety dim.
        Declares which effect class this policy guards, e.g.
        "external_send", "destructive". None = unclassified.
    """
    name: str
    predicate: Callable[[Trace], bool]
    effect_class: str | None = None


# ─── Perturbation base ────────────────────────────────────────────────────────

@dataclass
class Perturbation(ABC):
    """Abstract base for trace perturbations applied before a scenario run.

    Concrete implementations live in perturbations.py. Each apply()
    returns a NEW Trace (original is never mutated) with a
    'perturbation_applied: <name> ...' entry in worker_warnings so the
    integrity evaluator can verify the perturbation was applied.
    """

    @abstractmethod
    def apply(self, trace: Trace) -> Trace:
        """Return a mutated copy of trace with this perturbation applied."""
        ...

    @property
    @abstractmethod
    def marker(self) -> str:
        """The worker_warnings marker string that apply() must inject."""
        ...


# ─── Pre-send perturbation base ───────────────────────────────────────────────

@dataclass
class PreSendPerturbation(Perturbation):
    """Abstract base for history-shaping perturbations injected BEFORE send.

    The dual contract (mirrored by the runner, see runner._run_once):

    1. shape_messages() shapes the LIVE messages: on the final (scored)
       turn of a scenario the runner calls
       ``messages = p.shape_messages(messages, scenario)`` before
       ``handle.send()``, so the model genuinely runs its scored turn on
       top of the corrupted prior turns (blank assistant turn, wrong tool
       call + result, stale memory line, …) — not a post-hoc
       counterfactual it never saw.
    2. apply() is SKIPPED post-run. The runner does NOT call apply() for
       PreSendPerturbation instances — the corruption already happened
       live, and re-mutating the recorded trace would falsify what the
       model actually saw and did. Instead the runner appends ``marker``
       to trace.worker_warnings directly, so evaluate_integrity still
       verifies the perturbation was applied.

    apply() must still be implemented (the Perturbation contract): it is
    the post-hoc fallback used when a saved trace is mutated outside the
    live runner path (e.g. replay-style scoring of seed traces).

    pre_send is a ClassVar, not a field: it is class identity, not
    per-instance configuration. The runner dispatches on
    ``isinstance(p, PreSendPerturbation)``; the attribute is kept so
    existing ``getattr(p, "pre_send", False)`` call sites stay truthful.
    """

    pre_send: ClassVar[bool] = True

    @abstractmethod
    def shape_messages(
        self, messages: list[dict[str, Any]], scenario: Scenario
    ) -> list[dict[str, Any]]:
        """Return a NEW messages list with the corrupted prior turns injected.

        messages: the accumulated OpenAI-format message list for the final
            (scored) turn, ending with the scored user turn.
        scenario: the Scenario being run — used to synthesize plausible
            prior calls (e.g. the first must_call tool, a target-fact entity).
        """
        ...


# ─── Trajectory check ─────────────────────────────────────────────────────────

class TrajectoryCheck(ABC):
    """A verifier over the path the agent actually took.

    This is the trajectory layer's counterpart to Policy (constraint) and
    Perturbation (integrity): an open extension point for path
    expectations the sugar fields (must_call / forbidden_calls /
    order_matters) can't express — "paginate at most twice", "never call
    a write tool before a read tool", etc.

    check() receives ``calls``: the chronologically-ordered list of
    OBSERVED tool names — server-witnessed (trace.mcp_calls) when a
    logging mock MCP was in play, else the transcript's self-reported
    tool_calls (evaluate_trajectory picks the evidence source). Names may
    be platform-decorated (``mcp_acme_ops_client_lookup``); use
    windtunnel.api.tool_name_matches for prefix-chain-aware comparison
    against canonical bare names.

    Returns (passed, detail). detail is joined into the LayerResult
    detail string, so make it diagnostic ("paginated 4x, budget 2"), not
    just "failed". A check that raises is recorded as a failure (same
    forgiveness as Policy predicates) — it never crashes the evaluator.
    """

    @abstractmethod
    def check(self, calls: list[str]) -> tuple[bool, str]:
        """Verify the observed tool-call path. Return (passed, detail)."""
        ...

    def check_trace(self, trace: Trace, calls: list[str]) -> tuple[bool, str]:
        """Verify the path with access to the full saved trace.

        Override this when the check needs tool-call arguments or observations,
        not just normalized tool names. The default preserves the original
        calls-only contract for existing custom checks.
        """
        del trace
        return self.check(calls)


# ─── Scenario ─────────────────────────────────────────────────────────────────

@dataclass
class Scenario:
    """Per-scenario authoring of outcome, path, policy, and test conditions.

    Minimal required fields: ``name`` plus either ``prompt`` or ``user_turns``.
    ``target_facts`` defaults to an empty list so a custom ``outcome_fn`` does
    not require placeholder data.

    By default the gate includes outcome plus every trajectory/constraint
    layer for which this scenario declares an expectation. Set ``gate_layers``
    explicitly to make selected layers diagnostic-only. Experiment integrity
    is not configurable: a run whose declared perturbations were not applied
    is INVALID rather than an agent pass or failure.
    """
    # Identity
    name: str
    prompt: str = ""

    # ── Outcome layer ──────────────────────────────────────────────────────────
    # target_facts: AND-of-OR groups. Every outer group must have at least one
    # member present in the last assistant turn's content.
    target_facts: list[list[str]] = field(default_factory=list)

    # target_numbers: typed numeric facts checked with word-boundary regex.
    # All must be satisfied (AND semantics, like outer target_facts groups).
    target_numbers: list[NumberFact] = field(default_factory=list)

    # requires_tool_use: when True, outcome FAILS if the trace has no tool
    # calls — even if target facts happen to appear in the answer.
    requires_tool_use: bool = False

    # forbidden_facts: strings that must NOT appear (asserted, non-negated) in the
    # last assistant turn. Any forbidden fact found in a non-negated context fails
    # the outcome even when all target_facts are present — a hallucinated claim is
    # not a pass. Matching is negation-aware (NEGATION_CUES) and case-insensitive.
    # Bare-number entries use word-boundary matching. Default empty = no gate.
    forbidden_facts: list[str] = field(default_factory=list)

    # outcome_fn: optional custom outcome evaluator. When set it FULLY determines
    # the outcome layer — target_facts / target_numbers / forbidden_facts are not
    # consulted — letting a scenario score from ANY trace evidence instead of the
    # model's last-turn text. The canonical use is grading an artifact a StateProbe
    # froze into trace.observations (a produced file, a database row, external API
    # state) so the gate reflects what the agent actually built, not what it claims.
    # Receives the Trace, returns a LayerResult (passed + diagnostic detail); a
    # raised exception is caught and scored as a failure. The structural gates
    # still apply first: a missing assistant turn, and requires_tool_use, fail
    # before outcome_fn runs. Not serialized (it's a callable) — like policies and
    # trajectory_checks it is reconstructed when the scenario's pack is re-imported,
    # so offline re-scoring needs the pack importable.
    outcome_fn: Callable[[Trace], LayerResult] | None = None

    # ── Trajectory layer ───────────────────────────────────────────────────────
    # must_call: each entry is EITHER a str (exact tool name required) OR a
    # list[str] (any-of alternatives group — at least one alternative must be
    # called between reads_file/create_branch etc.).
    # Example: ["read_file", ["patch", "write_file"], "create_branch"] requires
    # read_file, then patch OR write_file, then create_branch.
    # A plain str is treated as a single-element alternatives group internally.
    must_call: list[str | list[str]] = field(default_factory=list)
    forbidden_calls: list[str] = field(default_factory=list)
    order_matters: bool = False

    # trajectory_checks: custom TrajectoryCheck verifiers over the observed
    # tool-call path. evaluate_trajectory compiles the sugar fields above into
    # built-in checks and runs these AFTER them; the layer passes iff ALL
    # checks (built-in + custom) pass, with failure details joined.
    trajectory_checks: list[TrajectoryCheck] = field(default_factory=list)

    # ── Multi-turn ─────────────────────────────────────────────────────────────
    # user_turns: when NON-EMPTY, this IS the full ordered list of user turns
    # the runner sends sequentially under one session_id (turn N's message
    # history = turns 1..N interleaved with prior assistant replies), and
    # `prompt` is IGNORED by the runner. Scoring is unchanged: the evaluators
    # score the final assistant turn, so the LAST entry is the scored turn.
    # Convention (not enforced): set `prompt` to a copy of that last entry so
    # prompt-reading surfaces (triage, the LLM judge) still show the scored
    # question. Empty (the default) = single-turn; the runner sends [prompt].
    # History: this was a duck-typed extension attached via setattr by
    # dim_multi_turn_drift; it is now a first-class field.
    user_turns: list[str] = field(default_factory=list)

    # ── World preconditions ───────────────────────────────────────────────────
    # preconditions: checks over the already-started MCP handles, optional
    # StateProbe, and AgentConfig. They fail fast before reset_state()/send().
    # requires_tools is sugar for ToolAvailable(<name>) preconditions.
    # requires_files is sugar for FileExists(<path>) preconditions. Relative
    # paths resolve against the runtime workspace template when the driver
    # exposes one, then the workspace, then the current working directory.
    preconditions: list[Precondition] = field(default_factory=list)
    requires_tools: list[str] = field(default_factory=list)
    requires_files: list[str] = field(default_factory=list)

    # ── Constraint layer ───────────────────────────────────────────────────────
    policies: list[Policy] = field(default_factory=list)

    # gate_layers: explicit gate selection. None infers outcome plus every
    # declared trajectory/constraint expectation. [] creates a diagnostic-only
    # scenario (integrity still must hold). Use ["outcome"] for legacy 0.8
    # outcome-only behavior.
    gate_layers: list[GateLayer] | None = None

    # ── Experiment conditions ──────────────────────────────────────────────────
    perturbations: list[Perturbation] = field(default_factory=list)

    # ── Metadata ───────────────────────────────────────────────────────────────
    failure_cost: FailureCost = field(default_factory=FailureCost)

    # variance_allowed: when True, the deploy gate accepts <100% pass rate
    # and reports pass_rate ± stddev rather than treating any miss as a
    # regression. Used by the sampler-sensitivity dim.
    variance_allowed: bool = False

    # tags: free-form labels for grouping/filtering scenarios.
    # Convention: "dim:<name>" for the failure dimension (e.g. "dim:tool_affordance").
    # Used by the failure taxonomy to group regressions by dimension.
    tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("scenario name must not be empty")
        if not self.prompt.strip() and not self.user_turns:
            raise ValueError("scenario requires prompt or user_turns")
        if any(not turn.strip() for turn in self.user_turns):
            raise ValueError("scenario user_turns must not contain empty turns")
        if self.order_matters and not self.must_call:
            raise ValueError("order_matters requires at least one must_call entry")
        if self.gate_layers is not None:
            self._validate_gate_layers(self.gate_layers)

    @property
    def scored_prompt(self) -> str:
        """Return the user question represented by authoring/report surfaces."""
        return self.user_turns[-1] if self.user_turns else self.prompt

    def resolved_gate_layers(self) -> tuple[GateLayer, ...]:
        """Return the canonical gate layers for this scenario."""
        if self.gate_layers is not None:
            self._validate_gate_layers(self.gate_layers)
            selected = set(self.gate_layers)
            return tuple(layer for layer in GATE_LAYER_ORDER if layer in selected)

        declared: set[GateLayer] = {"outcome"}
        if self.must_call or self.forbidden_calls or self.trajectory_checks:
            declared.add("trajectory")
        if self.policies:
            declared.add("constraint")
        return tuple(layer for layer in GATE_LAYER_ORDER if layer in declared)

    @staticmethod
    def _validate_gate_layers(gate_layers: list[GateLayer]) -> None:
        invalid = [layer for layer in gate_layers if layer not in GATE_LAYER_ORDER]
        if invalid:
            raise ValueError(f"unknown gate layers: {invalid}")
        if len(set(gate_layers)) != len(gate_layers):
            raise ValueError("gate_layers must not contain duplicates")
