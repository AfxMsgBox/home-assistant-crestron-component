"""Load the component's Home-Assistant-independent modules for testing.

The package's real ``__init__.py`` imports ``homeassistant``, which isn't
available in a bare test environment. Importing ``custom_components.crestron``
directly would therefore fail. Instead we load the individual pure-logic
modules (``crestron``, ``value_coercion``, ``schema``) under a *synthetic*
package so that ``schema.py``'s ``from .crestron import ...`` relative import
still resolves, without ever executing the real ``__init__.py``.
"""

import importlib.util
import sys
import types
from pathlib import Path

_PKG = "crestron_under_test"
_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "crestron"


def _ensure_pkg():
    if _PKG not in sys.modules:
        pkg = types.ModuleType(_PKG)
        # __path__ lets the import machinery resolve submodules (and relative
        # imports like `from .crestron import ...`) against the real directory.
        pkg.__path__ = [str(_DIR)]
        sys.modules[_PKG] = pkg


def load(name):
    """Load ``<crestron>/<name>.py`` as ``crestron_under_test.<name>``."""
    _ensure_pkg()
    full = f"{_PKG}.{name}"
    existing = sys.modules.get(full)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(full, _DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module
