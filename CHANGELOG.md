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
