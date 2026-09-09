"""
Tests for the EDL LTI 1.3 migration tooling.

edx-platform is not installed in this plugin's test environment (see
``requirements/test.txt``: lxml and edx-opaque-keys are there, ``openedx.*`` /
``cms.*`` / ``xmodule.*`` are not). The migration modules import those at
module level because they have to run inside Studio, so stub modules are
installed here -- in the test package's ``__init__``, which Django's test
discovery imports before any test module inside it, guaranteeing the stubs are
in place first.

Tests then patch attributes on these stubs. Nothing here fakes edx-platform
*behaviour*; the stubs exist only so the modules under test are importable, and
each test supplies the specific return values it needs.
"""
import sys
from types import ModuleType
from unittest import mock


def _install(path, **attrs):
    """Register a stub module at `path` (creating parent packages) with `attrs` on it."""
    module = ModuleType(path)
    for name, value in attrs.items():
        setattr(module, name, value)
    sys.modules[path] = module

    parts = path.split(".")
    for depth in range(1, len(parts)):
        parent_path = ".".join(parts[:depth])
        if parent_path not in sys.modules:
            sys.modules[parent_path] = ModuleType(parent_path)
    if len(parts) > 1:
        setattr(sys.modules[".".join(parts[:-1])], parts[-1], module)


class _UpstreamLinkException(Exception):
    """Stands in for cms.lib.xblock.upstream_sync.UpstreamLinkException."""


class _Branch:
    """Stands in for ModuleStoreEnum.Branch."""

    draft_preferred = "draft-preferred"
    published_only = "published-only"


class _ModuleStoreEnum:
    """Stands in for xmodule.modulestore.ModuleStoreEnum."""

    Branch = _Branch


def install_stubs():
    """Install every edx-platform module the migration code imports at module level."""
    _install(
        "openedx.core.djangoapps.content_libraries.api",
        get_libraries_for_user=mock.MagicMock(name="get_libraries_for_user"),
        get_library_components=mock.MagicMock(name="get_library_components"),
        library_component_usage_key=mock.MagicMock(name="library_component_usage_key"),
        publish_component_changes=mock.MagicMock(name="publish_component_changes"),
        set_library_block_olx=mock.MagicMock(name="set_library_block_olx"),
    )
    _install(
        "openedx.core.djangoapps.xblock.api",
        get_block_draft_olx=mock.MagicMock(name="get_block_draft_olx"),
    )
    _install("cms.lib.xblock.upstream_sync", UpstreamLinkException=_UpstreamLinkException)
    _install(
        "cms.lib.xblock.upstream_sync_block",
        sync_from_upstream_block=mock.MagicMock(name="sync_from_upstream_block"),
    )
    _install("xmodule.modulestore", ModuleStoreEnum=_ModuleStoreEnum)
    _install("xmodule.modulestore.django", modulestore=mock.MagicMock(name="modulestore"))


install_stubs()
