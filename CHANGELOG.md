# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added

- **Automatic long-message splitting**: Outbound messages longer than
  `MAX_MESSAGE_LENGTH` (default 10000 chars) are split into multiple stanzas on
  word/code-fence boundaries instead of being truncated. Applies to plaintext,
  OMEMO-encrypted, and standalone (cron / `send_message`) send paths. The limit
  is configurable via the `max_message_length` config key or the
  `XMPP_MAX_MESSAGE_LENGTH` env var. The adapter now exposes `MAX_MESSAGE_LENGTH`
  so the host's streaming consumer chunks against the same value.
- **Direct TLS (XEP-0368)**: auto-enabled when `XMPP_PORT=5223`, overridable
  either way via `XMPP_DIRECT_TLS` / the `direct_tls` config key. STARTTLS
  remains the port-5222 default; plaintext is refused in every configuration.
- **Scoped per-account connection lock**: `connect()` now takes a lock on the
  normalized bare JID (matching the pattern used elsewhere in Hermes) so two
  gateways can't both log into the same XMPP account and double-handle every
  inbound stanza. The one-shot cron/`send_message` sender deliberately skips
  it — it attaches as a short-lived second resource, which XMPP permits.
- **Presence-subscription auto-approval**: inbound subscription requests from
  allow-listed peers are approved and subscribed back automatically, so OMEMO
  device-list PEP updates reach the peer's client (this is what clears a
  stale "no OMEMO for this contact" cache).
- `splits_long_messages = True` is now declared on the adapter so a host
  gateway that checks it can skip pre-truncating cron/delivery output before
  `send()` ever sees it.
- **Background connect watchdog**: `connect()` kicks off the handshake and
  reports success immediately (it does not block on session establishment —
  the gateway connects platforms one at a time at startup, and blocking here
  would stall every other platform behind a slow XMPP handshake). A
  background `_connect_watchdog()` task independently waits up to
  `_CONNECT_TIMEOUT_SECS` (default 90s; tunable via `XMPP_CONNECT_TIMEOUT_SECS`
  / the `connect_timeout_secs` config key) and, if the session never
  establishes, tears the connection down and notifies the gateway so its
  reconnect watcher retries — the same escalation path used for a live
  connection dropping (#28919).

### Fixed

- **`XMPP_OMEMO_ENABLED=False` is honored** (issue #5). `omemo_enabled` /
  `XMPP_OMEMO_ENABLED` — whether set via env var, `extra["omemo_enabled"]`, or
  the nested `extra["omemo"]["enabled"]` config key — is now parsed with a
  `_truthy()` helper everywhere instead of `bool(str)`. Plain `bool("False")`
  is `True` for any non-empty string, so setting `XMPP_OMEMO_ENABLED=False` in
  `.env` was silently ignored and OMEMO stayed on.
- **Readiness (`_session_ready`, `send_presence()`, roster/MUC/subscribe
  setup) now fires on slixmpp's `session_bind` event instead of
  `session_start`.** `session_start` additionally depends on the legacy,
  *optional* RFC 3921 IQ-based session-establishment round-trip
  (`slixmpp/features/feature_session`) completing. A real server was
  observed advertising that feature but never reliably responding to the
  IQ — so `session_start` never fired at all, even though authentication and
  resource binding had fully succeeded (confirmed by slixmpp's own "JID
  set to" log line) and OMEMO had already initialized (it keys off the
  separate, always-reliable `session_bind` event, which is also what dozens
  of built-in slixmpp plugins use for their own post-connect setup).
  Concretely, this meant `send_presence()` never ran, so the bot never
  appeared online to contacts, and the watchdog eventually timed out and
  cycled a connection that was otherwise perfectly usable.
- Previously, an earlier revision of this fix made `connect()` block
  (bounded) on session establishment before reporting success — a real
  deploy showed this was the wrong design entirely, for two reasons: (1) it
  stalled every other platform's startup behind a slow-but-working XMPP
  handshake, and (2) any timeout value here could race the gateway's own
  per-platform connect timeout (`gateway/run.py` wraps `connect()` in its own
  `asyncio.wait_for`) — if that outer timeout won, it injected
  `CancelledError` at the await point inside `connect()`, which only caught
  `asyncio.TimeoutError` there, skipping cleanup entirely and leaking the
  client/task/lock while the handshake kept running unsupervised in the
  background. Replaced with the background-watchdog design above, which
  can't race the outer timeout and never blocks the gateway's startup.
- `connect()` no longer silently drops `XMPP_HOST`/`XMPP_PORT` on a fallback
  path. The old code called `client.connect(address=(host, port))`, which
  isn't a valid slixmpp kwarg — it always raised `TypeError` and fell through
  to a bare `client.connect()`, sending the bot to SRV/JID-domain lookup
  instead of the configured server.
- Auth failure now reacts to `failed_all_auth` (fired once, after every SASL
  mechanism the server offered has been exhausted) instead of `failed_auth`
  (fired per rejected mechanism). Reacting to `failed_auth` marked the
  adapter dead even when a later mechanism succeeded, silently poisoning a
  working connection.
- An unexpected mid-session disconnect (server restart, network blip) now
  escalates to a retryable fatal error and notifies the host gateway, so its
  reconnect watcher actually retries. Previously the adapter just marked
  itself disconnected with nothing driving recovery — a silently dead bridge
  in an otherwise-healthy gateway.
- `send_voice`/`send_document`/`send_video` now accept the same keyword
  argument names (`audio_path`/`file_path`/`video_path`) the host gateway
  calls them with (e.g. `cron/scheduler.py`). The previous `path` parameter
  name raised `TypeError` on every keyword call, so cron/scheduled media
  attachments never sent.
- `xep_0380` (Explicit Message Encryption hint) is now registered as a
  slixmpp plugin. The EME-hint code in `_send_encrypted_one`/`edit_message`
  already checked for it, but it was never added to the plugin registration
  list, so the hint silently never fired.
- Fixed `enable_starttls`/`enable_direct_tls`/`enable_plaintext` being the
  actual TLS-posture attributes current slixmpp reads. The previous
  `use_starttls`/`force_starttls` names don't exist on the pinned slixmpp
  version, so that code was a no-op (harmless only because slixmpp's own
  default already refuses plaintext).
- `send_xmpp_message()`'s connect-failure path called `fatal_error_message()`
  as a method; it's a property on the host's `BasePlatformAdapter`, so this
  raised `TypeError` instead of surfacing the real error message.
- Bumped the `aiohttp` pin to 3.14.1 (CVE-2026-34993 `CookieJar.load`
  deserialization; CVE-2026-54273/54274/54280 DoS via unbounded pipelining,
  oversized websocket frames, and unclosed payloads on mid-write disconnect).

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
