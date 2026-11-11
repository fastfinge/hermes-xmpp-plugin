"""Make the optional call packages (aiortc, av, numpy, webrtcvad) importable.

Order: already importable (installed into the Hermes environment) -> an isolated Hermes
environment built by `pm.ensure_environment` -> a `vendor/` folder next to the plugin, only as the
fallback for Hermes without that API or with lazy installs off (Docker). The environment lives under
the Hermes root, not in the plugin folder or the main venv, so plugin updates and `hermes update`
leave it alone. Both are appended to `sys.path`, so Hermes' own packages win.
"""

from __future__ import annotations

import importlib.util
import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

ENV_NAME = "hermes-xmpp-calls"
_PLUGIN_DIR = Path(__file__).resolve().parent


def requirements() -> list[str]:
    """The packages in requirements-calls.txt: the one list for CI, vendor/ and the environment."""
    lines = (_PLUGIN_DIR / "requirements-calls.txt").read_text(encoding="utf-8").splitlines()
    return [line.split("#")[0].strip() for line in lines if line.split("#")[0].strip()]


def _site_packages(python: str) -> str:
    """The environment's purelib, refused if its Python differs (compiled wheels would not load)."""
    code = "import sys, sysconfig; print(sysconfig.get_paths()['purelib']); print(*sys.version_info[:2])"
    out = subprocess.run([python, "-c", code], capture_output=True, text=True, timeout=60, check=True)
    purelib, version = out.stdout.split("\n")[:2]
    if version != " ".join(map(str, sys.version_info[:2])):
        raise RuntimeError(f"environment Python {version} differs from {sys.version_info[:2]}")
    return purelib


def provide() -> None:
    """Never raises. On failure the caller's import fails and logs what to install by hand."""
    if importlib.util.find_spec("aiortc") is not None:
        return
    vendor = _PLUGIN_DIR / "vendor"
    try:
        import pm  # type: ignore[import-not-found]  # Hermes' package manager; absent in old versions

        root = None
        try:
            from hermes_constants import get_default_hermes_root  # type: ignore[import-not-found]

            # One environment shared by every profile, not one per profile.
            root = Path(get_default_hermes_root()) / "environments" / ENV_NAME
        except ImportError:
            pass
        logger.info("xmpp: preparing the call packages environment (first start can take a minute)")
        python = pm.ensure_environment(ENV_NAME, requirements(), root=root)
        path = _site_packages(str(python))
        if path not in sys.path:
            sys.path.append(path)
    except Exception as exc:
        if vendor.is_dir():
            logger.warning("xmpp: call packages environment unavailable (%s); using %s", exc, vendor)
            sys.path.append(str(vendor))
            return
        logger.warning(
            "xmpp: could not prepare the call packages environment (%s). Install them by hand: "
            "uv pip install --python <hermes python> --target %s -r %s",
            exc,
            vendor,
            _PLUGIN_DIR / "requirements-calls.txt",
        )
