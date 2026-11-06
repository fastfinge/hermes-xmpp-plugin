"""Exercise installed Hermes source without importing runtime config or credentials.

Set HERMES_SOURCE for another checkout. Base helper definitions are compiled
unchanged from AST because importing base loads unrelated optional dependencies.
"""
import ast
import asyncio
import importlib.util
import os
import socket
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from test_voice_notes import adapter

SOURCE = Path(os.environ.get("HERMES_SOURCE") or "")


@pytest.fixture
def real_core(monkeypatch):
    if not os.environ.get("HERMES_SOURCE"):
        pytest.skip("set HERMES_SOURCE to installed Hermes source")
    if not (SOURCE / "tools/url_safety.py").is_file():
        pytest.fail("HERMES_SOURCE does not contain Hermes URL-safety source")
    # These unrelated imports must never access operator config.
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home_override = lambda: None
    utils = types.ModuleType("utils")
    utils.is_truthy_value = lambda value: str(value).lower() in ("true", "yes", "1")
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    monkeypatch.setitem(sys.modules, "utils", utils)
    spec = importlib.util.spec_from_file_location("tools.url_safety", SOURCE / "tools/url_safety.py")
    safety = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(safety)
    monkeypatch.setattr(safety, "_global_allow_private_urls", lambda: False)
    monkeypatch.setattr(safety, "_global_fake_ip_ranges", lambda: ())
    monkeypatch.setitem(sys.modules, "tools.url_safety", safety)
    base = types.ModuleType("gateway.platforms.base")
    tree = ast.parse((SOURCE / "gateway/platforms/base.py").read_text())
    names = {"get_inbound_media_max_bytes", "validate_inbound_media_size"}
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    assert {node.name for node in nodes} == names
    base.__dict__.update(Optional=__import__("typing").Optional,
                         DEFAULT_INBOUND_MEDIA_MAX_BYTES=128 * 1024 * 1024,
                         _or_default=lambda fn, default, errors: default)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "installed-base-helpers", "exec"), base.__dict__)
    monkeypatch.setitem(sys.modules, "gateway.platforms.base", base)
    return safety, base


def test_requested_core_source_cannot_silently_skip(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_SOURCE", str(tmp_path))
    monkeypatch.setitem(globals(), "SOURCE", tmp_path)
    with pytest.raises(pytest.fail.Exception, match="HERMES_SOURCE"):
        getattr(real_core, "__wrapped__")(monkeypatch)


def answers(*ips):
    return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ips]


@pytest.mark.parametrize("ips", [[], ["169.254.169.254"], ["100.100.1.2", "169.254.169.254"], ["::ffff:169.254.169.254"], ["not-an-ip"]])
def test_trusted_real_backend_blocks_floor_before_dial(real_core, monkeypatch, ips):
    safety, _ = real_core
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: answers(*ips))
    async def check():
        async with adapter._open_direct_client(timeout=1, follow_redirects=False) as client:
            backend = client._transport._pool._network_backend
            backend._backend = types.SimpleNamespace(connect_tcp=AsyncMock())
            with pytest.raises(safety.SSRFConnectionBlocked):
                await backend.connect_tcp("upload.example.org", 443)
            backend._backend.connect_tcp.assert_not_called()
    asyncio.run(check())


def test_trusted_real_backend_pins_current_answer_and_dns_failure(real_core, monkeypatch):
    safety, _ = real_core
    calls = iter([answers("100.100.1.2"), answers("169.254.169.254")])
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: next(calls))
    async def check():
        async with adapter._open_direct_client(timeout=1, follow_redirects=False) as client:
            backend = client._transport._pool._network_backend
            dial = AsyncMock(return_value=object())
            backend._backend = types.SimpleNamespace(connect_tcp=dial)
            await backend.connect_tcp("upload.example.org", 443)
            assert dial.call_args.args[0] == "100.100.1.2"
            with pytest.raises(safety.SSRFConnectionBlocked):
                await backend.connect_tcp("upload.example.org", 443)
            assert dial.await_count == 1
            def fail(*args):
                raise socket.gaierror("DNS failed")
            monkeypatch.setattr(socket, "getaddrinfo", fail)
            with pytest.raises(socket.gaierror):
                await backend.connect_tcp("upload.example.org", 443)
            assert dial.await_count == 1
    asyncio.run(check())


@pytest.mark.parametrize("trusted", [True, False])
def test_fetch_imports_actual_core_api(real_core, monkeypatch, trusted):
    safety, base = real_core
    import httpx
    monkeypatch.setattr(safety, "is_always_blocked_url", lambda _: False)
    monkeypatch.setattr(safety, "is_safe_url", lambda _: True)
    built = []
    def client(**kwargs):
        built.append(kwargs)
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b"OggS")), **kwargs)
    monkeypatch.setattr(safety, "create_ssrf_safe_async_client", client)
    if trusted:
        monkeypatch.setattr(adapter, "_open_direct_client", client)
    assert asyncio.run(adapter._fetch_voice_bytes("https://upload.example.org/a.ogg", trusted=trusted)) == b"OggS"
    if not trusted:
        assert built[0]["follow_redirects"] is False
        assert built[0]["trust_env"] is False
