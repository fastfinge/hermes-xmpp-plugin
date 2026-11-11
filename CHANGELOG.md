# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

## [0.5.0] — Audio calls and agent hand-off

### Added

- `xmpp.calls.filler`: the "One moment." filler is now a string or list (six built-in phrases,
  picked at random, never twice in a row; `""` turns it off). Each phrase is synthesized once and
  kept in `<HERMES_HOME>/cache/xmpp-calls/`. Calls need Python 3.13 or newer.
- `xmpp.calls.greeting` takes a string or a list too (one at random, never twice in a row) and is
  cached like a filler. `ack_tone` and the new `warn_tone` (the two falling beeps) accept `false`,
  `""` or a sound file. `max_seconds: false` lifts the time limit. New limits with unchanged
  defaults: `xmpp.calls.max_calls_per_hour` (6), `xmpp.calls.opening_max_chars` (1500),
  `xmpp.agent_peer_limit` (4 per 10 minutes) and `xmpp.message_tool_limit` (10 per recipient per
  10 minutes).
- More gateway status lines are recognised and not exchanged with a partner agent (paused, first-time
  tips, compaction, restart, retry and fallback warnings).
- `xmpp.calls.allowed_users` and `xmpp.calls.hangup_grace` (warning beeps and a speaking
  window before a planned hang-up).

- **Audio calls (Jingle RTP, optional)**: `xmpp.calls.enabled` lets allowed JIDs call the bot
  (XEP-0166/0167/0176/0320/0353 over aiortc). Speech goes through the configured `stt` and `tts`
  providers, replies are normal agent turns, barge-in and a spoken hang-up are supported. Code
  lives in `calls/`; its packages (`requirements-calls.txt`, about 230 MB) get their own Hermes environment (`pm.ensure_environment`) on the first start with calls on, so they are optional and survive `hermes update`; a `vendor/` folder is the fallback. Off by default. `xmpp_message` works with calls off. The `xmpp_call` agent tool places an outgoing call (also usable from cron jobs).
- **Inbound voice notes → Hermes STT**: a message that is exactly one audio
  attachment and no text (XEP-0066 OOB, XEP-0447 SFS with an `audio/*`
  media type, or an OMEMO `aesgcm://` link per XEP-0454) is downloaded into
  Hermes' audio cache and dispatched as a `MessageType.VOICE` event, so the
  gateway transcribes it with the operator's configured `stt` provider
  (local or remote) and uses the transcript as the prompt. Fetching reuses
  Hermes core's SSRF-safe client and inbound media size cap; authorization
  runs before any download. Captioned/multi-file/non-audio messages are
  unchanged. Toggle with `voice_transcription` / `XMPP_VOICE_TRANSCRIPTION`
  (default on). If the download fails the message falls back to the
  previous plain-text behaviour.
- **`voice_trusted_hosts`** (`XMPP_VOICE_TRUSTED_HOSTS`): upload hosts that
  may resolve to LAN/tailnet addresses when fetching voice notes, without
  turning on Hermes' global `allow_private_urls`. Defaults to the bot's own
  XMPP domain and its subdomains; https-only, no redirects, metadata floor
  and size cap kept. Other hosts still get Hermes' full SSRF checks.

### Fixed

- Preserve queued caller requests across goodbye replies, recoverable agent failures and steering
  races. Coalesce concurrent hang-up grace checks; prevent late STT/TTS after terminal cleanup.
- Apply sentence-level playback backpressure. Filler synthesis failure no longer discards a
  successful agent result; zero-length preroll does not retain silence indefinitely.
- Register pending sessions before TURN discovery, retain early candidates, and honor early
  termination. Disconnect prevents late outgoing answers from creating calls. Remote termination
  wakes setup waits immediately instead of becoming a false timeout.
- Reject unsupported multi-content audio/video offers and unsupported Jingle actions explicitly.
  Handle authenticated received JMI carbons, retractions, duplicate and stale proposals; use the
  standard `cancel` termination reason.
- Default invalid/nonfinite numeric settings safely even when calls are off; normalize tool
  targets and process partner filters and request metadata after OMEMO decryption.

- Preserve outgoing voice SFS metadata, file hash, and upload URL by appending
  the populated Slixmpp stanza. Send matching URL/body/OOB fallback for client
  compatibility, with a whole-body XEP-0428 marker. Real serialization tests
  cover DM and groupchat. Conversations playback still requires live acceptance.
- Keep voice settings profile-scoped, accept list-valued sender allowlists, and
  carry verified MUC-only grants through Hermes core without granting DM access.
- Withhold encrypted reply keys even when fallback stripping removes the URL
  scheme. Public redirects remain HTTPS and revalidate address policy without
  buffering redirect bodies or blocking the gateway event loop.

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

### Fixed

- **Clarify form body includes numbered fallback** (issue #8): When XEP-0004
  data forms are sent, the message body now contains a numbered choice list
  (identical to the text fallback path) instead of a bare question string.
  Clients that don't render `jabber:x:data` forms in `<message>` stanzas
  (Conversations, ConverseJS, older Gajim) now display actionable choices.
- **Inbound form submission handling** (issue #8): Submitted XEP-0004 forms
  are now parsed in `_on_message` — the hidden `clarify_id` and `answer`
  field are extracted and the clarify is resolved through the gateway.
  Previously, form submissions were silently dropped because they typically
  have an empty `<body>` and the early-return guard discarded them.
- **Config toggle to disable data forms** (issue #8): Set
  `XMPP_DISABLE_CLARIFY_FORMS=true` (env) or `disable_clarify_forms: true`
  (config.yaml) to force the numbered text fallback path even when the
  `xep_0004` plugin is registered.
- **`disconnect()` now shuts down dependency-owned background tasks**
  (issue #12): Slixmpp's `run_filters` out-filter pump and the OMEMO
  session manager's signed-pre-key rotation task are started by
  `XMLStream.connect()` / `SessionManager.initialize()` and stopped by
  nothing on the old teardown path — every reconnect cycle leaked one
  client and one session manager apiece in long-running gateways (207 of
  each and 361k queued stanzas observed in one report). Teardown now
  cancels and awaits both tasks with a bounded `_TEARDOWN_TIMEOUT_SECS`
  (5s default), invokes `SessionManager.shutdown()`, nulls the session
  manager cached under the `xep_0384` plugin so it isn't recreated from a
  stale cache, and drains `XMLStream.__queued_stanzas` and the slow
  out-filter tasks owned by the old client. Covers timeout, partially
  initialized, and already-torn-down paths. Late `_session_bind`
  reflections belonging to an already-torn-down identifier are now
  ignored. The documented refusal to blanket-cancel an in-flight
  connection attempt (see Notes) is unchanged.

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
