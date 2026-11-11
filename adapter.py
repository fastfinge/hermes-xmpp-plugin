"""XMPP (Jabber) platform adapter.

Built on slixmpp. Connects to any XMPP server, supports 1:1 chats and MUC
groupchat, and uses XEP-0363 (HTTP File Upload) for attachments.

Encryption posture (ADR-0002): TLS-to-server is always on. When
`omemo_enabled` is true (the default) and slixmpp-omemo is installed,
outbound 1:1 and MUC private messages are encrypted with OMEMO where the
recipient has published device keys. Inbound OMEMO messages are decrypted
automatically.

Packaged as a third-party Hermes platform plugin.
"""
from __future__ import annotations

# Hard dependency: slixmpp itself (the `from slixmpp...` imports below would
# otherwise raise ImportError and break the whole adapter module, and the
# gateway with it). Only `slixmpp` is checked here — `aiohttp` is a
# transitive dep slixmpp's own XEP-0363 plugin pulls in, and slixmpp-omemo /
# omemo are optional (the adapter already degrades to TLS-only when they're
# missing; see SLIXMPP_OMEMO_AVAILABLE below).
#
# Most installs get slixmpp from this plugin's requirements.txt (see
# README.md: `uv pip install -r requirements.txt`) and never hit anything
# below. The self-heal path is opt-in local infrastructure some operators
# run to survive `hermes update` wiping third-party plugin deps — it is
# NOT part of this repo and does not exist on a fresh install, so its
# absence must never be treated as a fatal error.
try:
    import slixmpp as _slixmpp_probe  # noqa: F401
    _XMPP_DEPS_OK = True
except ImportError:
    _XMPP_DEPS_OK = False
    try:
        import sys as _sys, os as _os
        _sys.path.insert(0, _os.path.expanduser("~/.hermes/plugins/_shared"))
        from lazy_install import ensure as _hermes_lazy_ensure

        _XMPP_DEPS_OK = _hermes_lazy_ensure(
            "hermes-xmpp",
            pip_specs=(
                "slixmpp==1.15.0",
                # CVE-2026-34993 (CookieJar.load deserialization, fixed 3.14.0) +
                # CVE-2026-54273/54274/54280 (DoS via unbounded pipelining/websocket
                # frames/unclosed payloads, fixed 3.14.1).
                "aiohttp==3.14.1",
                "slixmpp-omemo",
                "omemo",
            ),
            imports=("slixmpp", "aiohttp", "slixmpp_omemo", "omemo"),
        )
    except Exception:  # noqa: BLE001 — the self-heal hook is optional; never fatal on its own
        _XMPP_DEPS_OK = False


import asyncio
import inspect
import json
import logging
import math
import mimetypes
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if not _XMPP_DEPS_OK:
    # We can't continue — the imports below are required for the adapter to
    # even parse. Surface a clear error rather than a cryptic ImportError
    # deep in the gateway boot path.
    raise ImportError(
        "hermes-xmpp-plugin: slixmpp is not installed. Install with: "
        "uv pip install -r requirements.txt (from the plugin directory), or: "
        "uv pip install 'slixmpp==1.15.0' 'aiohttp==3.14.1' slixmpp-omemo omemo"
    )

from slixmpp.clientxmpp import ClientXMPP
from slixmpp.jid import JID  # type: ignore[import-untyped]
from slixmpp.plugins import register_plugin  # type: ignore[import-untyped]
from slixmpp.stanza import Message  # type: ignore[import-untyped]

# ----------------------------------------------------------------
# slixmpp-omemo imports — guarded but present at runtime on this host
# ----------------------------------------------------------------

if TYPE_CHECKING:
    from omemo.storage import Just, Maybe, Nothing, Storage  # type: ignore[import-untyped]
    from omemo.types import DeviceInformation, JSONType  # type: ignore[import-untyped]
    from slixmpp_omemo import TrustLevel, XEP_0384  # type: ignore[import-untyped]

try:
    from slixmpp_omemo import TrustLevel, XEP_0384
    from omemo.storage import Just, Maybe, Nothing, Storage
    from omemo.types import DeviceInformation, JSONType

    SLIXMPP_OMEMO_AVAILABLE = True
except ImportError:
    SLIXMPP_OMEMO_AVAILABLE = False
    XEP_0384 = None  # type: ignore[misc]
    Storage = None   # type: ignore[misc]
    JSONType = None  # type: ignore[misc]

from gateway.config import Platform, PlatformConfig  # pyright: ignore[reportMissingImports]
from gateway.platforms.base import (  # pyright: ignore[reportMissingImports]
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
)

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------
# Lazy dependency helper for slixmpp
# ----------------------------------------------------------------

def check_xmpp_requirements() -> bool:
    """Confirm the [xmpp] extra is installed."""
    try:
        import slixmpp as _slixmpp
    except ImportError:
        return False
    return True


# ----------------------------------------------------------------
# OMEMO storage (JSON file backed)
# ----------------------------------------------------------------

if SLIXMPP_OMEMO_AVAILABLE:
    class _StorageImpl(Storage):  # type: ignore[misc]
        """Simple JSON-file backed OMEMO storage."""

        def __init__(self, json_file_path: Path) -> None:
            super().__init__()  # type: ignore[misc]
            self._path = json_file_path
            self._data: Dict[str, Any] = {}
            try:
                with open(self._path, encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception:
                pass

        async def _load(self, key: str) -> Any:  # type: ignore[override]
            if key in self._data:
                return Just(self._data[key])
            return Nothing()

        async def _store(self, key: str, value: Any) -> None:  # type: ignore[override]
            self._data[key] = value
            self._save()

        async def _delete(self, key: str) -> None:
            self._data.pop(key, None)
            self._save()

        def _save(self) -> None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)

    class _XEP_0384Impl(XEP_0384):  # type: ignore[misc,valid-type]
        """Concrete OMEMO plugin with BTBV and JSON-file storage."""

        default_config = {
            "fallback_message": "This message is OMEMO encrypted.",
            "json_file_path": None,
        }

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)  # type: ignore[misc]
            self.__storage = None  # type: ignore[var-annotated]

        def plugin_init(self) -> None:
            if not self.json_file_path:  # type: ignore[attr-defined]
                raise Exception("OMEMO JSON file path not specified.")
            self.__storage = _StorageImpl(Path(self.json_file_path))  # type: ignore[attr-defined]
            super().plugin_init()  # type: ignore[misc]

        @property
        def storage(self):
            return self.__storage

        @property
        def _btbv_enabled(self) -> bool:
            return True

        async def _devices_blindly_trusted(  # type: ignore[override]
            self,
            blindly_trusted,
            identifier,
        ) -> None:
            logger.info("OMEMO: blindly trusted %d device(s) [%s]", len(blindly_trusted), identifier)

        async def _prompt_manual_trust(  # type: ignore[override]
            self,
            manually_trusted,
            identifier,
        ) -> None:
            # BTBV is enabled so this is rare. Log and auto-distrust to avoid blocking.
            session_manager = await self.get_session_manager()
            for device in manually_trusted:
                logger.warning(
                    "OMEMO: manual trust required for %s %s — distrusting to avoid block",
                    device.bare_jid,
                    device.device_id
                )
                await session_manager.set_trust(
                    device.bare_jid,
                    device.identity_key,
                    TrustLevel.DISTRUSTED.value
                )

        async def get_session_manager(self):  # type: ignore[override]
            """Return a usable OMEMO session manager, recovering from failed init.

            slixmpp-omemo caches the in-flight initialization task. If that task
            fails once (for example, a server times out while fetching a twomemo
            device list), later decrypt/encrypt attempts await the same failed
            task forever. That makes one transient PubSub hiccup poison OMEMO
            until the whole gateway restarts. Reset the cached task on failure.

            Some servers/clients still behave much better with legacy OMEMO
            (oldmemo) than OMEMO:2 (twomemo). If initialization fails while
            touching the twomemo namespace, fall back to an oldmemo-only session
            manager so existing Conversations/Gajim-style devices can still
            decrypt instead of getting a useless "message from myself" blob.
            """
            # Heal a previously cancelled/failed cached init task BEFORE
            # awaiting it. CancelledError is a BaseException in py3.8+, so the
            # `except Exception` below never catches it — a single timed-out
            # cron delivery that cancels the shared init task would otherwise
            # poison OMEMO (sends + decrypts) until a full gateway restart.
            # Observed in the wild 2026-07-18.
            self._heal_cancelled_session_manager_task()
            try:
                # Shield the shared init from caller cancellation: a timed-out
                # delivery must abort ITS send, not kill the one init task
                # every other send/decrypt is waiting on.
                return await asyncio.shield(super().get_session_manager())  # type: ignore[misc]
            except asyncio.CancelledError:
                # Our caller was cancelled, or the cached task was already
                # cancelled. Clear the cache so the NEXT call can re-init,
                # then propagate the cancellation.
                self._heal_cancelled_session_manager_task()
                raise
            except Exception as exc:
                self._reset_failed_session_manager()
                if "urn:xmpp:omemo:2" in str(exc):
                    logger.warning(
                        "OMEMO: twomemo initialization failed (%s); falling back to legacy OMEMO only",
                        exc,
                    )
                    try:
                        manager = await self._create_oldmemo_only_session_manager()
                        setattr(self, "_XEP_0384__session_manager", manager)
                        self.xmpp.event("omemo_initialized")  # type: ignore[attr-defined]
                        return manager
                    except Exception:
                        self._reset_failed_session_manager()
                        logger.exception("OMEMO: legacy fallback initialization failed")
                raise

        def _heal_cancelled_session_manager_task(self) -> None:
            """Clear the cached OMEMO init task if it already died.

            slixmpp-omemo caches its init task forever. If that task finished
            in a cancelled or failed state, every future get_session_manager()
            re-awaits the same corpse and re-raises CancelledError, which the
            Exception-based recovery path never sees. Only a DONE task in a
            bad state is cleared — a still-running init is left alone so
            concurrent callers keep sharing it.
            """
            task = getattr(self, "_XEP_0384__session_manager_task", None)
            if task is None or not getattr(task, "done", lambda: True)():
                return
            failed = task.cancelled() or (task.exception() is not None)
            if failed:
                logger.warning(
                    "OMEMO: cached session manager init task is dead "
                    "(cancelled=%s); resetting so next use re-initializes",
                    task.cancelled(),
                )
                setattr(self, "_XEP_0384__session_manager_task", None)
                setattr(self, "_XEP_0384__session_manager", None)

        def _reset_failed_session_manager(self) -> None:
            task = getattr(self, "_XEP_0384__session_manager_task", None)
            if task is not None and not getattr(task, "done", lambda: True)():
                task.cancel()
            setattr(self, "_XEP_0384__session_manager_task", None)
            setattr(self, "_XEP_0384__session_manager", None)

        async def _create_oldmemo_only_session_manager(self):
            from slixmpp_omemo.xep_0384 import _make_session_manager  # type: ignore[import-untyped]
            from oldmemo.oldmemo import Oldmemo  # type: ignore[import-untyped]

            session_manager_cls = _make_session_manager(self.xmpp, self)  # type: ignore[attr-defined]
            manager = session_manager_cls.__new__(session_manager_cls)
            storage = self.storage
            if storage is None:
                raise RuntimeError("OMEMO storage is not initialized")
            backend = Oldmemo(storage)
            name_map = {
                "__backends": [backend],
                "__storage": storage,
                "__own_bare_jid": self.xmpp.boundjid.bare,  # type: ignore[attr-defined]
                "__undecided_trust_level_name": TrustLevel.UNDECIDED.value,
                "__synchronizing": False,
            }
            for name, value in name_map.items():
                setattr(manager, f"_SessionManager{name}", value)
            own_device_id = (await storage.load_primitive("/own_device_id", int)).from_just()
            setattr(manager, "_SessionManager__own_device_id", own_device_id)
            return manager

else:
    _StorageImpl = None  # type: ignore[misc,assignment]
    _XEP_0384Impl = None  # type: ignore[misc,assignment]

# ----------------------------------------------------------------
# MUC room helpers
# ----------------------------------------------------------------

@dataclass
class _MucRoom:
    """A configured MUC room the adapter joins on connect."""
    room: str
    nick: Optional[str] = None


def _parse_muc_rooms(value: str, default_nick: Optional[str]) -> List[_MucRoom]:
    rooms: List[_MucRoom] = []
    for entry in (value or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "/" in entry:
            room, _, nick = entry.partition("/")
            rooms.append(_MucRoom(room=room.strip(), nick=nick.strip() or default_nick))
        else:
            rooms.append(_MucRoom(room=entry, nick=default_nick))
    return rooms


def _has_working_ipv6_route() -> bool:
    """Cheap heuristic: a global (non-link-local, non-ULA) default IPv6 route
    *and* a global source address suggest v6 actually works. A bare default
    route from a broken RA without upstream v6 (observed here: v6 SYNs simply
    time out) fails the source-address check — better to fall back to IPv4
    than to hang AAAA-first connection attempts forever."""
    try:
        import socket as _socket

        has_default = False
        try:
            with open("/proc/net/if_inet6", "r") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 6:
                        addr_hex, scope = parts[0], parts[4]
                        # scope 0x00 = global; skip fe80::/10 link-local and
                        # fc00::/7 ULA (no guaranteed global reachability)
                        if scope == "00" and not addr_hex.lower().startswith(("fe8", "fe9", "fea", "feb", "fc", "fd")):
                            has_default = True
                            break
        except OSError:
            return False
        if not has_default:
            return False
        _s = _socket.socket(_socket.AF_INET6, _socket.SOCK_DGRAM)
        try:
            _s.connect(("2001:4860:4860::8888", 53))
            src = _s.getsockname()[0]
            return not (src.startswith("fe80") or src.startswith("fc") or src.startswith("fd"))
        finally:
            _s.close()
    except Exception:
        return False


def _find_omemo_plugin(client: Any) -> Any:
    """Return the client's registered xep_0384 plugin instance, or None.

    Walks slixmpp's PluginManager registry directly: the public
    ``client['xep_0384']`` accessor logs warnings and returns False for
    anything not loaded, and auto-mocks under MagicMock clients. Anything
    without a real plugin registry — test fakes, mocks, future slixmpp
    layouts — yields None, which callers treat as "OMEMO was never set up".
    """
    try:
        plugin_manager = getattr(client, "plugin", None)
        plugins = getattr(plugin_manager, "_plugins", None)
        if isinstance(plugins, dict):
            return plugins.get("xep_0384") or None
    except Exception:
        pass
    return None


# ----------------------------------------------------------------
# Adapter
# ----------------------------------------------------------------

# Defaults of limits that settings can change (xmpp.agent_peer_limit, xmpp.message_tool_limit,
# xmpp.calls.max_calls_per_hour, xmpp.calls.opening_max_chars).
_AGENT_PEER_LIMIT = 4  # messages per 10 minutes accepted from one partner agent
_MESSAGE_TOOL_LIMIT = 10  # xmpp_message sends per recipient per 10 minutes
_CALL_TOOL_LIMIT = 6  # xmpp_call placements per hour
_OPENING_LIMIT = 1500  # characters an agent may speak when it places a call
# Gateway status lines ("⚡ Interrupting...", "⚙️ tool_search...") the other agent's gateway
# sends as chat; answering them would keep two agents talking forever.
_NOTICE_PREFIXES = (
    "⚡ Interrupting", "⚙", "📞", "⏳", "💭", "🔧", "⏩", "🔄", "💾",
    "⏸", "💡", "🔁", "🗜", "📦", "♻️ Gateway",  # paused, first-time tips, compaction, restart
)
# Transient gateway warnings: the partner's turn is still running or retrying, so there is nothing
# to act on yet. Real warnings written by an agent ("⚠️ Disk is almost full") still pass.
_NOTICE_PHRASES = (
    "returned only a silence marker",  # the gateway's reply to a bare [SILENT]
    "I seem to be stuck", "No response from provider", "No first stream event",
    "Model returning empty", "Empty response from model", "Empty/malformed response",
    "trying fallback", "switching to fallback", "Provider unreachable",
    "Provider temporarily unavailable", "Turn aborted by the liveness watchdog",
    "This turn stopped making progress", "Stream interrupted mid tool-call",
    "Retrying in", "Retrying API call", "Primary model restored", "Model fallback:",
    "Tool guardrail halted", "Hermes is restarting", "This conversation is paused",
    "Your message is saved and will be answered",
)


def _is_gateway_notice(text: str) -> bool:
    text = (text or "").lstrip()
    return text.startswith(_NOTICE_PREFIXES) or any(p in text for p in _NOTICE_PHRASES)


_AGENT_PROMPT = (
    "This message comes from another automated agent, not from a person. Do not chat, thank or "
    "acknowledge it. If it asks for work, do it; if it needs no action, your whole reply must be "
    "exactly [SILENT]."
)


class XmppAdapter(BasePlatformAdapter):
    """slixmpp-backed adapter satisfying BasePlatformAdapter."""

    # Per-stanza body length cap (Unicode code-points). XMPP itself defines no
    # protocol-level body limit and slixmpp exposes no negotiated stanza-size
    # value, so this is a conservative default that every common server
    # (Prosody, ejabberd, etc.) accepts with room to spare for OMEMO/base64
    # inflation and XML escaping. Overridable via config/env. The host's
    # streaming consumer reads this attribute (getattr ... "MAX_MESSAGE_LENGTH")
    # and the adapter's own send paths chunk against it too.
    MAX_MESSAGE_LENGTH = 10000

    # send() chunks via _chunk_body(MAX_MESSAGE_LENGTH); declaring this lets a
    # host gateway that checks it skip pre-truncating cron/delivery output
    # before send() ever sees it.
    splits_long_messages = True

    # How long the background _connect_watchdog() waits for the session to
    # establish (auth + resource bind — signaled the instant _on_session_bind
    # fires, before any of its own post-connect housekeeping) before declaring
    # a retryable failure and tearing the connection down.
    #
    # This does NOT block connect()'s return — connect() kicks off the
    # handshake and reports success immediately, then this watchdog runs
    # independently. Two things that don't work here and why:
    #
    # - Blocking connect() on this wait: the gateway connects platforms one
    #   at a time at startup, so blocking here stalls every other platform
    #   behind XMPP for however long the wait takes.
    # - Any fixed value under the gateway's own per-platform connect timeout
    #   (gateway/run.py wraps connect() in its own asyncio.wait_for): if this
    #   value were ever raised past that outer timeout, the outer one wins
    #   the race and cancels connect() at whatever await point it's
    #   suspended on. A previous version of this code awaited the session
    #   directly inside connect() and only caught asyncio.TimeoutError there
    #   — the outer timeout's CancelledError skipped that entirely, leaking
    #   the client/task/lock while the handshake kept running unsupervised
    #   in the background (and eventually succeeding, well after the gateway
    #   had already logged the connect as failed and moved on).
    #
    # Running this as a decoupled background task sidesteps both problems.
    #
    # 180s default: measured directly against a real, reproducible case where
    # the handshake consistently took ~135s end-to-end (136.16s and 134.74s
    # across two independent connects on a freshly booted machine, zero prior
    # connection history). That signature — a fixed ~127-136s delay before
    # the connection suddenly succeeds — matches a blackholed IPv6 route: DNS
    # returns both an A and AAAA record, the IPv6 attempt gets silently
    # dropped somewhere in the network path, the OS exhausts its default TCP
    # SYN retry budget (Linux's default retry count sums to ~127-130s) before
    # falling back to IPv4, which then succeeds immediately. Every smaller
    # timeout tried before this (20s/25s/60s/90s/the operator's own 120s) was
    # never going to be enough. Overridable via XMPP_CONNECT_TIMEOUT_SECS /
    # the connect_timeout_secs config key — raise it further for an even
    # slower network, or lower it once/if the underlying IPv6 routing issue
    # is fixed (which would also make connects noticeably faster).
    _CONNECT_TIMEOUT_SECS = 180.0

    # How long disconnect() waits for slixmpp to flush the send queue before
    # giving up — bounded so an unresponsive server can't hang an unattended
    # cron job on teardown.
    _DISCONNECT_TIMEOUT_SECS = 10.0

    # How long disconnect() waits for each dependency-owned background task
    # (slixmpp's out-filter pump, OMEMO's session-manager init and signed
    # pre-key rotation) to actually finish after being cancelled. The stop
    # signals themselves are plain task.cancel() calls issued before any
    # await, so this bound only limits the confirmation wait — a wedged task
    # can delay teardown, never keep it from stopping the rest (issue #12).
    _TEARDOWN_TIMEOUT_SECS = 5.0

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("xmpp"))
        extra = config.extra or {}

        # Allow operators to tune the per-message length cap. A server with a
        # tighter `max_stanza_size` (or a desire for shorter messages) can lower
        # it; nobody should need to raise it much. Falls back to the class
        # default on any bad value. Guarded on isinstance(dict) so MagicMock
        # configs in tests don't yield a bogus MagicMock-derived int.
        _max_len_raw = None
        if isinstance(extra, dict):
            _max_len_raw = extra.get("max_message_length") or os.getenv("XMPP_MAX_MESSAGE_LENGTH")
        if _max_len_raw:
            try:
                _max_len = int(_max_len_raw)
                if _max_len > 0:
                    self.MAX_MESSAGE_LENGTH = _max_len
            except (TypeError, ValueError, OverflowError):
                logger.warning("xmpp: invalid max_message_length %r, using default", _max_len_raw)

        # Allow operators on a slow/high-latency link to the server to raise
        # the connect-session-establish timeout beyond the 25s default. Falls
        # back to the class default on any bad value.
        _connect_timeout_raw = None
        if isinstance(extra, dict):
            _connect_timeout_raw = extra.get("connect_timeout_secs") or _scoped_getenv("XMPP_CONNECT_TIMEOUT_SECS")
        if _connect_timeout_raw:
            try:
                _connect_timeout = float(_connect_timeout_raw)
                if math.isfinite(_connect_timeout) and _connect_timeout > 0:
                    self._CONNECT_TIMEOUT_SECS = _connect_timeout
            except (TypeError, ValueError, OverflowError):
                logger.warning(
                    "xmpp: invalid connect_timeout_secs %r, using default", _connect_timeout_raw
                )

        self.jid: str = str(extra.get("jid") or _scoped_getenv("XMPP_JID", "")).strip()
        self._password: str = str(extra.get("password") or _scoped_getenv("XMPP_PASSWORD", "")).strip()
        self.host: Optional[str] = extra.get("host") or _scoped_getenv("XMPP_HOST") or None
        try:
            self.port: int = int(extra.get("port") or _scoped_getenv("XMPP_PORT", "5222") or 5222)
        except (TypeError, ValueError, OverflowError):
            logger.warning("xmpp: invalid port, using default")
            self.port = 5222
        # IPv6 tri-state: None = auto-detect a working global v6 route at
        # connect time. Explicit true/false via XMPP_USE_IPV6 / use_ipv6.
        # NOTE: a YAML `use_ipv6: false` arrives as a real Python bool, and
        # `or` would swallow a False literal back into auto-detect — so check
        # key presence explicitly before falling back to the env var.
        _use_ipv6_cfg = extra.get("use_ipv6")
        if _use_ipv6_cfg is None:
            _use_ipv6_cfg = os.getenv("XMPP_USE_IPV6")
        _use_ipv6_raw = str(_use_ipv6_cfg).strip().lower()
        self._use_ipv6: Optional[bool]
        if _use_ipv6_raw in ("1", "true", "yes", "on"):
            self._use_ipv6 = True
        elif _use_ipv6_raw in ("0", "false", "no", "off"):
            self._use_ipv6 = False
        else:
            self._use_ipv6 = None

        # Direct TLS ("TLS from byte one", XEP-0368 — the classic port-5223
        # posture). Auto-enabled when the operator points us at port 5223,
        # overridable either way via XMPP_DIRECT_TLS / direct_tls. slixmpp
        # falls back to STARTTLS if the direct-TLS attempt fails, and
        # plaintext stays refused in every configuration.
        _direct_raw = str(
            extra.get("direct_tls") or os.getenv("XMPP_DIRECT_TLS", "")
        ).strip().lower()
        if _direct_raw in ("1", "true", "yes", "on"):
            self.direct_tls: bool = True
        elif _direct_raw in ("0", "false", "no", "off"):
            self.direct_tls = False
        else:
            self.direct_tls = self.port == 5223

        self.muc_nick: str = extra.get("muc_nick") or os.getenv("XMPP_MUC_NICK") or self._default_nick()
        self.muc_rooms: List[_MucRoom] = _parse_muc_rooms(
            str(extra.get("muc_rooms") or os.getenv("XMPP_MUC_ROOMS", "")), self.muc_nick
        )

        # Allow-list
        allow_all_raw = str(extra.get("allow_all_users", _scoped_getenv("XMPP_ALLOW_ALL_USERS", "")))
        self.allow_all_users: bool = allow_all_raw.strip().lower() in ("1", "true", "yes")
        allowed_env = _csv(extra.get("allowed_users") or _scoped_getenv("XMPP_ALLOWED_USERS", ""))
        self.allowed_users = {self._bare(j.strip()) for j in allowed_env.split(",") if j.strip()}
        muc_allowed = extra.get("muc_allowed_users")
        if muc_allowed is None:
            muc_allowed = _scoped_getenv("XMPP_MUC_ALLOWED_USERS", allowed_env)
        self.muc_allowed_users = {self._bare(j.strip()) for j in _csv(muc_allowed).split(",") if j.strip()}

        # OMEMO
        omemo_cfg = extra.get("omemo", {})
        # Coerce via _truthy so string values from env/config ("False", "0",
        # "no") are honoured. Plain bool(str) treats any non-empty string as
        # True, which silently ignored XMPP_OMEMO_ENABLED=False (issue #5).
        if isinstance(omemo_cfg, dict) and "enabled" in omemo_cfg:
            self._omemo_enabled: bool = _truthy(omemo_cfg.get("enabled"))
        else:
            self._omemo_enabled = _truthy(
                extra.get("omemo_enabled", os.getenv("XMPP_OMEMO_ENABLED", "true"))
            )
        self._omemo_storage_path: str = str(
            (omemo_cfg.get("storage_path") if isinstance(omemo_cfg, dict) else None)
            or extra.get("omemo_storage_path")
            or os.getenv("XMPP_OMEMO_STORAGE_PATH", "")
        ) or str(Path(_hermes_home()) / "xmpp_omemo.json")
        self._omemo_initialized = asyncio.Event()
        self._omemo_initialized_occurred = False

        # True only for the one-shot send_xmpp_message() path: connect as an
        # ephemeral second resource without taking (or releasing) the
        # per-account scoped lock held by the running gateway.
        self._ephemeral_sender = False

        # Lazy state
        self.client: Optional[Any] = None
        self._process_task: Optional[asyncio.Task] = None
        self._connect_watchdog_task: Optional[asyncio.Task] = None
        self._session_ready: Optional[asyncio.Event] = None
        self._session_established = False
        # Set while disconnect() is tearing the client down on purpose, so
        # the "disconnected" handler doesn't mistake a deliberate close for
        # an outage and escalate it to a reconnect (#28919).
        self._closing = False
        self._self_bare = self._bare(self.jid)
        # Audio calls (Jingle RTP). Off by default; see the README "Audio calls" section.
        self._calls_cfg: Dict[str, Any] = _calls_settings(extra)
        self._calls: Optional[Any] = None  # calls.controller.CallController while connected
        self._calls_host: Optional[Any] = None  # calls.hermes_host.HermesCallHost
        self._calls_loop: Optional[asyncio.AbstractEventLoop] = None  # loop that owns the calls
        self._calls_key = ""  # Hermes home this adapter registered its calls under
        self._agent_peers: frozenset = frozenset(  # other agents: never called, rate-limited
            self._bare(j.strip()).lower()
            for j in _csv(extra.get("agent_peers", _scoped_getenv("XMPP_AGENT_PEERS", ""))).split(",")
            if j.strip()
        )
        self._agent_peer_limit: int = _bounded_int(extra.get("agent_peer_limit"), 1, 1000) or _AGENT_PEER_LIMIT
        self._message_tool_limit: int = _bounded_int(extra.get("message_tool_limit"), 1, 1000) or _MESSAGE_TOOL_LIMIT
        self._agent_peer_times: Dict[str, List[float]] = {}
        self._message_tool_times: Dict[str, List[float]] = {}
        self._call_tool_times: List[float] = []
        self._call_tool_lock = threading.Lock()
        self._call_request: tuple = (-1e9, "", "")  # (when, topic, context) of the last CALL REQUEST
        self._known_mucs = {r.room for r in self.muc_rooms}
        # Bare JIDs we have observed sending us 1:1 ("chat") messages. Used to
        # keep _is_muc() from misclassifying a real user as a group when the
        # user's domain happens to match a MUC-style prefix (e.g. Snikket
        # installs to "chat.example.com", so users are alice@chat.example.com).
        self._known_dms: set[str] = set()
        self._registered_plugins: set[str] = set()

        # Reaction state: message_id → original stanza (for lifecycle hooks)
        self._pending_reactions: Dict[str, Any] = {}
        self._reactions_enabled: bool = os.getenv("XMPP_REACTIONS", "true").lower() not in {"false", "0", "no"}

        # XEP-0004 data forms for clarify: many popular clients (Conversations,
        # ConverseJS, older Gajim) don't render jabber:x:data forms inside
        # <message> stanzas.  When the form path is active the body still
        # carries a numbered fallback list, so disabling forms is rarely
        # needed — but operators can force pure-text clarify with this flag.
        self._clarify_forms_disabled: bool = (
            str(extra.get("disable_clarify_forms", os.getenv("XMPP_DISABLE_CLARIFY_FORMS", "")))
            .strip().lower() in ("1", "true", "yes")
        )

        # Inbound voice notes: a message whose only content is one audio
        # attachment (XEP-0066 OOB, XEP-0447 SFS, or an OMEMO aesgcm:// link)
        # is cached locally and handed to Hermes as a VOICE event, so core
        # transcribes it with the configured ``stt`` provider. On by default;
        # set ``voice_transcription: false`` / XMPP_VOICE_TRANSCRIPTION=false
        # to get the old behaviour (ordinary HTTPS URLs arrive as plain text).
        self._voice_transcription_enabled: bool = _truthy(
            extra.get("voice_transcription", _scoped_getenv("XMPP_VOICE_TRANSCRIPTION", "true"))
        )
        # Upload hosts allowed to resolve to private / LAN / tailnet
        # (100.64.0.0/10) addresses when fetching voice notes. Hermes' SSRF
        # policy blocks those ranges, which would break every voice note on
        # a self-hosted server reachable only over LAN or Tailscale. Default:
        # the bot's own XMPP domain and its subdomains (upload.<domain>, …).
        # Everything else still goes through Hermes' full SSRF checks.
        self._voice_trusted_hosts: tuple = _parse_trusted_hosts(
            extra.get("voice_trusted_hosts", _scoped_getenv("XMPP_VOICE_TRUSTED_HOSTS", None)),
            self.jid,
        )

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        # ``is_reconnect`` is part of the BasePlatformAdapter.connect contract:
        # the gateway's reconnect watcher (gateway/run.py) rebuilds a fresh
        # adapter after a fatal error and calls ``connect(is_reconnect=True)``.
        # The adapter MUST accept the kwarg or that recovery path raises
        # TypeError and XMPP can never reconnect. XMPP keeps no client-side
        # offline queue (the server stores offline messages), so there is
        # nothing extra to preserve on a watcher-driven reconnect — we accept
        # the flag and connect normally with a fresh slixmpp client.
        del is_reconnect
        self._closing = False
        self._session_established = False

        # One gateway per account: two adapters logged into the same JID both
        # receive inbound stanzas (the server fans out by resource), so every
        # message would be handled twice. Lock on the normalized bare JID —
        # localpart/domain are case-insensitive. A same-process reacquire
        # succeeds, so watcher-driven reconnects pass. Released from
        # disconnect(), which every failed-connect path below goes through.
        # The one-shot cron sender (send_xmpp_message) skips this: it attaches
        # as a short-lived second resource, which XMPP permits by design.
        if not self._ephemeral_sender:
            lock_jid = self._bare(self.jid).lower()
            if not self._acquire_platform_lock(
                "xmpp", lock_jid, f"XMPP account {lock_jid}"
            ):
                # The base helper leaves the identity set on failure; clear it
                # so a later disconnect() from this never-connected adapter
                # can't release a lock owned by a same-PID profile.
                self._platform_lock_identity = None
                return False

        client = ClientXMPP(self.jid, self._password)
        # IPv6 gate: on hosts with no working global IPv6 route, an AAAA-first
        # address list makes create_connection() hang on the v6 address
        # indefinitely (no per-attempt timeout) — the adapter's connect
        # watchdog then fires xmpp_connect_timeout every cycle while the
        # perfectly reachable IPv4 address is never tried. Parsed in
        # __init__ (self._use_ipv6: True/False/None=auto) alongside the
        # other enable_* knobs; overridable via XMPP_USE_IPV6 / use_ipv6.
        if self._use_ipv6 is True:
            client.use_ipv6 = True
        elif self._use_ipv6 is False:
            client.use_ipv6 = False
        else:  # auto: trust a working global v6 route if one exists
            client.use_ipv6 = _has_working_ipv6_route()
        if not client.use_ipv6:
            logger.info(
                "xmpp: IPv6 disabled for this connection (no working global v6 route, or use_ipv6=false)"
            )
        # Plugins - core
        for plugin in ("xep_0030", "xep_0045", "xep_0066", "xep_0085", "xep_0199", "xep_0203", "xep_0363", "xep_0380"):
            try:
                client.register_plugin(plugin)
                self._registered_plugins.add(plugin)
            except Exception:
                logger.warning("xmpp: failed to register slixmpp plugin %s", plugin)

        # Plugins - first-class features (XEP-0394, 0444, 0004, 0050, 0461, 0447)
        # Lazy-load: if slixmpp doesn't have them the adapter continues without them.
        for plugin in ("xep_0394", "xep_0444", "xep_0004", "xep_0050", "xep_0461", "xep_0446", "xep_0447", "xep_0308", "xep_0424"):
            try:
                client.register_plugin(plugin)
                self._registered_plugins.add(plugin)
                logger.debug("xmpp: registered slixmpp plugin %s", plugin)
            except Exception:
                logger.warning("xmpp: slixmpp plugin %s not available", plugin)

        # OMEMO plugin registration
        omemo_ok = False
        if self._omemo_enabled and SLIXMPP_OMEMO_AVAILABLE:
            try:
                register_plugin(_XEP_0384Impl)
                client.register_plugin(
                    "xep_0384",
                    {"json_file_path": self._omemo_storage_path},
                )
                self._registered_plugins.add("xep_0384")
                client.add_event_handler("omemo_initialized", self._on_omemo_initialized)
                omemo_ok = True
            except Exception:
                logger.exception("xmpp: failed to register OMEMO plugin")
        elif self._omemo_enabled and not SLIXMPP_OMEMO_AVAILABLE:
            logger.warning(
                "xmpp: OMEMO enabled but slixmpp-omemo not installed. "
                "Install with: uv pip install slixmpp-omemo omemo"
            )

        # Enforce TLS, refuse plaintext. ``enable_plaintext = False`` is the
        # load-bearing knob current slixmpp actually reads (its default is
        # already False, but we set it explicitly rather than rely on that).
        # ``enable_direct_tls`` follows the direct-TLS config (auto-on for
        # port 5223) with STARTTLS kept available as a fallback.
        client.enable_starttls = True  # type: ignore[attr-defined,reportAttributeAccessIssue]
        client.enable_direct_tls = self.direct_tls  # type: ignore[attr-defined,reportAttributeAccessIssue]
        client.enable_plaintext = False  # type: ignore[attr-defined,reportAttributeAccessIssue]

        # "session_bind" (fired immediately once resource binding succeeds —
        # see slixmpp/features/feature_bind/bind.py) rather than
        # "session_start". session_start additionally depends on the legacy,
        # OPTIONAL RFC 3921 IQ-based session-establishment round-trip
        # completing (slixmpp/features/feature_session/session.py) — several
        # real servers advertise that feature but never reliably respond to
        # the IQ, in which case session_start NEVER fires even though
        # authentication and resource binding fully succeeded. Observed in
        # production: bind completed and OMEMO initialized successfully, yet
        # session_start never fired, so send_presence() (previously here)
        # never ran — the bot never appeared online and _session_ready never
        # fired, even though the connection was otherwise fully usable.
        # session_bind is what slixmpp_omemo and dozens of built-in slixmpp
        # plugins (xep_0030, xep_0045, xep_0199, ...) key their own
        # post-connect setup off for exactly this reason.
        client.add_event_handler("session_bind", self._on_session_bind)
        if self._calls_cfg["enabled"] and not self._ephemeral_sender:
            await self._await_call_packages()
        self._calls = self._build_calls(client) if not self._ephemeral_sender else None
        if not self._ephemeral_sender:
            self._register_for_tools()
        # slixmpp dispatches MUC stanzas to BOTH "message" and
        # "groupchat_message", so registering _on_message for both would run it
        # twice for every group-chat message. Register "message" only — it
        # already covers private (chat/normal) and groupchat stanzas.
        client.add_event_handler("message", self._on_message)
        client.add_event_handler("disconnected", self._on_disconnected)
        # Handle "failed_all_auth" (fired once, after every SASL mechanism the
        # server offered has been exhausted) rather than "failed_auth" (fired
        # once per *rejected* mechanism). slixmpp moves on to the next
        # mechanism after each failed_auth, so reacting to failed_auth marks
        # the adapter dead even when a later mechanism (e.g. SCRAM after a
        # rejected PLAIN) succeeds — silently poisoning a working connection
        # (#28919).
        client.add_event_handler("failed_all_auth", self._on_failed_all_auth)

        # Presence subscriptions. slixmpp's blanket auto_authorize would
        # approve *anyone*; gate on the allow-list instead so an unauthorized
        # JID can't force a roster entry. auto_subscribe stays off — we send
        # our own subscribe-back only to approved peers so the subscription
        # is mutual (required for OMEMO device-list PEP pushes to reach the
        # peer's client and clear its stale "no OMEMO" cache).
        client.roster.auto_authorize = False
        client.roster.auto_subscribe = False
        client.add_event_handler("presence_subscribe", self._on_subscribe)

        self.client = client
        self._session_ready = asyncio.Event()
        self._omemo_initialized.clear()
        self._omemo_initialized_occurred = False

        # slixmpp >=1.x uses ``connect(host, port)``; try the modern signature
        # first and fall back WITHOUT dropping the configured host/port — the
        # old ``connect(address=(host, port))`` fallback silently discarded
        # XMPP_HOST/XMPP_PORT (unknown kwarg → TypeError → bare connect()),
        # sending the bot to SRV/JID-domain instead of the operator's server.
        if self.host:
            try:
                client.connect(host=self.host, port=self.port)
            except TypeError:
                client.connect(address=(self.host, self.port))
        else:
            client.connect()

        loop = asyncio.get_event_loop()
        self._process_task = loop.create_task(self._run_process())

        # ``connect()`` only kicks off the TCP/TLS/SASL handshake (slixmpp
        # returns a Future, never a bool — there is no synchronous failure to
        # check here) and returns immediately, matching the pre-existing
        # behavior. It deliberately does NOT block waiting for the session to
        # establish: the gateway connects platforms one at a time during
        # startup, and a real server's TLS/SASL negotiation can legitimately
        # take much longer than any fixed bound comfortably fits into a
        # sequential startup (observed in production: 60-90s+) — blocking
        # here would stall every other platform behind XMPP. It also can't
        # be raced against the gateway's own per-platform connect timeout
        # (gateway/run.py wraps connect() in its own asyncio.wait_for): if
        # that outer timeout ever won the race against a long internal wait
        # here, it would inject CancelledError at our await point, skip our
        # cleanup, and leak the client/task/lock while the handshake kept
        # running unsupervised in the background.
        #
        # Instead, a background watchdog independently waits up to
        # _CONNECT_TIMEOUT_SECS for the session and, if it never establishes,
        # escalates via the same fatal-error + notify path used for a live
        # connection dropping (#28919) — so the reconnect watcher still gets
        # a chance to recover a truly-dead connection, without connect()
        # itself ever blocking on it.
        self._connect_watchdog_task = loop.create_task(self._connect_watchdog())

        if omemo_ok:
            logger.info("XMPP adapter: OMEMO enabled (storage: %s)", self._omemo_storage_path)
        else:
            logger.warning(
                "XMPP adapter is running without OMEMO. Messages are encrypted in "
                "transit (TLS) but visible to your XMPP server operator."
            )
        self._mark_connected()
        return True

    def _register_for_tools(self) -> None:
        """The agent tools find this adapter here. `xmpp_message` needs only the connection, so
        this happens whether or not calls are enabled."""
        self._calls_key = _home_key()
        _CALL_ADAPTERS[self._calls_key] = self
        try:
            self._calls_loop = asyncio.get_running_loop()  # connect() runs on it
        except RuntimeError:
            self._calls_loop = None

    async def _await_call_packages(self, timeout: float = 25.0) -> None:
        """Give the call packages a short head start. The first start downloads ~230 MB, and the
        gateway gives connect() 30 s, so a slow build continues in its thread while chat starts;
        calls come with the next reconnect or restart."""
        task = asyncio.ensure_future(asyncio.to_thread(self._provide_call_packages))
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:
            logger.warning(
                "xmpp: the call packages are still being prepared; chat starts now, "
                "calls start after the next reconnect or gateway restart"
            )

    def _provide_call_packages(self) -> None:
        """Blocking on the first start (builds an environment); never raises. See call_packages."""
        try:
            from importlib import import_module

            import_module(f"{__package__}.call_packages" if __package__ else "call_packages").provide()
        except Exception:
            logger.exception("xmpp: could not prepare the call packages")

    def _build_calls(self, client: Any) -> Optional[Any]:
        """Create the call controller when calls are enabled and their dependencies import.

        Never raises: a missing aiortc/av must not take chat down with it.
        """
        self._calls_host = None
        if not self._calls_cfg["enabled"]:
            return None
        try:
            from importlib import import_module

            base = f"{__package__}.calls" if __package__ else "calls"
            controller = import_module(f"{base}.controller")
            host_module = import_module(f"{base}.hermes_host")
            client.register_plugin("xep_0115")
            self._calls_host = host_module.HermesCallHost(
                self, frozenset(self._calls_cfg["allowed_users"])
            )
            config = controller.CallConfig(
                max_seconds=self._calls_cfg["max_seconds"],
                greetings=self._calls_cfg["greeting"],
                filler_after=self._calls_cfg["filler_after"],
                **({"fillers": self._calls_cfg["filler"]} if self._calls_cfg["filler"] is not None else {}),
                filler_cache=Path(_hermes_home()) / "cache" / "xmpp-calls",
                ring_timeout=self._calls_cfg["ring_timeout"],
                silence_ms=self._calls_cfg["silence_ms"],
                vad_level=self._calls_cfg["vad_level"],
                ack_tone=self._calls_cfg["ack_tone"],
                warn_tone=self._calls_cfg["warn_tone"],
                barge_in=self._calls_cfg["barge_in"],
                hangup_grace=self._calls_cfg["hangup_grace"],
                recap=self._calls_cfg["recap"],
                idle_timeout=self._calls_cfg["idle_timeout"],
                log_transcripts=self._calls_cfg["log_transcripts"],
                busy_wait=self._calls_cfg["busy_wait"],
                ring_attempts=self._calls_cfg["ring_attempts"],
                ring_pause=self._calls_cfg["ring_pause"],
            )
            calls = controller.CallController(client, self._calls_host, config)
            calls.install()
            logger.info("xmpp: audio calls enabled")
            return calls
        except Exception as exc:
            logger.warning(
                "xmpp: calls are enabled but unavailable (%s); "
                "install the call packages (see the README, Calls quick start). "
                "Chat continues without calls.",
                exc,
            )
            self._calls_host = None
            return None

    def _note_call_request(self, body: str) -> None:
        """A partner agent asked us to phone the operator ("CALL REQUEST ... topic=x"): keep its
        topic and the text between the first line and "BRIEFING:" (REF, edit playbook), so the
        `xmpp_call` tool can use them even when the model forgets to pass them."""
        if not body.lstrip().startswith("CALL REQUEST"):
            return
        head, _, rest = body.partition("\n")
        topic = re.search(r"topic=([\w.:-]+)", head)
        context = rest.split("BRIEFING:", 1)[0].strip()
        self._call_request = (time.monotonic(), topic.group(1).rstrip(".") if topic else "", context)

    def _agent_peer_may_speak(self, peer: str) -> bool:
        """Loop guard: two agents that answer each other never stop. Accept at most
        `agent_peer_limit` messages per 10 minutes from a partner agent; drop the rest."""
        now = time.monotonic()
        recent = self._agent_peer_times.setdefault(peer, [])
        recent[:] = [t for t in recent if now - t < 600]
        if len(recent) >= self._agent_peer_limit:
            logger.warning("xmpp: dropping message from agent peer %s (over %d per 10 min)", peer, self._agent_peer_limit)
            return False
        recent.append(now)
        return True

    async def _connect_watchdog(self) -> None:
        try:
            await asyncio.wait_for(
                self._session_ready.wait(), timeout=self._CONNECT_TIMEOUT_SECS
            )
        except asyncio.TimeoutError:
            if self._closing or self.has_fatal_error:
                return
            self._set_fatal_error(
                "xmpp_connect_timeout",
                f"XMPP session did not establish within "
                f"{self._CONNECT_TIMEOUT_SECS:g}s — check XMPP_HOST/XMPP_PORT "
                "and server reachability",
                retryable=True,
            )
            await self.disconnect()
            await self._notify_fatal_error()
        except asyncio.CancelledError:
            pass

    async def disconnect(self) -> None:
        self._closing = True
        if self._calls is not None:
            try:
                await self._calls.close()  # hang up open calls while the stream is still up
            except Exception:
                logger.exception("xmpp: closing calls failed")
            self._calls = self._calls_host = None
        if _CALL_ADAPTERS.get(getattr(self, "_calls_key", "")) is self:
            del _CALL_ADAPTERS[self._calls_key]
        # Capture the client once: teardown below must work from this
        # reference even though self.client is cleared unconditionally.
        client = self.client
        try:
            if client is not None:
                # NOTE: an earlier revision also called
                # self.client.cancel_connection_attempt() here, to abort a
                # still-in-flight handshake before calling disconnect() (slixmpp's
                # own disconnect() only cancels the in-flight attempt when
                # self.transport is already set). That was reverted: it
                # correlated with connection attempts going completely silent
                # (no SASL, no bind — nothing) on every subsequent retry in the
                # same process, consistent with the well-known hazard of
                # cancelling a task mid-flight through slixmpp's aiodns/pycares
                # (c-ares) based DNS resolution, which can leave shared resolver
                # state corrupted for the rest of the process. Raw TCP/STARTTLS
                # connectivity from the same machine was independently confirmed
                # fine via `openssl s_client`/`nc`, ruling out network/server
                # causes. An orphaned handshake finishing late on an already
                # torn-down adapter is a real but comparatively minor annoyance
                # (the None-guards elsewhere already make it harmless); silently
                # wedging every future connection attempt in the process is far
                # worse, so plain disconnect() without forced cancellation is the
                # safer default here.
                try:
                    # slixmpp's disconnect() returns a Future that drains the
                    # send queue before closing the stream — it must be awaited.
                    # The gateway's long-lived loop would eventually run it, but
                    # send_xmpp_message()'s caller closes its loop as soon as we
                    # return, silently dropping any still-queued stanza (send
                    # only enqueues, so success was already reported). Guarded
                    # with isawaitable since older slixmpp returned None here.
                    pending = client.disconnect()
                    if inspect.isawaitable(pending):
                        await asyncio.wait_for(
                            pending, timeout=self._DISCONNECT_TIMEOUT_SECS
                        )
                except asyncio.TimeoutError:
                    logger.warning(
                        "xmpp: send-queue flush did not complete within %gs",
                        self._DISCONNECT_TIMEOUT_SECS,
                    )
                except Exception:
                    logger.exception("xmpp: error during disconnect()")
                finally:
                    # Whether the flush completed, timed out, or this teardown
                    # itself was cancelled: the background tasks slixmpp and
                    # slixmpp-omemo started for this client must still be
                    # stopped — nothing else will ever stop them (issue #12).
                    # Runs after the flush because the out-filter pump is what
                    # drains the send queue.
                    await self._teardown_dependency_tasks(client)
            if self._process_task is not None:
                self._process_task.cancel()
                self._process_task = None
            current_task = asyncio.current_task()
            if self._connect_watchdog_task is not None and self._connect_watchdog_task is not current_task:
                self._connect_watchdog_task.cancel()
                self._connect_watchdog_task = None
        finally:
            # Unconditional: a disconnect() cut short by cancellation must
            # not keep the client reference or leave the platform lock held.
            self.client = None
            self._release_platform_lock()
            self._mark_disconnected()

    async def _teardown_dependency_tasks(self, client: Any) -> None:
        """Stop the background tasks that slixmpp and slixmpp-omemo start
        for a client and never stop themselves (issue #12).

        Two independent retention paths pin every past client and OMEMO
        session manager alive across reconnects in a long-running gateway:

        - slixmpp's XMLStream.connect() spawns ``_run_out_filters`` — a
          ``while True`` pump over the send queue
          (slixmpp/xmlstream/xmlstream.py). Nothing cancels it on disconnect;
          the only canceller is XMLStream.__del__, and the pump coroutine's
          own reference to the stream keeps __del__ from ever running. Every
          reconnect therefore left one more pump task behind, each pinning
          its client — together with everything the client accumulated in
          ``__queued_stanzas`` (stanzas send() parked there whenever the
          client had no session; slixmpp only drains that buffer on session
          start). Looking only at adapter.client hides all of this.

        - omemo's SessionManager spawns ``__manage_signed_pre_key_rotation``
          (a ``while True`` signed-pre-key rotation / bundle re-upload loop)
          during initialization. It is stopped only by
          ``SessionManager.shutdown()`` — which slixmpp-omemo's plugin_end()
          merely *schedules* without awaiting, and plugin_end() itself is
          never invoked on a plain disconnect.

        Dropping adapter.client touches neither path: the tasks hold their
        own references to their owners. Teardown therefore (1) cancel()s
        every owned task first — plain synchronous calls, so all stop
        signals are issued even if a later await is cut short by our own
        cancellation — (2) invokes the dependency shutdown API
        (SessionManager.shutdown()) and awaits each task's termination
        within _TEARDOWN_TIMEOUT_SECS, and (3) frees the dead client's
        queued-stanza buffer.

        Everything is attribute-based and defensive: fake clients in tests,
        partially initialized managers (the legacy oldmemo-only fallback has
        neither the rotation task nor the async-framework state shutdown()
        reads), and future slixmpp layout changes all degrade to "nothing to
        stop" — never to a crash, and never to touching tasks belonging to
        other adapters or the gateway itself.
        """
        try:
            plugin = _find_omemo_plugin(client)
            manager = (
                getattr(plugin, "_XEP_0384__session_manager", None)
                if plugin is not None
                else None
            )
            init_task = (
                getattr(plugin, "_XEP_0384__session_manager_task", None)
                if plugin is not None
                else None
            )
            rotation_task = (
                getattr(manager, "_SessionManager__signed_pre_key_management_task", None)
                if manager is not None
                else None
            )
            pump_task = getattr(client, "_run_out_filters", None)
            slow_tasks = getattr(client, "_XMLStream__slow_tasks", None)

            # (1) Stop signals first — synchronous, unconditional.
            for task in (init_task, rotation_task, pump_task):
                self._cancel_task(task)
            if isinstance(slow_tasks, list):
                for task in slow_tasks:
                    self._cancel_task(task)

            # The adapter is done with this client: drop the plugin's cached
            # manager refs up front so no late get_session_manager() caller
            # latches onto a manager that is already being shut down.
            if plugin is not None:
                for attr in (
                    "_XEP_0384__session_manager",
                    "_XEP_0384__session_manager_task",
                ):
                    try:
                        setattr(plugin, attr, None)
                    except Exception:
                        pass

            # (2) Dependency shutdown API + bounded confirmation.
            await self._shutdown_omemo_manager(manager)
            await self._reap_task(rotation_task, "OMEMO signed pre-key rotation task")
            await self._reap_task(init_task, "OMEMO session-manager init task")
            await self._reap_task(pump_task, "slixmpp out-filter pump task")
            if isinstance(slow_tasks, list):
                for task in slow_tasks:
                    await self._reap_task(task, "slixmpp slow out-filter task")

            # (3) Free stanzas the discarded client can never send. These
            # accumulate while a client has no session (failed reconnect
            # cycles, one-shot sends racing their own teardown) and are held
            # against the client for as long as anything references it.
            queued = getattr(client, "_XMLStream__queued_stanzas", None)
            if isinstance(queued, list) and queued:
                logger.debug(
                    "xmpp: dropping %d undeliverable queued stanza(s) of discarded client",
                    len(queued),
                )
                queued.clear()
        except Exception:
            # Teardown must never break disconnect() itself.
            logger.exception("xmpp: error while tearing down client background tasks")

    async def _shutdown_omemo_manager(self, manager: Any) -> None:
        """Invoke the OMEMO library's own SessionManager.shutdown() — the
        lifecycle method that cancels and awaits the signed pre-key rotation
        task — bounded so a wedged manager cannot hang teardown.

        Partially initialized managers (the legacy oldmemo-only fallback,
        which has neither the rotation task nor the async-framework state
        shutdown() reads) raise AttributeError there; there is nothing to
        stop for those and the quiet outcome is expected.
        """
        if manager is None:
            return
        shutdown = getattr(manager, "shutdown", None)
        if shutdown is None:
            return
        try:
            result = shutdown()
            if inspect.isawaitable(result):
                await asyncio.wait_for(
                    result, timeout=self._TEARDOWN_TIMEOUT_SECS
                )
        except asyncio.TimeoutError:
            logger.warning(
                "xmpp: OMEMO session-manager shutdown did not complete within %gs",
                self._TEARDOWN_TIMEOUT_SECS,
            )
        except AttributeError:
            logger.debug(
                "xmpp: OMEMO session manager only partially initialized; nothing to shut down"
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("xmpp: error shutting down OMEMO session manager")

    async def _reap_task(self, task: Any, what: str) -> None:
        """Wait (bounded) for a cancelled task to actually finish, so
        disconnect() only returns once the task is gone — a task that is
        merely *scheduled* to die could outlive the teardown window and keep
        its owner alive. asyncio.wait() is deliberate: awaiting the task
        directly would re-raise its CancelledError here.
        """
        if not isinstance(task, asyncio.Future) or task.done():
            return
        try:
            await asyncio.wait((task,), timeout=self._TEARDOWN_TIMEOUT_SECS)
        except Exception:
            logger.debug("xmpp: %s did not shut down cleanly", what, exc_info=True)

    @staticmethod
    def _cancel_task(task: Any) -> None:
        """cancel() a task/future if that is what it is. The isinstance check
        is on asyncio.Future (Task subclasses it) rather than duck-typed on
        cancel(), so MagicMock stand-ins in tests are never touched.
        """
        if isinstance(task, asyncio.Future) and not task.done():
            task.cancel()

    async def _run_process(self) -> None:
        """slixmpp's process loop."""
        if self.client is None:
            return
        try:
            await self.client.disconnected
        except asyncio.CancelledError:
            # slixmpp's event may not yield during disconnect, so force a
            # disconnect to unblock any internal awaits before the task unwinds.
            if self.client is not None:
                try:
                    self.client.disconnect()
                except Exception:
                    pass
            raise  # re-raise so the task is properly marked cancelled
        except Exception:
            logger.exception("xmpp: process loop crashed")

    async def _on_session_bind(self, _jid: Any) -> None:
        # Unconditional — logs even if self.client is somehow already None,
        # so the next capture proves definitively whether slixmpp even
        # invoked this handler (vs. the handler running but bailing out
        # early, vs. never being invoked at all). A production connection
        # was observed where this trigger appeared to silently never reach
        # readiness despite bind/OMEMO succeeding; see _mark_session_ready
        # for the omemo_initialized fallback added for the same reason.
        logger.info("xmpp: session_bind event received (client set: %s)", self.client is not None)
        await self._mark_session_ready()

    async def _mark_session_ready(self) -> None:
        """Signal readiness and run post-connect housekeeping, idempotently.

        Called from BOTH "session_bind" and "omemo_initialized" — whichever
        fires first wins, the other becomes a no-op via _session_established.
        omemo_initialized is a deliberate redundant trigger: a production
        connection was observed where session_bind's own readiness signal
        never took effect (cause still under investigation) even though the
        stream was fully authenticated and bound — proven by OMEMO
        initializing successfully on that same connection, since OMEMO
        cannot initialize without a working, bound stream. Relying on two
        independent triggers means a single point of failure in either one
        doesn't leave the connection stuck "not ready" forever.
        """
        if self.client is None or self._session_established:
            return
        self._session_established = True
        # Signal readiness FIRST, before literally anything else in this
        # method — including send_presence(). This being called at all means
        # the stream is authenticated and resource-bound, which is the
        # actual definition of "connected" that _connect_watchdog /
        # send_xmpp_message care about. Every other statement below is
        # best-effort post-connect housekeeping (send_presence, get_roster,
        # MUC joins, subscribe loop, ad-hoc commands): each one is wrapped in
        # its own try/except specifically so that NONE of them can prevent
        # this signal from firing. A previous version of this fix put
        # _session_ready.set() after an UNGUARDED send_presence() call — if
        # that raised (observed in production: the readiness signal never
        # fired even though bind/OMEMO succeeded, with slixmpp's own event
        # dispatcher silently swallowing whatever send_presence() threw),
        # nothing after it ever ran and the watchdog timed out a connection
        # that had actually succeeded.
        if self._session_ready is not None:
            self._session_ready.set()
        logger.info("xmpp: marking session ready")
        if self._calls is not None:
            try:
                await self._calls.advertise()  # before presence, so the caps hash covers calls
            except Exception:
                logger.exception("xmpp: advertising call features failed")
        try:
            self.client.send_presence()  # type: ignore[union-attr]
        except Exception:
            logger.exception("xmpp: initial send_presence() failed")
        try:
            await self.client.get_roster()  # type: ignore[union-attr]
        except Exception:
            logger.exception("xmpp: get_roster failed")
        for room in self.muc_rooms:
            try:
                self.client.plugin["xep_0045"].join_muc(room.room, room.nick or self.muc_nick)  # type: ignore[union-attr]
            except Exception:
                logger.exception("xmpp: failed to join MUC %s", room.room)
        # Proactively request a presence subscription to each allow-listed
        # peer. A mutual subscription is what makes the peer's client receive
        # our OMEMO device-list PEP updates (and clears a stale "no OMEMO for
        # this contact" cache); it also lets us see their presence. Only bare
        # JIDs we already trust get an unsolicited request.
        for jid in self.allowed_users:
            try:
                self.client.send_presence(pto=jid, ptype="subscribe")  # type: ignore[union-attr]
            except Exception:
                logger.debug("xmpp: subscribe request to %s failed", jid, exc_info=True)
        # Register ad-hoc commands now that session is active
        try:
            await self._setup_adhoc_commands()
        except Exception:
            logger.debug("xmpp: ad-hoc command setup failed", exc_info=True)

    async def _on_subscribe(self, presence: Any) -> None:
        """Approve an inbound presence-subscription request from an allowed peer.

        Runs the same allow-list gate as inbound messages, so only trusted
        JIDs get into the bot's roster. On approval we also subscribe back
        (if not already) so the subscription is mutual and our OMEMO
        device-list updates push to the peer's client.
        """
        if self.client is None:
            return
        try:
            requester = self._bare(str(presence.get_from()))
        except Exception:
            return
        if not (self.allow_all_users or requester in self.allowed_users):
            logger.debug("xmpp: ignoring subscription request from %s (not allowed)", requester)
            return
        try:
            self.client.send_presence(pto=requester, ptype="subscribed")
            self.client.send_presence(pto=requester, ptype="subscribe")
            logger.info("xmpp: approved presence subscription for %s", requester)
        except Exception:
            logger.debug("xmpp: failed to approve subscription for %s", requester, exc_info=True)

    async def _on_disconnected(self, _event: Any) -> None:
        self._session_established = False
        # A deliberate disconnect(), or a failure we have already reported
        # (e.g. failed_all_auth set a fatal error), needs no escalation —
        # just record the disconnect and let the existing status stand.
        if self._closing or self.has_fatal_error:
            self._mark_disconnected()
            return
        # An unexpected drop of a live connection (server restart, network
        # blip, ...). Escalate to a *retryable* fatal error AND notify the
        # gateway so its reconnect watcher actually retries. Marking the
        # adapter disconnected without notifying would leave the platform
        # down with nothing driving a reconnect — a silently dead bridge in
        # an otherwise-healthy gateway (#28919).
        self._set_fatal_error(
            "xmpp_connection_lost", "XMPP connection lost", retryable=True
        )
        await self._notify_fatal_error()

    async def _on_failed_all_auth(self, _event: Any) -> None:
        # Every SASL mechanism the server offered has been rejected —
        # credentials are wrong or the account is disabled. Non-retryable,
        # and we notify the gateway so the failure actually surfaces instead
        # of leaving a connected-looking but dead adapter (#28919).
        self._set_fatal_error(
            "xmpp_auth_failed",
            "XMPP authentication failed — check XMPP_JID/XMPP_PASSWORD",
            retryable=False,
        )
        await self._notify_fatal_error()

    async def _on_omemo_initialized(self, _event: Any) -> None:
        logger.info("OMEMO: initialized")
        self._omemo_initialized_occurred = True
        self._omemo_initialized.set()
        # Redundant readiness trigger — see _mark_session_ready's docstring.
        await self._mark_session_ready()

    # -----------------------------------------------------------------
    # Inbound
    # -----------------------------------------------------------------

    async def _on_message(self, stanza: Any) -> None:
        try:
            stanza_type = stanza["type"]
            if stanza_type in ("error", "headline"):
                return
            if stanza_type not in ("chat", "groupchat", "normal"):
                return

            from_jid = stanza.get_from()
            from_full = str(from_jid)
            from_bare = getattr(from_jid, "bare", None) or self._bare(from_full)
            from_resource = getattr(from_jid, "resource", "") or ""

            if from_bare == self._self_bare:
                return

            # In a MUC the stanza's `from` is room@host/nick, so the bare-JID
            # check above never matches our own JID. Reflected messages (and
            # MUC history replay on join) carry our own nick as the resource —
            # skip them, otherwise the bot replies to itself in an endless loop.
            if stanza_type == "groupchat":
                room_nick = self._muc_room_nick(from_bare) or self.muc_nick
                if from_resource and from_resource == room_nick:
                    return
                # MUC history replay carries a delay stamp; ignore old messages.
                try:
                    if stanza.get_plugin("delay", check=True) is not None:
                        return
                except (AttributeError, TypeError):
                    pass

            # Authorize before decryption, forms, downloads, or gateway dispatch.
            if stanza_type == "groupchat":
                chat_type, chat_id = "group", from_bare
                user_name = from_resource or None
                user_id = self._muc_real_jid(stanza) or ""
            else:
                chat_type, chat_id = "dm", from_bare
                user_name, user_id = None, from_bare
            if not self._is_authorized(chat_type=chat_type, chat_id=chat_id, user_jid=user_id):
                return
            if chat_type == "dm":
                self._known_dms.add(from_bare)

            body = stanza["body"] or ""
            stanza_to_dispatch = stanza

            # OMEMO decryption
            if self.client is not None and "xep_0384" in self._registered_plugins and SLIXMPP_OMEMO_AVAILABLE:
                client_local = self.client  # type: ignore[assignment]
                xep_0384 = client_local["xep_0384"]
                if xep_0384.is_encrypted(stanza):
                    try:
                        decrypted_stanza, device_info = await xep_0384.decrypt_message(stanza)
                        body = decrypted_stanza.get("body", "") or ""
                        stanza_to_dispatch = decrypted_stanza
                        logger.debug(
                            "OMEMO: decrypted message from %s (device %s)",
                            from_bare, device_info.device_id
                        )
                    except Exception as exc:
                        logger.warning("OMEMO: failed to decrypt message from %s: %s", from_bare, exc)
                        return

            if user_id.lower() in self._agent_peers:
                if _is_gateway_notice(body) or not self._agent_peer_may_speak(user_id.lower()):
                    return
                self._note_call_request(body)  # only decrypted text that passed the loop guard

            # ----------------------------------------------------------------
            # Inbound XEP-0004 form submission (clarify response)
            # ----------------------------------------------------------------
            # When a client renders our data form and the user submits it, the
            # reply arrives as a <message> with an <x xmlns="jabber:x:data">
            # child whose type is "submit".  The body is typically empty, so
            # we must intercept BEFORE the ``if not body: return`` guard
            # below — otherwise the submission is silently dropped.
            #
            # We extract the hidden clarify_id and the answer field, then
            # resolve the clarify through the gateway.  If anything looks
            # wrong we fall through to normal message handling so the user's
            # text (if any) still reaches the agent.
            #
            # Clients that don't support data forms will reply with plain
            # text; that's handled by the text-fallback path in
            # send_clarify() and the gateway's text-intercept mechanism.
            form = None
            try:
                form = stanza_to_dispatch.get("form", None) if stanza_to_dispatch else None
            except Exception:
                pass

            if form is not None:
                try:
                    form_type = form.get("type", "")
                except Exception:
                    form_type = ""
                if form_type == "submit":
                    resolved = self._handle_form_submission(
                        form=form,
                        from_bare=from_bare,
                        from_resource=from_resource,
                        stanza_type=stanza_type,
                    )
                    if resolved:
                        return
                    # Fall through: form submission could not be resolved,
                    # treat as normal message if there's a body.

            # ----------------------------------------------------------------
            # Inbound voice note / single audio attachment → Hermes STT
            # ----------------------------------------------------------------
            # Detected BEFORE the empty-body guard (an SFS-only voice note may
            # carry no body at all) but only downloaded AFTER authorization
            # above, so unknown senders can never make us fetch a URL.
            voice: Optional[_VoiceAttachment] = None
            if self._voice_transcription_enabled:
                try:
                    voice = _extract_voice_attachment(stanza_to_dispatch, body)
                except Exception:
                    logger.debug("xmpp: voice attachment detection failed")
                    voice = None

            if not body and voice is None:
                return

            source = self.build_source(
                chat_id=chat_id,
                chat_type=chat_type,
                user_id=user_id,
                user_name=user_name,
                role_authorized=chat_type == "group",
            )
            # Extract reply context for XEP-0461
            reply_to_message_id = None
            reply_to_text = None
            if self.client is not None and stanza_to_dispatch is not None:
                try:
                    reply_elem = stanza_to_dispatch.get("reply", None)
                    if reply_elem is not None:
                        reply_to_message_id = reply_elem.get("id", None)
                        # Build fallback body from the reply for context injection
                        raw_body = stanza_to_dispatch.get("body", "") or ""
                        # If the reply has fallback markers, strip them
                        if hasattr(reply_elem, "strip_fallback_content"):
                            stripped = reply_elem.strip_fallback_content()
                            if stripped:
                                reply_to_text = stripped
                            else:
                                reply_to_text = raw_body
                        else:
                            reply_to_text = raw_body
                except Exception:
                    pass

            # Reply fallback text can contain attachment keys, even after stripping.
            if reply_to_text and "aesgcm://" in body.lower():
                reply_to_text = None

            if voice is not None:
                audio_path = await self._cache_voice_attachment(voice, from_bare)
                if audio_path:
                    # Empty text + VOICE + an audio/* media type is exactly
                    # what Hermes core's STT pipeline keys on: the gateway
                    # transcribes the file with the operator's configured
                    # ``stt`` provider (local or remote) and uses the
                    # transcript as the user prompt. VOICE (not AUDIO) is
                    # deliberate — core treats AUDIO as a file attachment
                    # and never transcribes it.
                    voice_event = MessageEvent(
                        text="",
                        message_type=_voice_message_type(),
                        source=source,
                        raw_message=stanza_to_dispatch,
                        message_id=stanza.get("id") or None,
                        reply_to_message_id=reply_to_message_id,
                        reply_to_text=reply_to_text,
                    )
                    voice_event.media_urls = [audio_path]
                    voice_event.media_types = [voice.mime]
                    await self.handle_message(voice_event)
                    return
                if not body:
                    return
                # Encrypted URLs carry keys. Never send failed attachments to the LLM.
                if voice.encrypted:
                    return

            # Also cover disabled STT, captions, and unrecognized encrypted files.
            if "aesgcm://" in body.lower():
                return
            event = MessageEvent(
                text=body,
                message_type=MessageType.TEXT,
                source=source,
                raw_message=stanza_to_dispatch,
                message_id=stanza.get("id") or None,
                reply_to_message_id=reply_to_message_id,
                reply_to_text=reply_to_text,
            )
            if user_id.lower() in self._agent_peers:
                event.channel_prompt = _AGENT_PROMPT
            await self.handle_message(event)
        except Exception:
            logger.exception("xmpp: error handling inbound stanza")

    async def _cache_voice_attachment(self, voice: "_VoiceAttachment", sender: str) -> Optional[str]:
        """Download *voice* into Hermes' audio cache; return the local path or None.

        Hosts in ``voice_trusted_hosts`` (default: our own XMPP domain) may
        resolve to LAN/tailnet addresses; every other host gets Hermes'
        full SSRF checks. Either way the ``gateway.max_inbound_media_bytes``
        cap, the cloud-metadata floor and core's container sniffing apply.
        Never raises: on failure the caller falls back to plain text.
        """
        from urllib.parse import urlsplit

        try:
            fetch_url, iv, key = voice.url, b"", b""
            if voice.encrypted:
                fetch_url, iv, key = _parse_aesgcm_url(voice.url)
            host = (urlsplit(fetch_url).hostname or "").lower()
            trusted = _host_is_trusted(host, self._voice_trusted_hosts)
            try:
                data = await _fetch_voice_bytes(fetch_url, trusted=trusted)
            except _UntrustedHostBlocked:
                logger.warning(
                    "xmpp: voice note from %s not fetched: Hermes' URL-safety policy refused "
                    "upload host %r (private, LAN or tailnet address?). If it is your own "
                    "upload service, add it to voice_trusted_hosts / XMPP_VOICE_TRUSTED_HOSTS.",
                    sender, host,
                )
                return None
            if voice.encrypted:
                data = _aesgcm_decrypt(data, iv, key)
            path = await _cache_audio_bytes(data, voice.ext)
            logger.info(
                "xmpp: voice note from %s cached for STT (%d bytes, %s%s%s)",
                sender, len(data), voice.mime,
                ", OMEMO" if voice.encrypted else "",
                ", trusted host" if trusted else "",
            )
            return path
        except ImportError as exc:
            logger.warning(
                "xmpp: voice helper unavailable (%s)", type(exc).__name__,
            )
        except Exception as exc:
            logger.warning(
                "xmpp: could not fetch voice note (%s)", type(exc).__name__,
            )
        return None

    def _handle_form_submission(
        self,
        form: Any,
        from_bare: str,
        from_resource: str,
        stanza_type: str,
    ) -> bool:
        """Process an inbound XEP-0004 form submission (type="submit").

        Extracts the hidden ``clarify_id`` and ``answer`` field from the
        submitted form and resolves the pending clarify through the gateway.

        Returns True if the submission was recognised and resolved (or
        definitively handled), False to fall through to normal message
        processing.
        """
        try:
            fields = form.get_fields(use_dict=True) if hasattr(form, "get_fields") else {}
            if not fields:
                # Try get_values as a fallback
                values = form.get_values() if hasattr(form, "get_values") else {}
                if not values:
                    logger.debug("xmpp: form submission has no fields, ignoring")
                    return False
                clarify_id = values.get("clarify_id")
                answer = values.get("answer")
            else:
                clarify_id_field = fields.get("clarify_id")
                answer_field = fields.get("answer")
                clarify_id = clarify_id_field.get("value") if clarify_id_field else None
                answer = answer_field.get("value") if answer_field else None

            if not clarify_id:
                logger.debug("xmpp: form submission missing clarify_id, ignoring")
                return False

            if answer is None:
                answer = ""

            # The "__other__" sentinel means the user picked "Other" but
            # may not have typed a response.  Treat as empty and let the
            # clarify timeout handle it.
            if answer == "__other__":
                answer = ""

            from tools.clarify_gateway import resolve_gateway_clarify
            resolved = resolve_gateway_clarify(str(clarify_id), str(answer))
            if resolved:
                logger.debug(
                    "xmpp: resolved clarify %s from form submission by %s",
                    clarify_id, from_bare,
                )
                return True
            else:
                logger.debug(
                    "xmpp: form submission for unknown/expired clarify %s from %s",
                    clarify_id, from_bare,
                )
                return True  # Still consume the message — don't double-route
        except Exception:
            logger.exception("xmpp: error processing form submission")
            return False

    def _muc_real_jid(self, stanza: Any) -> Optional[str]:
        # XEP-0045 presence cache is maintained by slixmpp from room presence.
        # Never trust a message's <muc><item jid=...> or nickname as identity.
        try:
            sender = stanza.get_from()
            if self.client is None or "xep_0045" not in self._registered_plugins:
                return None
            jid = self.client["xep_0045"].get_jid_property(JID(sender.bare), sender.resource, "jid")
            if not isinstance(jid, (str, JID)) or not jid:
                return None
            real = JID(str(jid))
            return real.bare if real.user and real.domain else None
        except Exception:
            return None

    def _muc_room_nick(self, room_bare: str) -> Optional[str]:
        """Return the nick we joined a given MUC room under.

        Rooms may be configured with a per-room nick (room/nick); fall back to
        the global muc_nick when no specific match is found.
        """
        for room in self.muc_rooms:
            if room.room == room_bare:
                return room.nick or self.muc_nick
        return self.muc_nick

    def _is_authorized(self, *, chat_type: str, chat_id: str, user_jid: str) -> bool:
        if chat_type == "group":
            return (chat_id in self._known_mucs and bool(user_jid)
                    and self._bare(user_jid) in self.muc_allowed_users)
        if self.allow_all_users:
            return True
        return bool(user_jid) and self._bare(user_jid) in self.allowed_users

    # -----------------------------------------------------------------
    # Outbound
    # -----------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        image_paths: Optional[List[str]] = None,
        voice_path: Optional[str] = None,
        document_path: Optional[str] = None,
        reply_to: Optional[str] = None,
        thread_id: Optional[str] = None,
        message_id: Optional[str] = None,
        disable_web_page_preview: bool = False,
        parse_mode: Optional[str] = None,
        formatting: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if self.client is None or not getattr(self, "_running", True):
            return SendResult(success=False, error="xmpp not connected", retryable=True)

        # Gateway status lines ("⚠️ The model returned only a silence marker...") never go to a
        # partner agent: it would answer them and the two would talk forever.
        if (
            content
            and self._bare(chat_id).lower() in self._agent_peers
            and _is_gateway_notice(content)
        ):
            logger.info("xmpp: not sending a status line to agent peer %s", chat_id)
            return SendResult(success=True)

        # A reply to a spoken call turn is read aloud by the call, not sent as text.
        if (
            self._calls_host is not None
            and content
            and not (voice_path or document_path or image_paths)
            and self._calls_host.capture(self._bare(chat_id), content, reply_to)
        ):
            return SendResult(success=True)

        # Forward legacy media params to dedicated methods
        if voice_path:
            return await self.send_voice(chat_id, voice_path, caption=content or None)
        if document_path:
            return await self.send_document(chat_id, document_path, caption=content or None)
        if image_paths:
            last_result: SendResult | None = None
            for path in image_paths:
                last_result = await self.send_image_file(chat_id, path, caption=content if path == image_paths[0] else None)
                if last_result is not None and not last_result.success:
                    return last_result
            return last_result or SendResult(success=True)

        mtype = "groupchat" if self._is_muc(chat_id) else "chat"

        # OMEMO encrypt when available
        if (
            "xep_0384" in self._registered_plugins
            and SLIXMPP_OMEMO_AVAILABLE
            and mtype == "chat"
        ):
            try:
                return await self._send_encrypted(chat_id, content)
            except Exception as exc:
                logger.warning(
                    "OMEMO: encryption failed for %s (%s), sending plaintext", chat_id, exc
                )
                # fall through to plain send

        try:
            client_local = self.client  # type: ignore[assignment]
            # Split overly long bodies into multiple stanzas. truncate_message
            # (inherited from BasePlatformAdapter) preserves code-fence
            # boundaries and adds "(1/N)" indicators.
            chunks = self._chunk_body(content)
            last_stanza = None
            last_msg_id = None
            # XEP-0085 §5.1: a message carrying a <body> SHOULD include a chat
            # state notification. Embed <active/> on body messages when the
            # plugin is available (consistent with _send_encrypted()).
            include_chat_state = "xep_0085" in self._registered_plugins
            for idx, chunk in enumerate(chunks):
                # Only the first chunk threads as a reply (XEP-0461); the rest
                # are plain follow-up messages. Build each stanza here WITHOUT
                # sending so markup (XEP-0394) can be attached before it goes
                # out; the single .send() happens at the end of the loop body.
                reply_chunk = (
                    idx == 0 and reply_to and "xep_0461" in self._registered_plugins
                )
                if reply_chunk:
                    # slixmpp 1.15 XEP-0461: make_reply(reply_to, reply_id,
                    # **msg_kwargs) where msg_kwargs feed make_message (mto/
                    # mbody required). It returns the stanza WITHOUT sending.
                    stanza = client_local["xep_0461"].make_reply(
                        JID(chat_id),
                        reply_to,
                        mto=chat_id,
                        mbody=chunk,
                        mtype=mtype,
                    )
                else:
                    stanza = client_local.make_message(
                        mto=chat_id, mbody=chunk, mtype=mtype
                    )
                if include_chat_state:
                    try:
                        stanza["chat_state"] = "active"
                    except Exception:
                        pass
                # Attach XEP-0394 markup if available (before sending).
                if "xep_0394" in self._registered_plugins and getattr(stanza, "xml", None) is not None:
                    try:
                        markup = self._build_markup(chunk)
                        if markup is not None:
                            stanza.xml.append(markup.xml)
                    except Exception:
                        logger.debug("xmpp: failed to attach markup", exc_info=True)
                stanza.send()
                last_stanza = stanza
                try:
                    last_msg_id = stanza["id"]
                except Exception:
                    pass
            return SendResult(success=True, message_id=last_msg_id, raw_response=last_stanza)
        except Exception as exc:
            logger.exception("xmpp: send failed")
            return SendResult(success=False, error=str(exc), retryable=True)

    # -----------------------------------------------------------------
    # Message chunking
    # -----------------------------------------------------------------

    def _chunk_body(self, content: str) -> List[str]:
        """Split a message body into stanza-sized chunks.

        Prefers the inherited ``truncate_message`` (preserves code-fence
        boundaries, adds "(1/N)" indicators) against ``MAX_MESSAGE_LENGTH``.
        Falls back to a simple boundary-aware splitter if that helper is
        unavailable (e.g. an older host or a stubbed base in tests). Always
        returns at least one chunk so callers can loop unconditionally; an
        empty body yields a single empty chunk.
        """
        if not content:
            return [content]
        limit = self.MAX_MESSAGE_LENGTH
        if len(content) <= limit:
            return [content]
        truncate = getattr(self, "truncate_message", None)
        if callable(truncate):
            try:
                chunks = list(truncate(content, limit))  # type: ignore[arg-type]
                if chunks:
                    return chunks
            except Exception:
                logger.debug("xmpp: truncate_message failed, using fallback splitter", exc_info=True)
        return self._fallback_split(content, limit)

    @staticmethod
    def _fallback_split(content: str, limit: int) -> List[str]:
        """Simple length-bounded splitter, preferring newline/space boundaries."""
        chunks: List[str] = []
        remaining = content
        while len(remaining) > limit:
            window = remaining[:limit]
            split_at = window.rfind("\n")
            if split_at < limit // 2:
                split_at = window.rfind(" ")
            if split_at < 1:
                split_at = limit
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:].lstrip("\n ")
        if remaining:
            chunks.append(remaining)
        return chunks or [content]

    # -----------------------------------------------------------------
    # XEP-0308 Message Correction (edit_message)
    # -----------------------------------------------------------------

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        """Edit a previously sent message using XEP-0308 Last Message Correction."""
        if self.client is None or not getattr(self, "_running", True):
            return SendResult(success=False, error="xmpp not connected", retryable=True)

        if "xep_0308" not in self._registered_plugins:
            return SendResult(success=False, error="XEP-0308 not available")

        mtype = "groupchat" if self._is_muc(chat_id) else "chat"

        try:
            client_local = self.client
            stanza = client_local["xep_0308"].build_correction(
                id_to_replace=message_id,
                mto=JID(chat_id),
                mtype=mtype,
                mbody=content,
            )

            # OMEMO encrypt for 1:1 chats (same path as send())
            if (
                "xep_0384" in self._registered_plugins
                and SLIXMPP_OMEMO_AVAILABLE
                and mtype == "chat"
            ):
                try:
                    xep_0384 = client_local["xep_0384"]
                    recipient_jid = JID(chat_id)
                    encrypted, errors = await xep_0384.encrypt_message(stanza, {recipient_jid})
                    if errors:
                        logger.info("OMEMO: edit encryption non-critical errors: %s", errors)
                    if encrypted is not None:
                        stanza = encrypted
                        # encrypt_message clear()s the stanza and rebuilds with only
                        # OMEMO elements — manually restore the <replace> element that
                        # was stripped during encryption (see slixmpp-omemo docstring:
                        # "other elements such as read markers are lost and have to be
                        # copied over manually").
                        stanza["replace"]["id"] = message_id
                        # XEP-0380 EME hint for compatibility
                        if "xep_0380" in self._registered_plugins:
                            try:
                                import oldmemo
                                ns = oldmemo.oldmemo.NAMESPACE
                                stanza["eme"]["namespace"] = ns
                                stanza["eme"]["name"] = client_local["xep_0380"].mechanisms[ns]
                            except Exception:
                                pass
                except Exception as exc:
                    logger.warning("OMEMO: edit encryption failed for %s (%s), sending plaintext", chat_id, exc)

            stanza.send()
            msg_id = None
            try:
                msg_id = stanza["id"]
            except Exception:
                pass
            return SendResult(success=True, message_id=msg_id)
        except Exception as exc:
            logger.exception("xmpp: edit_message failed")
            return SendResult(success=False, error=str(exc), retryable=True)

    # -----------------------------------------------------------------
    # XEP-0424 Message Retraction (delete_message)
    # -----------------------------------------------------------------

    async def delete_message(
        self,
        chat_id: str,
        message_id: str,
    ) -> bool:
        """Delete a previously sent message using XEP-0424 Message Retraction."""
        if self.client is None or not getattr(self, "_running", True):
            return False

        if "xep_0424" not in self._registered_plugins:
            return False

        mtype = "groupchat" if self._is_muc(chat_id) else "chat"

        # MUC retractions require the room-assigned <stanza-id> (XEP-0359 /
        # XEP-0424 §5.1), not the client-generated message ID. The adapter
        # does not currently capture reflected room stanzas, so returning
        # False for group chats until MUC stanza-id mapping is implemented.
        if mtype == "groupchat":
            return False

        try:
            self.client["xep_0424"].send_retraction(
                mto=JID(chat_id),
                id=message_id,
                mtype=mtype,
                include_fallback=False,
            )
            return True
        except Exception as exc:
            logger.warning("xmpp: delete_message failed: %s", exc)
            return False

    # -----------------------------------------------------------------
    # XEP-0394 Message Markup helper
    # -----------------------------------------------------------------

    def _build_markup(
        self,
        body: str,
    ) -> Any:
        """Return a slixmpp Markup element with basic formatting hints.

        Supports Markdown-lite in body:
        - **bold** or __bold__ → emphasis
        - `code` or ``code`` → code span
        - ```code block``` → block-code
        """
        try:
            from slixmpp.plugins.xep_0394.stanza import Markup, Span, BlockCode, EmphasisType, CodeType
        except ImportError:
            return None
        markup = Markup(parent=None)
        spans: List[Any] = []
        import re
        for m in re.finditer(r'\*\*(.+?)\*\*|__(.+?)__', body, re.DOTALL):
            start = m.start()
            end = m.end()
            span = Span(parent=markup)
            span["start"] = start
            span["end"] = end
            span.xml.append(EmphasisType(parent=span).xml)
            spans.append(span)
        for m in re.finditer(r'`{1,2}([^`]+)`{1,2}', body, re.DOTALL):
            start = m.start()
            end = m.end()
            span = Span(parent=markup)
            span["start"] = start
            span["end"] = end
            span.xml.append(CodeType(parent=span).xml)
            spans.append(span)
        for m in re.finditer(r'```(.+?)```', body, re.DOTALL):
            start = m.start()
            end = m.end()
            bcode = BlockCode(parent=markup)
            bcode["start"] = start
            bcode["end"] = end
            spans.append(bcode)
        for s in spans:
            markup.xml.append(s.xml)
        return markup if spans else None

    async def _send_encrypted(self, chat_id: str, content: str) -> SendResult:
        """Send an OMEMO-encrypted 1:1 chat message, splitting long bodies.

        Each chunk is encrypted and sent as its own stanza. The result of the
        last chunk is returned; the first failing chunk short-circuits.
        """
        if self.client is None:
            return SendResult(success=False, error="xmpp not connected", retryable=True)
        chunks = self._chunk_body(content)
        last_result = SendResult(success=True)
        for chunk in chunks:
            last_result = await self._send_encrypted_one(chat_id, chunk)
            if not last_result.success:
                return last_result
        return last_result

    async def _send_encrypted_one(self, chat_id: str, content: str) -> SendResult:
        """Encrypt and send a single OMEMO stanza (one chunk)."""
        if self.client is None:
            return SendResult(success=False, error="xmpp not connected", retryable=True)
        client_local = self.client  # type: ignore[assignment]
        xep_0384 = client_local["xep_0384"]
        mtype = "chat"
        stanza = client_local.make_message(mto=chat_id, mtype=mtype)
        stanza["body"] = content
        stanza.set_from(client_local.boundjid)

        recipient_jid = JID(chat_id)
        message, encryption_errors = await xep_0384.encrypt_message(stanza, {recipient_jid})

        if encryption_errors:
            logger.info("OMEMO: encryption non-critical errors: %s", encryption_errors)

        if message is None:
            logger.warning("OMEMO: nothing to encrypt, falling back to plaintext")
            client_local = self.client  # type: ignore[assignment]
            stanza = client_local.send_message(mto=chat_id, mbody=content, mtype=mtype, mchat_state="active")
            msg_id = None
            try:
                msg_id = stanza["id"]
            except Exception:
                pass
            return SendResult(success=True, message_id=msg_id, raw_response=stanza)

        # Explicit Message Encryption (XEP-0380) hint for compatibility.
        # Only set it when the plugin is actually registered; without it the
        # 'eme' interface doesn't exist and accessing it logs an "Unknown
        # stanza interface" warning per slixmpp.
        if "xep_0380" in self._registered_plugins:
            try:
                import oldmemo
                ns = oldmemo.oldmemo.NAMESPACE
                message["eme"]["namespace"] = ns
                message["eme"]["name"] = client_local["xep_0380"].mechanisms[ns]
            except Exception:
                pass

        # Attach XEP-0085 chat state after encryption (encrypt_message clears
        # non-OMEMO elements, so we set it on the encrypted message object).
        if "xep_0085" in self._registered_plugins:
            try:
                message["chat_state"] = "active"
            except Exception:
                pass
        message.send()
        msg_id = None
        try:
            msg_id = message["id"]
        except Exception:
            pass
        return SendResult(success=True, message_id=msg_id, raw_response=message)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        if self.client is None or "xep_0085" not in self._registered_plugins or not getattr(self, "_running", True):
            return
        mtype = "groupchat" if self._is_muc(chat_id) else "chat"
        try:
            msg = self.client.make_message(mto=chat_id, mtype=mtype)
            msg["chat_state"] = "composing"
            msg["no-store"] = True
            msg.send()
        except Exception:
            logger.debug("xmpp: send_typing failed", exc_info=True)

    async def stop_typing(self, chat_id: str) -> None:
        if self.client is None or "xep_0085" not in self._registered_plugins or not getattr(self, "_running", True):
            return
        mtype = "groupchat" if self._is_muc(chat_id) else "chat"
        try:
            msg = self.client.make_message(mto=chat_id, mtype=mtype)
            msg["chat_state"] = "active"
            msg["no-store"] = True
            msg.send()
        except Exception:
            logger.debug("xmpp: stop_typing failed", exc_info=True)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        return await self._upload_and_send(chat_id, image_path, caption)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        # Param name must match BasePlatformAdapter.send_document(file_path=...)
        # — the gateway calls these by keyword (cron/scheduler.py:
        # send_document(chat_id=..., file_path=...)), so a renamed positional
        # arg raises TypeError and the attachment silently never sends.
        return await self._upload_and_send(chat_id, file_path, caption)

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        # Param name must match BasePlatformAdapter.send_video(video_path=...).
        return await self._upload_and_send(chat_id, video_path, caption)

    async def _upload_and_send(
        self, chat_id: str, path: str, caption: Optional[str]
    ) -> SendResult:
        if self.client is None or not getattr(self, "_running", True):
            return SendResult(success=False, error="xmpp not connected", retryable=True)
        if "xep_0363" not in self._registered_plugins:
            return SendResult(
                success=False,
                error="xmpp HTTP File Upload (XEP-0363) not available",
                retryable=False,
            )
        content_type = _guess_content_type(path)
        # XEP-0363 upload_file(): `input_file` must be a binary file object
        # (IO[bytes]), never a path string — slixmpp skips its own open()
        # whenever input_file is not None and calls input_file.seek(0, 2)
        # to size the upload, so a str raised "AttributeError: 'str' object
        # has no attribute 'seek'" and every attachment failed with
        # "couldn't deliver the file attachment" (issue #10). Passing the
        # full path as `filename` alone is sufficient and correct: slixmpp
        # opens the file itself (when input_file is omitted) and derives
        # the slot's basename via os.path.basename().
        upload_kwargs: Dict[str, Any] = {
            "filename": path,
        }
        if content_type:
            upload_kwargs["content_type"] = content_type
        try:
            upload = self.client["xep_0363"].upload_file
            try:
                url = await upload(**upload_kwargs)
            except TypeError:
                upload_kwargs.pop("content_type", None)
                url = await upload(**upload_kwargs)
        except Exception as exc:
            logger.exception("xmpp: HTTP upload (XEP-0363) failed")
            return SendResult(success=False, error=str(exc), retryable=True)

        body = url if not caption else f"{caption}\n{url}"
        mtype = "groupchat" if self._is_muc(chat_id) else "chat"
        try:
            stanza = self.client.send_message(mto=chat_id, mbody=body, mtype=mtype)
            msg_id = None
            try:
                msg_id = stanza["id"]
            except Exception:
                pass
            return SendResult(success=True, message_id=msg_id, raw_response=stanza)
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=True)

    # -----------------------------------------------------------------
    # Lifecycle hooks (reactions)
    # -----------------------------------------------------------------

    def _reactions_allowed(self, event: MessageEvent) -> bool:
        if not self._reactions_enabled:
            return False
        sender = getattr(getattr(event, "source", None), "user_id", None)
        if sender and not self.allow_all_users and self.allowed_users:
            if self._bare(sender) not in self.allowed_users:
                return False
        return True

    def _extract_reaction_target(self, event: MessageEvent) -> Optional[str]:
        return getattr(event, "message_id", None) or None

    def _send_reaction(self, chat_id: str, target_id: str, reactions: list[str]) -> None:
        """Send a reaction message with the correct type per XEP-0444 §4.

        XEP-0444 says message type SHOULD be 'chat' or 'groupchat'.
        Gajim enforces this strictly and rejects type='normal' reactions.
        """
        if self.client is None or "xep_0444" not in self._registered_plugins:
            return
        mtype = "groupchat" if self._is_muc(chat_id) else "chat"
        msg = self.client.make_message(mto=chat_id, mtype=mtype)
        self.client["xep_0444"].set_reactions(msg, target_id, reactions)
        msg.enable("store")
        msg.send()

    async def on_processing_start(self, event: MessageEvent) -> None:
        if not self._reactions_allowed(event):
            return
        target_id = self._extract_reaction_target(event)
        if target_id and self.client is not None and "xep_0444" in self._registered_plugins:
            try:
                self._send_reaction(event.source.chat_id, target_id, ["👀"])
                self._pending_reactions[target_id] = event
            except Exception:
                logger.debug("xmpp: failed to send 👀 reaction", exc_info=True)

    async def on_processing_complete(
        self, event: MessageEvent, outcome: ProcessingOutcome
    ) -> None:
        if self._calls_host is not None:
            self._calls_host.complete(event.source.chat_id, event.message_id)
        if not self._reactions_allowed(event):
            return
        if outcome == ProcessingOutcome.CANCELLED:
            return
        target_id = self._extract_reaction_target(event)
        if not target_id or self.client is None or "xep_0444" not in self._registered_plugins:
            return
        try:
            # Remove in-progress reaction
            self._send_reaction(event.source.chat_id, target_id, [])
        except Exception:
            logger.debug("xmpp: failed to remove 👀 reaction", exc_info=True)
        try:
            if outcome == ProcessingOutcome.SUCCESS:
                self._send_reaction(event.source.chat_id, target_id, ["✅"])
            elif outcome == ProcessingOutcome.FAILURE:
                self._send_reaction(event.source.chat_id, target_id, ["❌"])
        except Exception:
            logger.debug("xmpp: failed to send final reaction", exc_info=True)
        finally:
            self._pending_reactions.pop(target_id, None)

    # -----------------------------------------------------------------
    # Clarify via XEP-0004 Data Forms
    # -----------------------------------------------------------------

    async def send_clarify(
        self,
        chat_id: str,
        question: str,
        choices: Optional[list],
        clarify_id: str,
        session_key: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if self.client is None:
            return SendResult(success=False, error="xmpp not connected", retryable=True)

        if not choices or "xep_0004" not in self._registered_plugins or self._clarify_forms_disabled:
            # Fallback to text-based clarify
            from tools.clarify_gateway import mark_awaiting_text
            if choices:
                lines = [f"❓ {question}", ""]
                for i, choice in enumerate(choices, start=1):
                    lines.append(f"  {i}. {choice}")
                lines.append("")
                lines.append("Reply with the number, the option text, or your own answer.")
                text = "\n".join(lines)
                mark_awaiting_text(clarify_id)
            else:
                text = f"❓ {question}"
            return await self.send(chat_id=chat_id, content=text, metadata=metadata)

        mtype = "groupchat" if self._is_muc(chat_id) else "chat"
        try:
            form = self.client["xep_0004"].make_form(
                ftype="form", title=question, instructions="Select an option and submit."
            )
            form.add_field(
                var="clarify_id",
                ftype="hidden",
                value=clarify_id,
            )
            options = [
                {"label": str(c), "value": str(c)}
                for c in (choices or [])
            ]
            options.append({"label": "Other (type your own answer)", "value": "__other__"})
            form.add_field(
                var="answer",
                ftype="list-single",
                label=question,
                options=options,
            )

            msg = self.client.make_message(mto=chat_id, mtype=mtype)
            # Build a numbered text fallback in the body so clients that
            # don't render jabber:x:data forms (Conversations, ConverseJS,
            # older Gajim) still show actionable choices instead of a bare
            # question string.  Clients that DO render the form will show
            # their native UI; the body is secondary text in that case.
            body_lines = [f"❓ {question}", ""]
            for i, choice in enumerate(choices or [], start=1):
                body_lines.append(f"  {i}. {choice}")
            body_lines.append("")
            body_lines.append("Reply with the number, the option text, or your own answer.")
            msg["body"] = "\n".join(body_lines)
            msg["form"] = form
            msg.send()

            msg_id = None
            try:
                msg_id = msg["id"]
            except Exception:
                pass
            return SendResult(success=True, message_id=msg_id, raw_response=msg)
        except Exception as exc:
            logger.exception("xmpp: send_clarify with data form failed")
            return SendResult(success=False, error=str(exc), retryable=True)

    # -----------------------------------------------------------------
    # Ad-Hoc Commands (XEP-0050)
    # -----------------------------------------------------------------

    async def _setup_adhoc_commands(self) -> None:
        if self.client is None or "xep_0050" not in self._registered_plugins:
            return
        try:
            xep_0050 = self.client["xep_0050"]
            # Register a simple "hermes" command node that lists available actions
            xep_0050.add_command(
                jid=self.client.boundjid,
                node="hermes",
                name="Hermes Agent Commands",
                handler=self._adhoc_hermes_handler,
            )
        except Exception:
            logger.debug("xmpp: failed to register ad-hoc commands", exc_info=True)

    async def _adhoc_hermes_handler(self, iq: Any, session: Dict[str, Any]) -> Dict[str, Any]:
        client = self.client
        if client is None or "xep_0004" not in self._registered_plugins:
            session["notes"] = [("error", "Data forms not available")]
            return session
        try:
            form = client["xep_0004"].make_form(
                ftype="form", title="Hermes Commands", instructions="Choose a command to execute."
            )
            form.add_field(
                var="command",
                ftype="list-single",
                label="Command",
                options=[
                    {"label": "Status", "value": "status"},
                    {"label": "Help", "value": "help"},
                    {"label": "Ping", "value": "ping"},
                ],
            )
            session["payload"] = form
            session["has_next"] = False
            session["next"] = None
            return session
        except Exception:
            session["notes"] = [("error", "Failed to build command list")]
            return session

    # -----------------------------------------------------------------
    # Voice messages via XEP-0447 SFS
    # -----------------------------------------------------------------

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        **kwargs,
    ) -> SendResult:
        # Param name must match BasePlatformAdapter.send_voice(audio_path=...)
        # — cron/scheduler.py calls send_voice(chat_id=..., audio_path=...) by
        # keyword, so a renamed positional arg raises TypeError and the
        # attachment silently never sends.
        if self.client is None or not getattr(self, "_running", True):
            return SendResult(success=False, error="xmpp not connected", retryable=True)
        if "xep_0447" not in self._registered_plugins or "xep_0363" not in self._registered_plugins:
            return await self._upload_and_send(chat_id, audio_path, caption=None)

        content_type = _guess_content_type(audio_path)
        # Same as _upload_and_send(): `input_file` must be a binary stream,
        # not a path string (issue #10) — pass the full path as `filename`
        # only and let slixmpp open the file itself.
        upload_kwargs: Dict[str, Any] = {
            "filename": audio_path,
        }
        if content_type:
            upload_kwargs["content_type"] = content_type
        try:
            upload = self.client["xep_0363"].upload_file
            try:
                url = await upload(**upload_kwargs)
            except TypeError:
                upload_kwargs.pop("content_type", None)
                url = await upload(**upload_kwargs)
        except Exception as exc:
            logger.exception("xmpp: HTTP upload for voice failed")
            return SendResult(success=False, error=str(exc), retryable=True)

        mtype = "groupchat" if self._is_muc(chat_id) else "chat"
        try:
            sfs = self.client["xep_0447"].get_sfs(
                path=Path(audio_path),
                uris=[url],
                media_type=content_type or "audio/ogg",
                desc="Voice message",
                disposition="inline",
            )
            msg = self.client.make_message(mto=chat_id, mtype=mtype)
            # Append the stanza: assigning msg["sfs"] creates an empty plugin.
            msg.append(sfs)
            msg["body"] = url
            msg["oob"]["url"] = url
            from slixmpp.xmlstream import ET
            fallback = ET.Element("{urn:xmpp:fallback:0}fallback", {"for": "urn:xmpp:sfs:0"})
            ET.SubElement(fallback, "{urn:xmpp:fallback:0}body")
            msg.append(fallback)
            msg.send()

            msg_id = None
            try:
                msg_id = msg["id"]
            except Exception:
                pass
            return SendResult(success=True, message_id=msg_id, raw_response=msg)
        except Exception as exc:
            logger.exception("xmpp: send_voice via SFS failed")
            return SendResult(success=False, error=str(exc), retryable=True)

    # -----------------------------------------------------------------
    # Misc
    # -----------------------------------------------------------------

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        chat_type = "group" if self._is_muc(chat_id) else "dm"
        return {"chat_id": chat_id, "type": chat_type, "name": chat_id}

    # MUC-only subdomains by strong convention. "chat." is deliberately NOT
    # here: Snikket (and others) recommend installing to chat.example.com as
    # the primary *user* domain, so alice@chat.example.com is a 1:1 contact,
    # not a room. Membership is driven by _known_mucs / _known_dms instead.
    _MUC_DOMAIN_PREFIXES = ("conference.", "muc.", "rooms.", "groups.")

    def _is_muc(self, chat_id: str) -> bool:
        # Explicit knowledge wins over any heuristic.
        if chat_id in self._known_mucs:
            return True
        if chat_id in self._known_dms:
            return False
        domain = chat_id.split("@", 1)[-1]
        return any(domain.startswith(p) for p in self._MUC_DOMAIN_PREFIXES)

    @staticmethod
    def _bare(jid: str) -> str:
        return jid.split("/", 1)[0] if "/" in jid else jid

    @staticmethod
    def _default_nick() -> str:
        return "hermes"


# ---------------------------------------------------------------------
# Standalone helper
# ---------------------------------------------------------------------

async def send_xmpp_message(
    pconfig: PlatformConfig,
    chat_id: str,
    message: str,
    *,
    thread_id: str | None = None,
    media_files: list[str] | None = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """One-shot send used by cron jobs and the send_message tool."""
    adapter = XmppAdapter(pconfig)
    # Ephemeral resource on the account — must not consult or release the
    # gateway adapter's per-JID scoped lock (compare IRC's "-cron" nick).
    adapter._ephemeral_sender = True
    if not check_xmpp_requirements():
        return {"success": False, "error": "slixmpp not installed"}
    try:
        ok = await adapter.connect()
        if not ok:
            return {"success": False, "error": adapter.fatal_error_message or "connect failed"}
        # connect() now returns as soon as the handshake is kicked off (see
        # _connect_watchdog's docstring for why it no longer blocks on
        # session establishment) — the stream isn't necessarily authenticated
        # yet. Unlike the gateway's long-running adapter, this one-shot
        # sender has nothing else useful to do meanwhile, so it explicitly
        # waits for the session here before attempting to send.
        if adapter._session_ready is not None:
            try:
                await asyncio.wait_for(
                    adapter._session_ready.wait(), timeout=adapter._CONNECT_TIMEOUT_SECS
                )
            except asyncio.TimeoutError:
                return {
                    "success": False,
                    "error": (
                        f"XMPP session did not establish within "
                        f"{adapter._CONNECT_TIMEOUT_SECS:g}s"
                    ),
                }
        last_result = None
        if message:
            last_result = await adapter.send(chat_id=chat_id, content=message)
            if not last_result.success:
                return {"success": False, "error": last_result.error}
        for media_path in media_files or []:
            last_result = await adapter.send_document(chat_id=chat_id, file_path=media_path)
            if not last_result.success:
                return {"success": False, "error": last_result.error}
        return {
            "success": True,
            "platform": "xmpp",
            "chat_id": chat_id,
            "message_id": getattr(last_result, "message_id", None),
        }
    finally:
        await adapter.disconnect()


# ---------------------------------------------------------------------
# Inbound voice notes (single audio attachment → Hermes STT)
# ---------------------------------------------------------------------

_OOB_NS = "jabber:x:oob"
_SFS_NS = "urn:xmpp:sfs:0"
_FILE_META_NS = "urn:xmpp:file:metadata:0"
_URL_DATA_NS = "http://jabber.org/protocol/url-data"

# Extension → MIME for the formats XMPP clients record voice notes in
# (Conversations: .m4a/.oga/.opus, Dino/Gajim/Monal: .ogg/.opus/.m4a/.aac).
# Hermes core re-sniffs the real container before STT, so this map only has
# to decide "is this audio?" and give core a sensible starting extension.
_VOICE_EXT_MIME: Dict[str, str] = {
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".weba": "audio/webm",
    ".amr": "audio/amr",
}
_MIME_VOICE_EXT: Dict[str, str] = {
    "audio/ogg": ".ogg", "audio/opus": ".ogg", "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a", "audio/aac": ".aac", "audio/mpeg": ".mp3",
    "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/flac": ".flac",
    "audio/webm": ".weba", "audio/amr": ".amr",
}


def _guess_content_type(path: str) -> Optional[str]:
    """mimetypes first; the voice table when the system has no mime.types (Python < 3.14 then
    does not know even .ogg, and the upload would go out without a content type)."""
    return mimetypes.guess_type(path)[0] or _VOICE_EXT_MIME.get(os.path.splitext(path)[1].lower())


@dataclass(frozen=True)
class _VoiceAttachment:
    url: str         # https:// (plain) or aesgcm:// (XEP-0454, OMEMO)
    mime: str        # audio/* — fed to MessageEvent.media_types
    ext: str         # starting extension for the cache file
    encrypted: bool  # True for aesgcm:// links


def _voice_message_type() -> Any:
    """MessageType.VOICE, tolerating an enum/fake that lacks it."""
    return getattr(MessageType, "VOICE", "voice")


def _url_path_ext(url: str) -> str:
    from urllib.parse import urlsplit, unquote
    return Path(unquote(urlsplit(url).path)).suffix.lower()


def _classify_audio(url: str, declared_mime: str = "") -> Optional[_VoiceAttachment]:
    """Return a _VoiceAttachment if *url* points at audio, else None."""
    url = (url or "").strip()
    scheme = url.split(":", 1)[0].lower() if ":" in url else ""
    if scheme not in ("https", "aesgcm"):
        return None  # plain http and odd schemes are never auto-fetched
    ext = _url_path_ext(url)
    mime = (declared_mime or "").split(";", 1)[0].strip().lower()
    if mime.startswith("audio/"):
        ext = ext if ext in _VOICE_EXT_MIME else _MIME_VOICE_EXT.get(mime, ".ogg")
    elif mime:
        return None  # sender declared a non-audio type; trust it
    elif ext in _VOICE_EXT_MIME:
        mime = _VOICE_EXT_MIME[ext]
    else:
        return None
    return _VoiceAttachment(url=url, mime=mime, ext=ext, encrypted=(scheme == "aesgcm"))


def _extract_voice_attachment(stanza: Any, body: str) -> Optional[_VoiceAttachment]:
    """Detect a message that is exactly ONE audio attachment and no text.

    Recognised carriers:
      * XEP-0066 OOB ``<x xmlns='jabber:x:oob'><url>`` (Conversations, Dino,
        Gajim, Monal for unencrypted uploads);
      * XEP-0447 SFS ``<file-sharing>`` with an ``audio/*`` media-type;
      * an OMEMO-decrypted body that is a single ``aesgcm://`` link
        (XEP-0454 — encrypted payloads carry no OOB element).

    "No text" means the body is empty or is just the attachment URL (the
    fallback body clients put next to OOB/SFS). A captioned file, several
    attachments, or a non-audio attachment returns None so the message
    takes the normal text path unchanged.
    """
    body_s = (body or "").strip()
    xml = getattr(stanza, "xml", None)

    # Every attachment URL the stanza carries, with any declared MIME type.
    # Read straight from the XML: ``stanza["oob"]`` would *create* an empty
    # <x/> as a side effect on messages that have none.
    found: List[tuple] = []
    if xml is not None:
        for x in xml.findall(f"{{{_OOB_NS}}}x"):
            u = x.find(f"{{{_OOB_NS}}}url")
            if u is not None and (u.text or "").strip():
                found.append((u.text.strip(), ""))
        for fs in xml.findall(f"{{{_SFS_NS}}}file-sharing"):
            mt = fs.find(f"{{{_FILE_META_NS}}}file/{{{_FILE_META_NS}}}media-type")
            declared = (mt.text or "").strip() if mt is not None else ""
            for ud in fs.findall(f"{{{_SFS_NS}}}sources/{{{_URL_DATA_NS}}}url-data"):
                target = (ud.get("target") or "").strip()
                if target:
                    found.append((target, declared))
    if not found and body_s.lower().startswith("aesgcm://") and not any(c.isspace() for c in body_s):
        found.append((body_s, ""))

    # Collapse duplicates (OOB + SFS often advertise the same URL), keeping
    # the most specific declared type.
    by_url: Dict[str, str] = {}
    for url, declared in found:
        by_url[url] = by_url.get(url) or declared
    if len(by_url) != 1:
        return None

    url, declared = next(iter(by_url.items()))
    if body_s and body_s != url:
        return None  # has a caption / real text → not a bare voice note
    return _classify_audio(url, declared)


def _parse_aesgcm_url(url: str) -> tuple:
    """Split an XEP-0454 ``aesgcm://`` URL → (https_url, iv, key).

    The fragment is hex: a 12- or 16-byte IV followed by a 32-byte key.
    """
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(url)
    frag = parts.fragment
    if len(frag) not in (88, 96):
        raise ValueError("aesgcm link has an unexpected key fragment length")
    raw = bytes.fromhex(frag)
    iv, key = raw[:-32], raw[-32:]
    https_url = urlunsplit(("https", parts.netloc, parts.path, parts.query, ""))
    return https_url, iv, key


class _UntrustedHostBlocked(ValueError):
    """Hermes' SSRF policy refused an upload host not in voice_trusted_hosts."""


def _parse_trusted_hosts(raw: Any, jid: str) -> tuple:
    """Normalise ``voice_trusted_hosts`` → tuple of lower-case host patterns.

    ``None`` (unset)        → the bot's JID domain and ``*.<domain>``
    ``"none"``/``"off"``/…  → () — no exceptions to Hermes' SSRF policy
    list or comma string    → exactly those entries; ``*.example.org``
                              matches subdomains only, IP literals match
                              exactly (IPv6 with or without brackets)
    """
    if raw is None:
        domain = (jid or "").split("@")[-1].split("/")[0].strip().lower().rstrip(".")
        return (domain, f"*.{domain}") if domain else ()
    if isinstance(raw, (list, tuple, set)):
        items = [str(x) for x in raw]
    else:
        text = str(raw).strip()
        if text.lower() in ("", "none", "off", "false", "0", "no"):
            return ()
        items = text.split(",")
    out = []
    for item in items:
        h = item.strip().lower().rstrip(".")
        if h.startswith("[") and h.endswith("]"):
            h = h[1:-1]
        if h and h not in out:
            out.append(h)
    return tuple(out)


def _host_is_trusted(host: str, patterns: tuple) -> bool:
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    for pat in patterns:
        if pat.startswith("*."):
            if host.endswith(pat[1:]) and len(host) > len(pat) - 1:
                return True
        elif host == pat:
            return True
    return False


def _open_direct_client(**kwargs: Any) -> Any:
    """Hermes' pinned-IP transport with private resolution allowed only here.

    ponytail: private core backend API; fail closed if unsupported, never use
    plain httpx as fallback. Replace with public per-client policy when available.
    """
    import socket
    from tools.url_safety import (
        create_ssrf_safe_async_client, _SSRFGuardedAsyncNetworkBackend,
        _iter_resolved_ips, _is_always_blocked_ip, SSRFConnectionBlocked,
    )

    class _TrustedBackend(_SSRFGuardedAsyncNetworkBackend):
        def _connect_ips(self, host: str, port: int) -> list[str]:
            answers = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
            ips = []
            for _, text, ip in _iter_resolved_ips(answers):
                if ip is None or _is_always_blocked_ip(ip):
                    raise SSRFConnectionBlocked("refusing invalid or cloud-metadata address")
                if text not in ips:
                    ips.append(text)
            if not ips:
                raise SSRFConnectionBlocked("upload DNS returned no addresses")
            return ips

    # Proxies would resolve remotely and bypass our connection-time floor.
    client = create_ssrf_safe_async_client(trust_env=False, **kwargs)
    pool = client._transport._pool
    backend = pool._network_backend
    pool._network_backend = _TrustedBackend(backend._schemes_by_origin_var)
    return client


async def _fetch_voice_bytes(url: str, *, trusted: bool) -> bytes:
    """GET an https:// voice-note URL under Hermes' inbound media cap.

    Untrusted hosts: Hermes' own SSRF-safe client — pre-flight
    ``is_safe_url``, connect-time IP validation, re-checked redirects.
    Trusted hosts (our own upload service, possibly on a LAN/tailnet
    address): pinned-IP Hermes backend with a private-address exception;
    redirects refused, cloud-metadata floor enforced at connection time.
    """
    from tools.url_safety import (  # pyright: ignore[reportMissingImports]
        create_ssrf_safe_async_client,
        is_always_blocked_url,
        is_safe_url,
    )
    from gateway.platforms.base import (  # pyright: ignore[reportMissingImports]
        get_inbound_media_max_bytes,
        validate_inbound_media_size,
    )
    if trusted:
        client_cm = _open_direct_client(timeout=60.0, follow_redirects=False)
    else:
        client_cm = create_ssrf_safe_async_client(
            timeout=60.0, follow_redirects=False, trust_env=False,
        )
    from urllib.parse import urljoin, urlsplit

    max_bytes = get_inbound_media_max_bytes()
    async with client_cm as client:
        for _ in range(client.max_redirects + 1):
            if urlsplit(url).scheme.lower() != "https":
                raise ValueError("voice attachments require HTTPS")
            if await asyncio.to_thread(is_always_blocked_url, url):
                raise ValueError("refusing cloud-metadata address")
            if not trusted and not await asyncio.to_thread(is_safe_url, url):
                raise _UntrustedHostBlocked("upload host refused by Hermes URL-safety policy")
            async with client.stream("GET", url) as resp:
                if trusted and 300 <= resp.status_code < 400:
                    raise ValueError("trusted upload host redirected; not following")
                if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                    # Closing the stream discards redirect bodies without buffering them.
                    url = urljoin(url, resp.headers["location"])
                    continue
                resp.raise_for_status()
                declared = resp.headers.get("content-length")
                if declared and str(declared).isdigit():
                    validate_inbound_media_size(int(declared), media_type="audio", max_bytes=max_bytes)
                chunks: List[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    validate_inbound_media_size(total, media_type="audio", max_bytes=max_bytes)
                    chunks.append(chunk)
                return b"".join(chunks)
    raise ValueError("too many voice attachment redirects")


def _aesgcm_decrypt(data: bytes, iv: bytes, key: bytes) -> bytes:
    """XEP-0454: ciphertext carries the 16-byte GCM tag; decrypt() verifies it."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore[import-not-found]
    return AESGCM(key).decrypt(iv, data, None)


async def _cache_audio_bytes(data: bytes, ext: str) -> str:
    """Write decrypted voice-note bytes into Hermes core's audio cache."""
    from gateway.platforms.base import cache_audio_from_bytes_async  # pyright: ignore[reportMissingImports]
    return await cache_audio_from_bytes_async(data, ext)


# ---------------------------------------------------------------------
# Registration helpers
# ---------------------------------------------------------------------

_CALL_GREETING = "Hi, I'm here. What can I do for you?"
# Adapters with live calls, by Hermes home. One gateway process may serve several profiles
# (multiplex_profiles), each with its own XMPP account; the agent tool finds its own profile's.
_CALL_ADAPTERS: Dict[str, "XmppAdapter"] = {}


def _home_key() -> str:
    return os.path.realpath(_hermes_home())


def _live_adapter() -> Optional["XmppAdapter"]:
    return _CALL_ADAPTERS.get(_home_key())

_CALL_TOOL_SCHEMA = {
    "name": "xmpp_call",
    "description": (
        "Place a voice call to the user over XMPP and speak an opening (a line or a whole briefing) "
        "when they answer; unless hang_up_after is set the conversation then continues by voice. "
        "Use only when something warrants interrupting them (an urgent alert, a finished task "
        "they asked to be told about). One call at a time. Returns whether the call connected, "
        "was declined, or went unanswered. The call keeps running after this returns."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "opening": {
                "type": "string",
                "description": (
                    "What you say as soon as they answer. A short greeting, or a whole briefing "
                    "written as plain spoken sentences (no markdown, lists or links)."
                ),
            },
            "uninterruptible": {
                "type": "boolean",
                "description": (
                    "True to deliver the opening in full even if the person starts talking "
                    "(a briefing they asked for). Default false: they may interrupt."
                ),
            },
            "hang_up_after": {
                "type": "boolean",
                "description": "True to end the call when the opening is done instead of taking questions.",
            },
            "context": {
                "type": "string",
                "description": "Background to keep for follow-up questions during the call (never "
                "spoken), such as a reference and the steps to use when the person asks for a change.",
            },
            "attempts": {
                "type": "integer",
                "description": "Only for urgent matters: how many times to ring before falling back to "
                "text (default 1; at most 5). Each ring waits for the pause before the next.",
            },
            "pause_seconds": {
                "type": "number",
                "description": "Seconds between rings when attempts is above 1 (default 60).",
            },
            "topic": {
                "type": "string",
                "description": "Short name of what the call is about (e.g. a cron job name). "
                "It becomes part of the call's session title.",
            },
            "to": {
                "type": "string",
                "description": "Bare JID to call. Must be an allowed user. Optional when there is only one.",
            },
        },
        "required": ["opening"],
    },
}


_MESSAGE_TOOL_SCHEMA = {
    "name": "xmpp_message",
    "description": (
        "Send a text message over XMPP to the person or to a partner agent, for example to hand "
        "the other agent a task. Hermes has no general send_message tool. The recipient must be "
        "an allowed user or a partner agent. Returns whether it was sent."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "Bare JID of the recipient."},
            "text": {"type": "string", "description": "The message. Plain text."},
        },
        "required": ["to", "text"],
    },
}


def _xmpp_message_tool(args: dict, **_kw: Any) -> str:
    """Agent tool: send a chat message through the live adapter (never a second process)."""

    def result(success: bool, message: str) -> str:
        return json.dumps({"success": success, "status": message})

    adapter_ = _live_adapter()
    if adapter_ is None or adapter_._calls_loop is None:
        return result(False, "XMPP is not connected")
    to = adapter_._bare(str((args or {}).get("to") or "").strip()).lower()
    text = str((args or {}).get("text") or "").strip()
    known = {j.lower() for j in adapter_.allowed_users} | set(adapter_._agent_peers)
    if not text:
        return result(False, "text is required")
    if to not in known:
        return result(False, f"cannot message {to or '(nobody)'}; allowed: " + ", ".join(sorted(known)))
    with adapter_._call_tool_lock:
        now = time.monotonic()
        recent = [t for t in adapter_._message_tool_times.get(to, []) if now - t < 600]
        if len(recent) >= adapter_._message_tool_limit:  # a confused agent must not flood a person or a peer
            return result(False, f"too many messages to {to} in the last 10 minutes")
        adapter_._message_tool_times[to] = recent + [now]
    # A reply_to keeps this out of the spoken reply of a call turn (see the call host's capture()).
    future = asyncio.run_coroutine_threadsafe(
        adapter_.send(chat_id=to, content=text, reply_to="xmpp-message-tool"), adapter_._calls_loop
    )
    try:
        sent = future.result(timeout=60)
    except Exception as exc:
        future.cancel()
        return result(False, f"sending failed: {exc}")
    return result(bool(sent.success), "sent" if sent.success else str(sent.error or "not sent"))


def _calls_available() -> bool:
    adapter_ = _live_adapter()
    return adapter_ is not None and adapter_._calls is not None


def _xmpp_connected() -> bool:
    """`xmpp_message` is offered whenever the adapter is connected; it needs no call libraries."""
    adapter_ = _live_adapter()
    return adapter_ is not None and adapter_._calls_loop is not None


def _bounded_int(value: Any, low: int, high: int) -> Optional[int]:
    try:
        return min(high, max(low, int(value)))
    except (TypeError, ValueError, OverflowError):
        return None


def _bounded_float(value: Any, low: float, high: float) -> Optional[float]:
    try:
        value = float(value)
        return min(high, max(low, value)) if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _xmpp_call_tool(args: dict, **_kw: Any) -> str:
    """Agent tool: runs in a worker thread, so hand the call to the adapter's event loop."""

    def result(success: bool, message: str) -> str:
        return json.dumps({"success": success, "status": message})

    adapter_ = _live_adapter()
    if adapter_ is None or adapter_._calls is None or adapter_._calls_loop is None:
        return result(False, "calls are not enabled or XMPP is not connected")
    opening = str((args or {}).get("opening") or "").strip()
    if not opening:
        return result(False, "an opening line is required")
    host = adapter_._calls_host
    allowed = sorted(host.callable_jids() if hasattr(host, "callable_jids") else adapter_.allowed_users)
    target = (
        adapter_._bare(str(args.get("to") or "").strip()).lower()
        if args.get("to") else (allowed[0] if len(allowed) == 1 else "")
    )
    if target and target not in allowed:
        return result(False, f"cannot call {target}; callable people: " + ", ".join(allowed))
    if not target:
        return result(False, "say who to call with `to`; one of: " + ", ".join(allowed))
    try:
        if asyncio.get_running_loop() is adapter_._calls_loop:
            return result(False, "cannot place a call from the XMPP event loop thread")
    except RuntimeError:
        pass  # no loop in this thread: the normal worker-thread case
    opening_limit = adapter_._calls_cfg["opening_max_chars"]
    if len(opening) > opening_limit:
        return result(False, f"the opening is too long ({len(opening)} characters; at most {opening_limit})")
    with adapter_._call_tool_lock:  # a runaway agent must not ring a person all day
        now = time.monotonic()
        recent = [t for t in adapter_._call_tool_times if now - t < 3600]
        call_limit = adapter_._calls_cfg["max_calls_per_hour"]
        if len(recent) >= call_limit:
            return result(False, f"too many calls placed in the last hour (limit {call_limit})")
        adapter_._call_tool_times = recent + [now]
    tries = _bounded_int(args.get("attempts"), 1, 5) or adapter_._calls_cfg["ring_attempts"]
    pause = _bounded_float(args.get("pause_seconds"), 0.0, 3600.0)
    asked_at, request_topic, request_context = adapter_._call_request
    if time.monotonic() - asked_at > 600:  # only a request that is still fresh
        request_topic = request_context = ""
    elif not args.get("topic") or not args.get("context"):
        adapter_._call_request = (0.0, "", "")  # used once: it does not leak into later calls
    future = asyncio.run_coroutine_threadsafe(adapter_._calls.place_call(
            target,
            opening,
            uninterruptible=_truthy(args.get("uninterruptible")),
            hang_up_after=_truthy(args.get("hang_up_after")),
            topic=str(args.get("topic") or request_topic)[:60],
            context=str(args.get("context") or request_context)[:3000],
            attempts=tries,
            pause=pause,
        ),
        adapter_._calls_loop,)
    cfg = adapter_._calls_cfg
    patience = tries * (cfg["ring_timeout"] + (pause if pause is not None else cfg["ring_pause"]))
    try:
        status = future.result(timeout=patience + 2 * cfg["ring_timeout"] + 30)
    except Exception as exc:
        future.cancel()
        return result(False, f"call failed: {exc}")
    return result(status.startswith("connected"), status)


def _calls_settings(extra: Any) -> Dict[str, Any]:
    """`xmpp.calls` options with safe fallbacks; a bad value never stops the adapter."""
    raw = extra.get("calls") if isinstance(extra, dict) else None
    raw = raw if isinstance(raw, dict) else {}

    def number(key: str, default: float, kind: type, minimum: float | None = None) -> Any:
        try:
            value = kind(raw.get(key, default))
            if not math.isfinite(value) or (minimum is not None and value < minimum):
                raise ValueError("not finite or below minimum")
            return value
        except (TypeError, ValueError, OverflowError):
            logger.warning("xmpp: invalid calls.%s %r, using %r", key, raw.get(key), default)
            return kind(default)

    return {
        "enabled": _truthy(raw.get("enabled", _scoped_getenv("XMPP_CALLS_ENABLED", "false"))),
        "max_seconds": _max_seconds(raw.get("max_seconds", 600), number),
        "greeting": (_phrases(raw["greeting"], "greeting", 500) or ()) if "greeting" in raw else (_CALL_GREETING,),
        "filler_after": number("filler_after", 3.0, float, 0),
        "filler": _phrases(raw.get("filler"), "filler", 200),
        "ring_timeout": number("ring_timeout", 60, int, 1),
        "silence_ms": number("silence_ms", 800, int, 1),
        "vad_level": min(3, max(0, number("vad_level", 2, int))),
        "ack_tone": _tone(raw.get("ack_tone", True)),
        "warn_tone": _tone(raw.get("warn_tone", True)),
        "max_calls_per_hour": min(100, max(1, number("max_calls_per_hour", _CALL_TOOL_LIMIT, int))),
        "opening_max_chars": min(10000, max(50, number("opening_max_chars", _OPENING_LIMIT, int))),
        "allowed_users": sorted(
            {j.strip().lower() for j in _csv(raw.get("allowed_users", "")).split(",") if j.strip()}
        ),
        "hangup_grace": max(0.0, number("hangup_grace", 5.0, float)),
        "recap": _truthy(raw.get("recap", True)),
        "idle_timeout": max(0.0, number("idle_timeout", 60.0, float)),
        "log_transcripts": _truthy(raw.get("log_transcripts", False)),
        "busy_wait": max(0.0, number("busy_wait", 90.0, float)),
        "ring_attempts": min(5, max(1, number("ring_attempts", 1, int))),
        "ring_pause": max(0.0, number("ring_pause", 60.0, float)),
        "barge_in": "stop" if str(raw.get("barge_in", "queue")).lower() == "stop" else "queue",
    }


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _phrases(value: Any, key: str, chars: int) -> tuple[str, ...] | None:
    """None: not set. Otherwise up to 20 phrases (a string or a list); empty means none."""
    if value is None:
        return None
    items = value if isinstance(value, (list, tuple)) else [value]
    phrases = tuple(p for item in items if isinstance(item, str) and (p := item.strip()))
    if len(phrases) != len([i for i in items if i is not False]) or len(phrases) > 20:
        logger.warning("xmpp: calls.%s takes up to 20 non-empty strings; ignoring the rest", key)
    return tuple(p[:chars] for p in phrases[:20])


def _max_seconds(value: Any, number: Any) -> int | None:
    """`false` (or off/none/no) lifts the time limit; anything else is the usual number."""
    if value is None:
        return 600  # not a number and not "off": the default
    if value is False or str(value).strip().lower() in ("false", "off", "none", "no"):
        return None
    return number("max_seconds", 600, int, 1) if value is not True else 600


def _tone(value: Any) -> bool | str:
    """True: the built-in beep. False, "" or off: none. Anything else is a sound file path
    (a relative one is looked up in the Hermes home)."""
    if value is None or value is True:
        return True
    text = str(value).strip()
    if text.lower() in ("true", "yes", "on", "1"):
        return True
    if value is False or text.lower() in ("", "false", "no", "off", "none", "0"):
        return False
    path = os.path.expanduser(text)
    return path if os.path.isabs(path) else os.path.join(_hermes_home(), path)


def _csv(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return ",".join(str(v).strip() for v in value if str(v).strip())
    return str(value).strip()


# ---------------------------------------------------------------------
# Credential probing (cron delivery preflight & platform enablement)
# ---------------------------------------------------------------------

_DOTENV_PEEK_CACHE: dict[tuple, dict] = {}


def _hermes_home() -> str:
    """``$HERMES_HOME`` honouring the contextvar home override the multiplexing
    gateway installs around each profile's work, else the process env, else
    ``~/.hermes``. Secondary adapters MUST resolve their home through here —
    raw ``os.environ["HERMES_HOME"]`` always names the LAUNCH profile's home,
    which is how per-profile state (the .env peek, OMEMO storage) silently
    collapsed onto the default profile's files under ``multiplex_profiles``."""
    try:
        from hermes_constants import get_hermes_home  # lazy: optional seam
        return str(get_hermes_home())
    except Exception:
        return os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")


def _dotenv_peek(key: str) -> str:
    """Non-mutating read of ``key`` from the Hermes ``.env``.

    Credentials live in ``$HERMES_HOME/.env``, but some asking processes — notably
    cron's delivery preflight (``cron/scheduler_preflight.py``) — probe plugin
    platforms *before* their own dotenv pass, so ``XMPP_JID``/``XMPP_PASSWORD``
    are not yet in ``os.environ``. This peek fills that gap WITHOUT exporting
    anything: process env always wins, the file is only read, and a missing or
    unparsable file simply yields ``""`` (same verdict as before this existed).
    Home resolves through :func:`_hermes_home` (contextvar override aware), so
    a multiplexed secondary reads ITS OWN profile's ``.env``, never the launch
    profile's.
    """
    home = _hermes_home()
    path = os.path.join(home, ".env")
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return ""
    cache_key = (path, mtime)
    cached = _DOTENV_PEEK_CACHE.get(cache_key)
    if cached is not None:
        return cached.get(key, "")
    values: dict[str, str] = {}
    try:
        from dotenv import dotenv_values

        for k, v in (dotenv_values(path) or {}).items():
            if isinstance(k, str) and isinstance(v, str):
                values[k] = v
    except Exception:
        # dotenv unavailable (standalone tests, foreign harness): minimal manual
        # parse — enough for the plain KEY=value lines Hermes itself writes.
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    if k.startswith("export "):
                        k = k[len("export "):].strip()
                    v = v.strip()
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                        v = v[1:-1]
                    if k:
                        values[k] = v
        except Exception:
            return ""
    _DOTENV_PEEK_CACHE.clear()
    _DOTENV_PEEK_CACHE[cache_key] = values
    return values.get(key, "")


def _multiplex_active() -> bool:
    """Whether this process is a profile multiplexer (one process, many
    profiles). When True, ``os.environ`` belongs to the LAUNCH profile — every
    other profile must resolve credentials from its own scope/``.env`` instead
    of borrowing the launch profile's env (which once made every secondary XMPP
    adapter bind the default profile's JID)."""
    try:
        from agent.secret_scope import is_multiplex_active
        return bool(is_multiplex_active())
    except Exception:
        return False


def _scoped_getenv(name: str, default: Any = "", *, peek: bool = False) -> Any:
    """``os.getenv`` honouring the active profile's secret scope.

    Resolution order: ``agent.secret_scope.get_secret`` (the per-profile
    contextvar mapping the multiplexing gateway installs around each profile's
    adapters and turns — falls back to ``os.environ`` on single-profile hosts),
    then, only when multiplexing is INACTIVE, the process env and — for
    ``peek=True`` names only — the cron preflight ``.env`` peek. Under
    multiplexing an unscoped read fails closed to ``default`` rather than
    borrowing the launch profile's value: a wrong credential is worse than none
    (three adapters, one JID, each rejecting its own owner's allowlist — the
    exact bug this fixes). Without the secret-scope machinery (foreign harness,
    plain plugin install), legacy single-profile resolution applies.

    ``peek`` is for the credential pair only: the cron preflight runs before
    its dotenv pass and must still see XMPP_JID/XMPP_PASSWORD in
    ``$HERMES_HOME/.env`` to judge whether an XMPP gateway is configured.
    Tuning vars must NOT peek — a developer-machine ``.env`` would otherwise
    override the class defaults of every bare test config (the
    connect-timeout regression)."""
    try:
        from agent.secret_scope import get_secret
    except Exception:
        # No secret-scope machinery at all: classic single-profile resolution.
        return os.getenv(name, "") or (_dotenv_peek(name) if peek else "") or default
    try:
        value = get_secret(name, None)
    except Exception:
        # UnscopedSecretError (multiplexed, no scope installed): fail closed —
        # never fall through to os.environ, that is the cross-profile leak.
        return default
    if value is not None:
        return str(value)
    if _multiplex_active():
        return default
    # Single-profile host / cron preflight: legacy resolution preserved.
    return os.getenv(name, "") or (_dotenv_peek(name) if peek else "") or default


def _xmpp_credentials(config: PlatformConfig) -> tuple[str, str]:
    """(jid, password) from config.extra, the profile-scoped env, or the .env
    peek — in that priority order. Read-only; never exports into
    ``os.environ``."""
    extra = getattr(config, "extra", {}) or {}
    jid = (extra.get("jid") or _scoped_getenv("XMPP_JID", peek=True) or "").strip()
    password = (
        extra.get("password") or _scoped_getenv("XMPP_PASSWORD", peek=True) or ""
    ).strip()
    return jid, password


def _env_enablement() -> Optional[dict[str, Any]]:
    jid, password = _xmpp_credentials(PlatformConfig())
    if not (jid and password):
        return None
    data: dict[str, Any] = {"jid": jid, "password": password}
    for env, key in (
        ("XMPP_HOST", "host"),
        ("XMPP_PORT", "port"),
        ("XMPP_DIRECT_TLS", "direct_tls"),
        ("XMPP_CONNECT_TIMEOUT_SECS", "connect_timeout_secs"),
        ("XMPP_MUC_ROOMS", "muc_rooms"),
        ("XMPP_MUC_NICK", "muc_nick"),
        ("XMPP_ALLOWED_USERS", "allowed_users"),
        ("XMPP_MUC_ALLOWED_USERS", "muc_allowed_users"),
        ("XMPP_ALLOW_ALL_USERS", "allow_all_users"),
    ):
        value = _scoped_getenv(env).strip()
        if value:
            data[key] = value
    return data


def _apply_yaml_config(yaml_cfg: dict, xmpp_cfg: dict) -> Optional[dict[str, Any]]:
    raw = dict(xmpp_cfg or {})
    extra = dict(raw.get("extra") or {})
    for key in (
        "jid", "password", "host", "port", "direct_tls", "connect_timeout_secs",
        "muc_rooms", "muc_nick", "allowed_users", "muc_allowed_users", "allow_all_users", "max_message_length",
        "voice_transcription", "voice_trusted_hosts", "calls", "agent_peers", "agent_peer_limit", "message_tool_limit",
    ):
        if key in raw and key not in extra:
            extra[key] = raw[key]

    # Merge omemo sub-config if present
    omemo_extra = {}
    if "omemo" in raw:
        omemo_raw = raw["omemo"]
        if isinstance(omemo_raw, dict):
            omemo_extra["omemo"] = omemo_raw
    if "omemo_enabled" in raw:
        omemo_extra.setdefault("omemo", {})["enabled"] = raw["omemo_enabled"]
    if "omemo_storage_path" in raw:
        omemo_extra.setdefault("omemo", {})["storage_path"] = raw["omemo_storage_path"]
    if omemo_extra:
        extra.update(omemo_extra)

    env_map = {
        "jid": "XMPP_JID",
        "password": "XMPP_PASSWORD",
        "host": "XMPP_HOST",
        "port": "XMPP_PORT",
        "direct_tls": "XMPP_DIRECT_TLS",
        "connect_timeout_secs": "XMPP_CONNECT_TIMEOUT_SECS",
        "muc_rooms": "XMPP_MUC_ROOMS",
        "muc_nick": "XMPP_MUC_NICK",
        "allowed_users": "XMPP_ALLOWED_USERS",
        "muc_allowed_users": "XMPP_MUC_ALLOWED_USERS",
        "allow_all_users": "XMPP_ALLOW_ALL_USERS",
        "home_channel": "XMPP_HOME_CHANNEL",
    }
    # Boolean flags: set env from yaml/config if the env isn't already set.
    if not os.getenv("XMPP_DISABLE_CLARIFY_FORMS"):
        raw_disable = raw.get("disable_clarify_forms", extra.get("disable_clarify_forms"))
        if raw_disable is not None:
            os.environ["XMPP_DISABLE_CLARIFY_FORMS"] = str(raw_disable)
    # Under multiplexing the process env is SHARED by every profile's adapter:
    # exporting one profile's XMPP_* value here would stomp (or be stomped by)
    # another profile's — the launch profile's block must never set another
    # tenant's env. Values stay in `extra`, which every read resolves first.
    _multiplex = _multiplex_active()
    for key, env in env_map.items():
        value = raw.get(key, extra.get(key))
        if key == "home_channel" and isinstance(value, dict):
            value = value.get("chat_id")
        if value is None or os.getenv(env):
            continue
        if _multiplex:
            continue
        os.environ[env] = _csv(value)
    return extra or None


def validate_config(config: PlatformConfig) -> bool:
    jid, password = _xmpp_credentials(config)
    return bool(jid and password)


def is_connected(config: PlatformConfig) -> bool:
    return validate_config(config)


def _build_adapter(config: PlatformConfig) -> XmppAdapter:
    return XmppAdapter(config)


def register(ctx) -> None:
    try:
        ctx.register_tool(
            name="xmpp_call", toolset="xmpp_calls", schema=_CALL_TOOL_SCHEMA,
            handler=_xmpp_call_tool, check_fn=_calls_available,
            description=_CALL_TOOL_SCHEMA["description"], emoji="📞",
        )
        ctx.register_tool(
            name="xmpp_message", toolset="xmpp_calls", schema=_MESSAGE_TOOL_SCHEMA,
            handler=_xmpp_message_tool, check_fn=_xmpp_connected,
            description=_MESSAGE_TOOL_SCHEMA["description"], emoji="💬",
        )
    except Exception:  # older Hermes without plugin tools: chat still works
        logger.debug("xmpp: could not register the xmpp tools", exc_info=True)
    ctx.register_platform(
        name="xmpp",
        label="XMPP/Jabber",
        adapter_factory=_build_adapter,
        check_fn=check_xmpp_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["XMPP_JID", "XMPP_PASSWORD"],
        install_hint="Install dependencies with: uv pip install slixmpp==1.15.0 aiohttp==3.14.1",
        env_enablement_fn=_env_enablement,
        apply_yaml_config_fn=_apply_yaml_config,
        cron_deliver_env_var="XMPP_HOME_CHANNEL",
        standalone_sender_fn=send_xmpp_message,
        allowed_users_env="XMPP_ALLOWED_USERS",
        allow_all_env="XMPP_ALLOW_ALL_USERS",
        max_message_length=10000,
        emoji="💬",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=(
            "You are communicating over XMPP/Jabber. Use plain text by default. "
            "XMPP clients vary in markdown rendering, so avoid heavy markdown tables. "
            "Keep replies reasonably concise unless the user asks for detail."
        ),
    )
