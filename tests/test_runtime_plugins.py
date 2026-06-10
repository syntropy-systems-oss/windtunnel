"""Runtime-plugin resolution tests — the CLI's pluggable-runtime seam.

Resolution goes:

  1. built-ins ("in_memory")
  2. importlib.metadata entry points, group "windtunnel.runtimes", by NAME
  3. "module:attr" dotted path
  4. error (exit 2) listing what IS available

These tests pin the platform-independent legs (1, 3, 4) plus the contract
details: the entry-point target may be a RuntimePlugin instance or class
(class → instantiated no-args), and pre_run is optional (invoked via
getattr). Leg 2 (entry-point discovery) is pinned by each driver package's
own suite — e.g. windtunnel-acme/tests/test_plugin_resolution.py — since
it requires that driver to be installed.
"""
from __future__ import annotations

import pytest

# ─── fixture plugins for the dotted-path leg ─────────────────────────────────


class _FixtureRuntime:
    """Stand-in AgentRuntime — never provisioned in these tests."""

    def __init__(self, runtime_name: str, label: str, soul_path: str | None) -> None:
        self.runtime_name = runtime_name
        self.label = label
        self.soul_path = soul_path

    def provision(self, config, mcps=None):  # pragma: no cover - contract stub
        raise NotImplementedError


class DottedFixturePlugin:
    """Minimal RuntimePlugin CLASS target for the module:attr form.

    Deliberately has NO pre_run — proves the hook is optional.
    """

    def build(self, runtime_name: str, label: str, soul_path: str | None):
        return _FixtureRuntime(runtime_name, label, soul_path)


# A pre-built INSTANCE target (the other allowed entry-point/dotted shape).
DOTTED_FIXTURE_INSTANCE = DottedFixturePlugin()



# ─── leg 1: built-in ─────────────────────────────────────────────────────────


class TestBuiltinResolution:
    def test_in_memory_resolves_to_builtin_plugin(self) -> None:
        from windtunnel.cli import _InMemoryPlugin, _resolve_runtime_plugin
        plugin = _resolve_runtime_plugin("in_memory")
        assert isinstance(plugin, _InMemoryPlugin)

    def test_in_memory_build_returns_in_memory_runtime(self) -> None:
        from windtunnel.cli import _resolve_runtime_plugin
        from windtunnel.runtimes.in_memory import InMemoryRuntime
        runtime = _resolve_runtime_plugin("in_memory").build("in_memory", "lbl", None)
        assert isinstance(runtime, InMemoryRuntime)

    def test_builtin_plugin_pre_run_is_absent(self) -> None:
        """The CLI invokes pre_run via getattr — the built-in plugin omitting it
        is the living proof that the hook is optional."""
        from windtunnel.cli import _resolve_runtime_plugin
        plugin = _resolve_runtime_plugin("in_memory")
        assert getattr(plugin, "pre_run", None) is None

    def test_build_runtime_in_memory_unchanged(self) -> None:
        """_build_runtime keeps its pre-refactor behavior for in_memory."""
        from windtunnel.cli import _build_runtime
        from windtunnel.runtimes.in_memory import InMemoryRuntime
        runtime = _build_runtime("in_memory", "lbl", soul_path=None)
        assert isinstance(runtime, InMemoryRuntime)



# ─── leg 3: module:attr dotted path ──────────────────────────────────────────


class TestDottedPathResolution:
    def test_dotted_path_class_is_instantiated(self) -> None:
        from windtunnel.cli import _resolve_runtime_plugin
        name = f"{__name__}:DottedFixturePlugin"
        plugin = _resolve_runtime_plugin(name)
        assert isinstance(plugin, DottedFixturePlugin)
        runtime = plugin.build(name, "lbl", None)
        assert isinstance(runtime, _FixtureRuntime)
        assert runtime.runtime_name == name

    def test_dotted_path_instance_is_used_as_is(self) -> None:
        from windtunnel.cli import _resolve_runtime_plugin
        plugin = _resolve_runtime_plugin(f"{__name__}:DOTTED_FIXTURE_INSTANCE")
        assert plugin is DOTTED_FIXTURE_INSTANCE

    def test_dotted_path_bad_attr_exits_2(self, capsys: pytest.CaptureFixture) -> None:
        from windtunnel.cli import _resolve_runtime_plugin
        with pytest.raises(SystemExit) as exc:
            _resolve_runtime_plugin(f"{__name__}:NoSuchPlugin")
        assert exc.value.code == 2
        assert "could not load runtime plugin" in capsys.readouterr().err

    def test_dotted_path_bad_module_exits_2(self, capsys: pytest.CaptureFixture) -> None:
        from windtunnel.cli import _resolve_runtime_plugin
        with pytest.raises(SystemExit) as exc:
            _resolve_runtime_plugin("no_such_module_xyz:Plugin")
        assert exc.value.code == 2
        assert "could not load runtime plugin" in capsys.readouterr().err


# ─── leg 4: unknown name error ───────────────────────────────────────────────


class TestUnknownRuntimeError:
    def test_unknown_name_exits_2_and_lists_available(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        from windtunnel.cli import _resolve_runtime_plugin
        with pytest.raises(SystemExit) as exc:
            _resolve_runtime_plugin("warp_drive")
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "unknown runtime 'warp_drive'" in err
        # The listing must cover at least the built-in. (Whether driver entry
        # points also appear depends on what's installed in the env — the
        # driver suites assert their own names show up.)
        assert "in_memory" in err


# ─── SPI conformance ─────────────────────────────────────────────────────────


class TestRuntimePluginProtocol:
    def test_protocol_exported_from_spi(self) -> None:
        from windtunnel.spi import RuntimePlugin  # noqa: F401

