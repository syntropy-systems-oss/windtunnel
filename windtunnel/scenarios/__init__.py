"""scenarios package — per-dim scenario libraries.

Each dim_* subpackage exposes a module-level PACK (a
windtunnel.api.ScenarioPack) bundling its scenarios with the CLI wiring
they need (mock-MCP factory, transport-only flag). builtin_packs() below is
how the `wt` CLI discovers them; third-party packs arrive via the
"windtunnel.scenario_packs" entry-point group instead (see windtunnel.api.pack).
"""


def builtin_packs() -> list:
    """Return the built-in ScenarioPacks, in canonical bench order.

    WHY an explicit list rather than pkgutil-iterating dim_* subpackages:
    the list order pins the CLI's scenario iteration order (filesystem/
    alphabetical discovery would silently reorder the sweep and every runs/
    log relative to the pre-pack CLI), and an explicit list is greppable —
    adding a dim is a deliberate one-line diff here, the pack equivalent of
    registering an entry point.

    Imports live inside the function so `import windtunnel.scenarios` stays
    cheap and dim packages can themselves import this package's helpers
    (e.g. scenarios._mock_factory) without a circular import.
    """
    from windtunnel.scenarios.dim_clarify_vs_guess import PACK as clarify_vs_guess
    from windtunnel.scenarios.dim_icl_poisoning import PACK as icl_poisoning
    from windtunnel.scenarios.dim_memory_conflict import PACK as memory_conflict
    from windtunnel.scenarios.dim_multi_turn_drift import PACK as multi_turn_drift
    from windtunnel.scenarios.dim_policy_pressure import PACK as policy_pressure
    from windtunnel.scenarios.dim_recovery import PACK as recovery
    from windtunnel.scenarios.dim_sampler_sensitivity import PACK as sampler_sensitivity
    from windtunnel.scenarios.dim_side_effect_safety import PACK as side_effect_safety
    from windtunnel.scenarios.dim_silent_failure import PACK as silent_failure
    from windtunnel.scenarios.dim_tool_affordance import PACK as tool_affordance

    return [
        tool_affordance,
        clarify_vs_guess,
        memory_conflict,
        policy_pressure,
        recovery,
        sampler_sensitivity,
        side_effect_safety,
        silent_failure,
        icl_poisoning,
        multi_turn_drift,
    ]
