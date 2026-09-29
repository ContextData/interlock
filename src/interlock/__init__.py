"""InterLock - Secure agent-to-data runtime proxy for enterprises."""

import re
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version

# The distribution's own metadata is the single source of the version, so an
# installed wheel and the published image report what was actually built
# instead of a literal somebody has to remember to edit. The fallback covers a
# source tree that was never installed, such as a bare `PYTHONPATH=src` run.
try:
    __version__ = _package_version("interlock-runtime")
except PackageNotFoundError:  # pragma: no cover - only without an install
    __version__ = "0.0.0.dev0"

_PEP440_PRERELEASE = re.compile(r"^(?P<base>\d+\.\d+\.\d+)(?P<kind>a|b|rc)(?P<number>\d+)$")


def release_version() -> str:
    """`__version__` in the SemVer form the release tag and chart use.

    Packaging normalises `1.0.0-rc.6` to `1.0.0rc6`, which is right on PyPI and
    wrong everywhere an operator reads it: the tag, the chart version and the
    console all say `1.0.0-rc.6`. Converting back keeps one source of truth
    instead of a display literal that drifts from the packaged version.
    """
    match = _PEP440_PRERELEASE.match(__version__)
    if match is None:
        return __version__
    return f"{match['base']}-{match['kind']}.{match['number']}"
