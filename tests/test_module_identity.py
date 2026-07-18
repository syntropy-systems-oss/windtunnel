"""Regression tests for the two-module-copies hazard.

`wt run --pack-source path/to/file.py:PACK --runtime path/to/file:PLUGIN`
used to import THE SAME source file twice under two different module
identities: the file-path form (--pack-source) hashed the resolved path
into a synthetic module name and exec'd it fresh, while the dotted form
(--runtime) imported it under its plain module name via
importlib.import_module — which also execs fresh the first time that
plain name is seen. Since neither loader ever checked whether the OTHER
had already loaded the exact same file, a pack's module-level singletons
(registries, started subprocesses, anything anchored on the module object
itself so it survives across CLI seams) silently existed as two unrelated
copies. windtunnel._cli.module_identity closes this by keying a shared
cache on the source file's resolved absolute path, so whichever loader
reaches a file first wins and the second reuses that exact module object.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

FIXTURE_TEMPLATE = '''
"""Fixture module: a module-level singleton dict, a trivial RuntimePlugin,
and a trivial ScenarioPack, both stashing a reference to the SAME dict —
the shape a real-world pack uses to anchor a shared registry (or any other
module-level singleton) on its own module object so it survives being
loaded via either CLI form."""
from windtunnel.api.pack import ScenarioPack

SINGLETON: dict = {{}}


class _Plugin:
    def build(self, runtime_name, label, soul_path):
        raise NotImplementedError("not used by this test")


PLUGIN = _Plugin()
PLUGIN.singleton = SINGLETON

PACK = ScenarioPack(name={pack_name!r})
PACK.singleton = SINGLETON
'''


def _write_fixture(tmp_path: Path) -> tuple[Path, str]:
    """Writes a fresh, uniquely-named fixture module (a fresh bare module
    name per test avoids colliding with another test's entry already sitting
    in sys.modules under the same name within the same test process)."""
    unique = uuid.uuid4().hex[:10]
    module_name = f"wt_two_copies_fixture_{unique}"
    path = tmp_path / f"{module_name}.py"
    path.write_text(FIXTURE_TEMPLATE.format(pack_name=module_name), encoding="utf-8")
    return path, module_name


class TestPackSourceThenRuntimeDottedPath:
    """--pack-source (file-path form) loads first, --runtime (dotted form)
    loads the SAME file second — the exact order the bug report's repro
    command uses."""

    def test_same_module_object_reused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from windtunnel._cli.runtime_discovery import _resolve_runtime_plugin
        from windtunnel._cli.scenario_discovery import _load_scenario_pack_source

        path, module_name = _write_fixture(tmp_path)
        monkeypatch.syspath_prepend(str(tmp_path))

        pack = _load_scenario_pack_source(f"{path}:PACK")
        plugin = _resolve_runtime_plugin(f"{module_name}:PLUGIN")

        assert pack.singleton is plugin.singleton


class TestRuntimeDottedPathThenPackSource:
    """Same fixture, opposite load order — the fix must not be order-
    dependent."""

    def test_same_module_object_reused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from windtunnel._cli.runtime_discovery import _resolve_runtime_plugin
        from windtunnel._cli.scenario_discovery import _load_scenario_pack_source

        path, module_name = _write_fixture(tmp_path)
        monkeypatch.syspath_prepend(str(tmp_path))

        plugin = _resolve_runtime_plugin(f"{module_name}:PLUGIN")
        pack = _load_scenario_pack_source(f"{path}:PACK")

        assert pack.singleton is plugin.singleton


class TestModuleIdentityCache:
    """Lower-level checks directly against the shared cache helpers."""

    def test_load_module_from_file_is_idempotent_for_the_same_path(
        self, tmp_path: Path
    ) -> None:
        from windtunnel._cli.module_identity import load_module_from_file

        path, _module_name = _write_fixture(tmp_path)

        first = load_module_from_file(path, "_wt_test_pack")
        second = load_module_from_file(path, "_wt_test_pack")

        assert first is second
        assert first.SINGLETON is second.SINGLETON

    def test_dotted_load_reuses_module_loaded_by_file_path_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from windtunnel._cli.module_identity import (
            load_module_by_dotted_path,
            load_module_from_file,
        )

        path, module_name = _write_fixture(tmp_path)
        monkeypatch.syspath_prepend(str(tmp_path))

        by_file = load_module_from_file(path, "_wt_test_pack")
        by_dotted = load_module_by_dotted_path(module_name)

        assert by_file is by_dotted
        assert by_file.SINGLETON is by_dotted.SINGLETON

    def test_file_path_load_reuses_module_loaded_by_dotted_path_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from windtunnel._cli.module_identity import (
            load_module_by_dotted_path,
            load_module_from_file,
        )

        path, module_name = _write_fixture(tmp_path)
        monkeypatch.syspath_prepend(str(tmp_path))

        by_dotted = load_module_by_dotted_path(module_name)
        by_file = load_module_from_file(path, "_wt_test_pack")

        assert by_dotted is by_file
        assert by_dotted.SINGLETON is by_file.SINGLETON
