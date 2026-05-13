"""P3.7 — `retrace --version` contract test.

Tiny but enough to catch the regression of "someone removed the
version_option" or "someone unpinned `__version__` from the
package metadata."
"""

from __future__ import annotations

import re

from click.testing import CliRunner

from retrace import __version__
from retrace.cli import main


def test_retrace_version_long_flag():
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    # Match the configured `%(prog)s %(version)s` template.
    assert result.output.strip() == f"retrace {__version__}"


def test_retrace_version_short_flag():
    runner = CliRunner()
    result = runner.invoke(main, ["-V"])
    assert result.exit_code == 0
    assert result.output.strip() == f"retrace {__version__}"


def test_package_version_is_semver_ish():
    """`__version__` must match a semver-ish pattern so the
    versioning policy in `docs/versioning.md` lines up with reality.
    Pre-stable suffixes (`a1`, `b3`, `rc2`, `.dev0`) are allowed."""
    assert re.match(
        r"^\d+\.\d+\.\d+([a-z]+\d*)?(\.dev\d+)?$", __version__
    ), f"version {__version__!r} doesn't look semver-ish"
