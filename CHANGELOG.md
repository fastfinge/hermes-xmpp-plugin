# Changelog

All notable changes to this project will be documented in this file.

## [0.4.0] — Connection reliability, OMEMO, and security fixes

### Added

- **Automatic long-message splitting**: outbound messages longer than
  `MAX_MESSAGE_LENGTH` (default 10000 chars) are split into multiple stanzas
  on word/code-fence boundaries instead of being truncated. Applies to
  plaintext, OMEMO-encrypted, and standalone (cron / `send_message`) send
  paths. Configurable via the `max_message_length` config key or the
  `XMPP_MAX_MESSAGE_LENGTH` env var. `splits_long_messages = True` is also
  declared so a host gateway that checks it skips pre-truncating cron output
  before `send()` ever sees it.
- **Direct TLS (XEP-0368)**: auto-enabled when `XMPP_PORT=5223`, overridable
  either way via `XMPP_DIRECT_TLS` / the `direct_tls` config key. STARTTLS
  remains the port-5222 default; plaintext is refused in every configuration.
- **Scoped per-account connection lock**: `connect()` takes a lock on the
  normalized bare JID so two gateways can't both log into the same XMPP
  account and double-handle every inbound stanza. The one-shot
  cron/`send_message` sender skips it deliberately — it attaches as a
  short-lived second resource, which XMPP permits.
- **Presence-subscription auto-approval**: inbound subscription requests
  from allow-listed peers are approved and subscribed back automatically, so
  OMEMO device-list PEP updates reach the peer's client (this clears a stale
  "no OMEMO for this contact" cache on their end).
- **Configurable connect timeout** (`XMPP_CONNECT_TIMEOUT_SECS` /
  `connect_timeout_secs`, default 180s): a background watchdog gives a
  connection this long to establish before tearing it down and retrying —
  see below for why 180s and why it doesn't block the gateway.

### Fixed

- **`XMPP_OMEMO_ENABLED=False` is honored** (issue #5). `omemo_enabled` /
  `XMPP_OMEMO_ENABLED` — via env var, `extra["omemo_enabled"]`, or the nested
  `extra["omemo"]["enabled"]` config key — is now parsed with a `_truthy()`
  helper everywhere instead of `bool(str)`. Plain `bool("False")` is `True`
  for any non-empty string, so `XMPP_OMEMO_ENABLED=False` was silently
  ignored and OMEMO stayed on.
- **`connect()` no longer blocks on session establishment**, and the
  connect-watchdog default is now 180s (was 90s, before that 20-60s).
  Blocking here stalled every other platform's startup behind XMPP, and
  worse, any wait inside `connect()` could race the gateway's own
  per-platform connect timeout — if that outer timeout won, it cancelled
  `connect()` at an await point that only caught `TimeoutError`, skipping
  cleanup and leaking the client/task/lock. `connect()` now kicks off the
  handshake and returns immediately; a background `_connect_watchdog()`
  independently waits up to the configured timeout and, if the session
  never establishes, tears the connection down and notifies the gateway's
  reconnect watcher (the same escalation path used for a live connection
  dropping, #28919). The 180s default reflects a real, measured case: a
  home-network connection consistently took ~135s end-to-end (confirmed
  reproducible, zero relation to server load) — the classic signature of a
  **blackholed IPv6 route**: DNS returns both an A and AAAA record, the IPv6
  attempt is silently dropped somewhere in the path, and the OS exhausts its
  default TCP SYN retry budget (~127-130s on Linux) before falling back to
  IPv4, which then succeeds instantly. If XMPP still times out after
  raising `XMPP_CONNECT_TIMEOUT_SECS`, check for exactly this — a
  disproportionately long, fixed delay before an otherwise-healthy
  connection succeeds is the tell.
- **Readiness now fires on slixmpp's `session_bind` event** rather than
  `session_start`, which additionally depends on a legacy, *optional* RFC
  3921 IQ-based session-establishment round-trip that some real servers
  advertise but never reliably answer — leaving `session_start` (and thus
  `send_presence()`/roster/MUC-join, and the bot's visible online status)
  stuck forever despite the connection being otherwise fully usable.
  `session_bind` is what `slixmpp_omemo` and dozens of built-in slixmpp
  plugins key their own post-connect setup off, for the same reason.
  `_session_ready.set()` is the unconditional first statement in that
  handler — before `send_presence()`, which is now wrapped in its own
  try/except — so nothing in the following best-effort housekeeping
  (`get_roster()`, MUC joins, presence subscribes, ad-hoc commands) can
  block or suppress the readiness signal. `omemo_initialized` is wired as a
  second, independent, idempotent trigger for the same readiness logic as
  extra insurance, since it's driven by a different part of slixmpp's
  plugin lifecycle.
- `connect()` no longer silently drops `XMPP_HOST`/`XMPP_PORT` on a fallback
  path. The old code called `client.connect(address=(host, port))`, which
  isn't a valid slixmpp kwarg — it always raised `TypeError` and fell
  through to a bare `client.connect()`, sending the bot to SRV/JID-domain
  lookup instead of the configured server.
- Auth failure now reacts to `failed_all_auth` (fired once, after every SASL
  mechanism the server offered has been exhausted) instead of `failed_auth`
  (fired per rejected mechanism), which marked the adapter dead even when a
  later mechanism succeeded, silently poisoning a working connection.
- An unexpected mid-session disconnect (server restart, network blip) now
  escalates to a retryable fatal error and notifies the host gateway, so its
  reconnect watcher actually retries, instead of just marking the adapter
  disconnected with nothing driving recovery.
- `send_voice`/`send_document`/`send_video` now accept the keyword argument
  names (`audio_path`/`file_path`/`video_path`) the host gateway actually
  calls them with (e.g. `cron/scheduler.py`) — the previous `path` parameter
  name raised `TypeError` on every keyword call, so cron/scheduled media
  attachments never sent.
- `xep_0380` (Explicit Message Encryption hint) is now registered as a
  slixmpp plugin. The EME-hint code in `_send_encrypted_one`/`edit_message`
  already checked for it, but it was never added to the plugin registration
  list, so the hint silently never fired.
- Fixed `enable_starttls`/`enable_direct_tls`/`enable_plaintext` being the
  actual TLS-posture attributes current slixmpp reads — the previous
  `use_starttls`/`force_starttls` names don't exist on the pinned slixmpp
  version, so that code was a no-op (harmless only because slixmpp's own
  default already refuses plaintext).
- `send_xmpp_message()`'s connect-failure path called `fatal_error_message()`
  as a method; it's a property on the host's `BasePlatformAdapter`, so this
  raised `TypeError` instead of surfacing the real error message.
- Bumped the `aiohttp` pin to 3.14.1 (CVE-2026-34993 `CookieJar.load`
  deserialization; CVE-2026-54273/54274/54280 DoS via unbounded pipelining,
  oversized websocket frames, and unclosed payloads on mid-write disconnect).

### Notes

- `disconnect()` deliberately does **not** force-cancel a still-in-flight
  connection attempt (slixmpp's own `disconnect()` only does that once a
  stream is already established). An orphaned handshake finishing late on
  an already-torn-down adapter is possible but harmless (`self.client is
  None` guards make it a no-op) and now rare given the 180s watchdog
  window. A version that called `cancel_connection_attempt()` explicitly
  was tried and reverted — it correlated with every subsequent connection
  attempt in the process going silent, consistent with the known hazard of
  cancelling a task mid-flight through slixmpp's aiodns/pycares (c-ares)
  DNS resolution.

## [0.3.0] — First-class XMPP

### Added

- **XEP-0444 message reactions**: Lifecycle hooks send 👀/✅/❌ reactions when
  Hermes starts/finishes processing. Visible in XEP-0444-capable clients.
- **XEP-0461 threaded replies**: Inbound reply extraction and outbound reply
  sending preserve conversation threads.
- **XEP-0394 message markup**: Bold, code, and block-code spans are generated
  from Markdown-like syntax in Hermes responses.
- **XEP-0004 data forms for clarify**: Multi-choice clarify prompts are sent as
  data forms instead of plain text. Form responses are parsed and forwarded.
- **XEP-0050 ad-hoc commands**: Basic command-list registration on the bot JID
  for client-side command discovery.
- **XEP-0447 voice messages**: Voice audio is sent as Stateless File Sharing
  references after HTTP upload. Clients show file metadata before download.
- **XEP-0446 file metadata**: File sharing now includes metadata (name, size,
  media type) when supported.
- **Backward-compatible `send()` API**: Legacy parameters (`image_paths`,
  `voice_path`, `document_path`, `reply_to`, etc.) are still accepted.

### Fixed

- OMEMO class definitions no longer break import when `omemo` is not installed.
- Cross-test contamination resolved via `sys.modules` cache clearing.

### Deprecated

- Passing media through the generic `send()` method is deprecated. Use the
  dedicated `send_image()`, `send_voice()`, `send_document()` methods.

## [0.2.0] — Standalone plugin packaging

### Added

- Initial standalone packaging from upstream Hermes Agent PR code.
- 1:1 chat and MUC support.
- XEP-0085 typing indicators.
- XEP-0363 HTTP file upload.
- OMEMO end-to-end encryption (optional).
- `cron` and `send_message` standalone sender hook.
- Platform plugin registration.

## [0.1.0] — Unreleased

- Prototype explorations by Eric Lars Lee and Mibay.
