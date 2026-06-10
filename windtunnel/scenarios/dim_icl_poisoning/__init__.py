"""Dim: ICL poisoning (serialization eval).

Three scenarios that test whether the model's policy survives when
prior turns in the conversation history are corrupted:

  empty_prior_assistant_turn — blank a prior assistant turn; model must
                               continue normally (not copy the empty shape)
  primitive_fallback_leak    — prior turn has 'tool: {...}' literal text
                               (the fallback-render bug shape); model must
                               not replicate it
  bad_prior_call_pattern     — prior assistant used wrong arg names; model
                               must recover with correct args, not copy them

Both production serialization bugs (empty-prior-turn and fallback-render)
are regression-guarded here.
Run via `wt run` (windtunnel.api.runner.run_scenario).
"""
from windtunnel.api.pack import ScenarioPack
from windtunnel.scenarios.dim_icl_poisoning.scenarios import ICL_POISONING_SCENARIOS
from windtunnel.scenarios.dim_tool_affordance import PACK as _TOOL_AFFORDANCE_PACK

PACK = ScenarioPack(
    name="icl_poisoning",
    scenarios=list(ICL_POISONING_SCENARIOS),
    # icl_poisoning has NO mock server of its own — it is a pure ICL-history
    # serialization eval that reuses the tool_affordance ops-suite tools.
    # Alias the tool_affordance factory so the platform runtime has tools to
    # register (provision() raises on empty mcps). Same port (8091) — only
    # one batch runs at a time so the reuse never collides.
    mcp_factory=_TOOL_AFFORDANCE_PACK.mcp_factory,
)
