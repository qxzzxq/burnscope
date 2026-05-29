"""`__version__` is single-sourced from pyproject.toml via installed
package metadata. These guard the two ways that can break: someone
re-hard-coding the version (drift), and a wrong distribution name that
would silently ship the `0.0.0+unknown` fallback.
"""

from importlib.metadata import version

import burnscope_client


def test_version_matches_installed_metadata():
    # The only source of truth is the package metadata (pyproject.toml).
    assert burnscope_client.__version__ == version("burnscope-client")


def test_version_is_not_the_unknown_fallback():
    # In any environment where the package is installed — CI, the dev
    # venv, the test runner — the metadata lookup must succeed. The
    # fallback means a broken distribution name or a missing install.
    assert burnscope_client.__version__ != "0.0.0+unknown"
