"""call_packages.provide(): already importable -> vendor/ -> pm environment, and it never raises."""

from __future__ import annotations

import importlib.util
import logging
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def mod(monkeypatch):
    spec = importlib.util.spec_from_file_location("call_packages_under_test", ROOT / "call_packages.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, "path", list(sys.path))  # restore after the test
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None)  # "aiortc is missing"
    monkeypatch.setattr(module, "_PLUGIN_DIR", ROOT)
    return module


def fake_pm(monkeypatch, ensure):
    monkeypatch.setitem(sys.modules, "pm", types.SimpleNamespace(ensure_environment=ensure))


def test_requirements_are_read_from_the_file(mod):
    assert mod.requirements()[0].startswith("aiortc")
    assert all("#" not in line for line in mod.requirements())


def test_environment_site_packages_join_the_end_of_sys_path(mod, monkeypatch):
    seen = {}

    def ensure(name, requirements, root=None):
        seen.update(name=name, requirements=requirements)
        return "/env/bin/python"

    fake_pm(monkeypatch, ensure)
    monkeypatch.setattr(mod, "_site_packages", lambda python: "/env/lib/site-packages")
    before = list(sys.path)
    mod.provide()
    assert sys.path[-1] == "/env/lib/site-packages"
    assert seen["name"] == "hermes-xmpp-calls" and seen["requirements"] == mod.requirements()
    assert sys.path[: len(before)] == before and len(sys.path) == len(before) + 1


def test_environment_wins_over_a_vendor_folder(mod, monkeypatch, tmp_path):
    (tmp_path / "vendor").mkdir()
    monkeypatch.setattr(mod, "_PLUGIN_DIR", tmp_path)
    monkeypatch.setattr(mod, "requirements", lambda: ["aiortc"])
    monkeypatch.setattr(mod, "_site_packages", lambda python: "/env/lib/site-packages")
    fake_pm(monkeypatch, lambda *a, **k: "/env/bin/python")
    mod.provide()
    assert sys.path[-1] == "/env/lib/site-packages" and str(tmp_path / "vendor") not in sys.path


def test_vendor_folder_is_the_fallback_when_the_environment_fails(mod, monkeypatch, tmp_path, caplog):
    (tmp_path / "vendor").mkdir()
    monkeypatch.setattr(mod, "_PLUGIN_DIR", tmp_path)
    monkeypatch.delitem(sys.modules, "pm", raising=False)  # an old Hermes without pm
    monkeypatch.setattr(sys, "meta_path", [])  # so `import pm` cannot be found
    with caplog.at_level(logging.WARNING):
        mod.provide()
    assert sys.path[-1] == str(tmp_path / "vendor") and "unavailable" in caplog.text


def test_failure_logs_the_manual_command_and_does_not_raise(mod, monkeypatch, caplog):
    def refuse(*a, **k):
        raise RuntimeError("lazy installs are disabled")

    fake_pm(monkeypatch, refuse)
    before = list(sys.path)
    with caplog.at_level(logging.WARNING):
        mod.provide()
    assert sys.path == before
    assert "lazy installs are disabled" in caplog.text and "--target" in caplog.text


def test_environment_with_another_python_is_refused(mod, monkeypatch, caplog):
    monkeypatch.setattr(sys, "version_info", (3, 99, 0, "final", 0))
    fake_pm(monkeypatch, lambda *a, **k: sys.executable)
    before = list(sys.path)
    with caplog.at_level(logging.WARNING):
        mod.provide()
    assert sys.path == before and "differs" in caplog.text
