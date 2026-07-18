"""Canonical module identity for CLI-loaded source files.

`wt run` can load the SAME Python source file through two different CLI
seams: `--pack-source path/to/file.py:attr` (scenario_discovery.py's
file-path form) and `--runtime path/to/file:attr` (runtime_discovery.py's
dotted-module form, used e.g. when a pack module also exposes a
RuntimePlugin). Loaded independently, each seam used to give that ONE file
a DIFFERENT module identity: the file-path form hashed the resolved path
into a synthetic name (`_windtunnel_pack_<digest>`) and exec'd it fresh;
the dotted form imported it under its plain module name via
`importlib.import_module`, which also execs fresh whenever that plain name
isn't already in `sys.modules`. Since the two forms never checked each
other's registered name, the file was executed TWICE, producing two
independent module namespaces — two copies of every module-level
singleton (registries, dicts, started subprocesses, anything a pack
anchors on its own module object to survive across CLI seams). Code
holding a reference into one copy (e.g. a hook registered from it) and
code reading from the other (e.g. a factory the CLI calls) silently
stopped agreeing with each other, with no error at all.

This module is the ONE place both loaders check: a source file's resolved
(symlink-following) absolute path is the identity key, and whichever
loader reaches a given file first wins — the second loader, arriving via
the other CLI form, gets back that exact module object instead of
executing the file again.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

# Keyed by a source file's resolved absolute path. Populated by whichever
# loader below reaches that file first, regardless of which CLI flag
# (--pack-source or --runtime) triggered the load.
_LOADED_BY_REALPATH: dict[str, ModuleType] = {}


def _realpath(path: Path) -> str:
    return str(path.resolve())


def load_module_from_file(path: Path, synthetic_name_prefix: str) -> ModuleType:
    """Load `path` as a module (the `--pack-source path/to/file.py:attr`
    form). Returns the ALREADY-loaded module for this exact resolved path
    if some other CLI seam (e.g. `--runtime` on the same file) got there
    first, instead of executing the file a second time under a fresh
    synthetic name."""
    realpath = _realpath(path)
    cached = _LOADED_BY_REALPATH.get(realpath)
    if cached is not None:
        return cached

    digest = hashlib.sha1(realpath.encode("utf-8")).hexdigest()[:12]
    module_name = f"{synthetic_name_prefix}_{digest}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load module spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    _LOADED_BY_REALPATH[realpath] = module
    return module


def load_module_by_dotted_path(module_name: str) -> ModuleType:
    """Import `module_name` (the `--runtime path/to/file:attr` dotted
    form). Returns the ALREADY-loaded module for this module's backing
    file if some other CLI seam (e.g. `--pack-source` on the same file)
    got there first, instead of letting normal import machinery execute
    the file a second time under this second name.

    Falls back to a plain `importlib.import_module` for anything whose
    backing file can't be resolved up front (namespace packages, frozen/
    built-in modules, or a name that simply doesn't resolve — in which
    case the normal ImportError/ModuleNotFoundError still surfaces)."""
    spec = importlib.util.find_spec(module_name)
    origin = getattr(spec, "origin", None) if spec is not None else None
    if not origin or origin in ("built-in", "frozen"):
        return importlib.import_module(module_name)

    realpath = _realpath(Path(origin))
    cached = _LOADED_BY_REALPATH.get(realpath)
    if cached is not None:
        sys.modules.setdefault(module_name, cached)
        return cached

    module = importlib.import_module(module_name)
    # module.__file__ is the authoritative post-import path (falls back to
    # the pre-import spec origin if a module is unusual enough to omit it).
    resolved = _realpath(Path(getattr(module, "__file__", None) or origin))
    _LOADED_BY_REALPATH.setdefault(resolved, module)
    return module
