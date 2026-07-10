"""Build hook: include the built frontend ONLY in the standard (installed) wheel.

An editable install must NOT materialize a top-level ``havn/static`` directory
in site-packages: that directory has no ``__init__.py`` (the real package is
served from ``src/havn`` via the editable .pth), so it turns ``havn`` into a
namespace-package portion. Under parallel test workers on Linux that breaks
import resolution of ``havn.server.app`` (routers end up empty; see the
``/api/resources/stream`` nightly failure). Editable installs resolve the
frontend from ``frontend/dist`` at runtime, so they need no force-include.
"""
from __future__ import annotations

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class FrontendIncludeHook(BuildHookInterface):
    PLUGIN_NAME = "havn-frontend"

    def initialize(self, version, build_data):
        # ``standard`` = the real wheel; ``editable`` = pip install -e.
        if self.build_config.builder.PLUGIN_NAME == "wheel" and version != "editable":
            build_data.setdefault("force_include", {})["frontend/dist"] = "havn/static"
