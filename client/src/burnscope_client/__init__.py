"""BurnScope client (v2)."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth: the version is declared once in
    # pyproject.toml and read back here from the installed package
    # metadata, so `__version__` and the build can never drift. Falls
    # back only when imported from a source tree with no install
    # (e.g. `python path/to/script.py` outside the venv).
    __version__ = version("burnscope-client")
except PackageNotFoundError:  # pragma: no cover - source tree without install
    __version__ = "0.0.0+unknown"
