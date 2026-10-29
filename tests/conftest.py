"""Shared test bootstrap for order-independent adapter imports.

Problem this solves: every test file installs its own sys.modules fakes at
module scope (gateway/tools/omemo) and then imports ``adapter``. pytest runs
all files in ONE process, so fakes installed by an earlier-imported file leak
into every later file — e.g. ``test_first_class_features`` mocking
``slixmpp_omemo`` broke ``test_connect_resilience``'s OMEMO test, which
deliberately relies on the real slixmpp-omemo installed in this env.

Conventions for every test file in this suite:

1. Call ``snapshot_sys_modules()`` BEFORE installing your fakes.
2. Install fakes + ``import adapter``.
3. Call ``restore_sys_modules()`` — this removes the fakes you added (they
   are already baked into your module-global ``adapter`` object) and puts
   back whatever was there before, so later files see the real world again.

``evict_adapter_modules()`` drops any cached adapter/gateway copies so a
later file's ``import adapter`` re-executes adapter.py against ITS fakes
rather than inheriting an earlier file's build.

The gateway fakes each file needs differ per file (some need rich
BasePlatformAdapter/MessageEvent fakes), so files keep defining their own —
this conftest only centralizes the snapshot/restore/evict discipline.
"""
from __future__ import annotations

import sys

_ADAPTER_PREFIXES = ("adapter", "gateway", "tools")


def snapshot_sys_modules() -> dict:
    return dict(sys.modules)


def restore_sys_modules(snapshot: dict) -> None:
    """Remove modules added since the snapshot; restore replaced ones."""
    for key in list(sys.modules.keys()):
        if key not in snapshot:
            del sys.modules[key]
        else:
            sys.modules[key] = snapshot[key]


def evict_adapter_modules() -> None:
    """Drop cached adapter/gateway/tools modules so the next import re-executes
    adapter.py against whatever fakes are currently installed."""
    for key in list(sys.modules.keys()):
        root = key.split(".", 1)[0]
        if root in _ADAPTER_PREFIXES:
            del sys.modules[key]


def restore_fake_modules(snapshot: dict, fake_roots: tuple[str, ...] = ("adapter", "gateway", "tools")) -> None:
    """Scoped variant of :func:`restore_sys_modules` for files that keep the
    real dependency modules (slixmpp, slixmpp_omemo, omemo) real.

    The global restore removes EVERY module first imported since the snapshot
    — including real dependency submodules that ``import adapter`` pulled in on
    first import (e.g. ``slixmpp.plugins.base``). Deleting those splits the
    real module graph across two copies: a later re-import gets a FRESH
    ``PLUGIN_REGISTRY``, so a plugin registered through the old copy
    (adapter's ``register_plugin``) is invisible to the new copy
    (``PluginManager.enable``) — a spurious ``PluginNotFound('xep_0384')`` out
    of a perfectly good registration. This helper restores/deletes only the
    faked roots and leaves the real dependency graph intact.
    """
    for key in list(sys.modules.keys()):
        root = key.split(".", 1)[0]
        if root in fake_roots:
            if key in snapshot:
                sys.modules[key] = snapshot[key]
            else:
                del sys.modules[key]
