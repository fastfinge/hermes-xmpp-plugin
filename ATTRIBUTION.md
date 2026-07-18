# Attribution

This repository exists to package XMPP/Jabber support as a third-party Hermes
Agent platform plugin while upstream PRs are waiting.

Primary implementation source:

- Eric Lars Lee (`ericlarslee`) — author of NousResearch/hermes-agent PR #17469,
  "feat(gateway): add XMPP/Jabber platform adapter". Most of the adapter design
  and implementation here is derived from that PR: slixmpp adapter, MUC handling,
  STARTTLS posture, XEP-0363 uploads, standalone send helper, tests, and docs.
  Later revisions of #17469 (through 2026-07-13) also fixed a set of connection-
  reliability issues — a bounded wait for session establishment, a scoped
  per-account connection lock, `failed_all_auth` handling, direct-TLS (XEP-0368)
  support, and corrected `send_voice`/`send_document`/`send_video` parameter
  names — that are ported into this plugin as well.

Related prior work reviewed and credited:

- Mibay (`Mibayy`) — author of NousResearch/hermes-agent PR #3105,
  "feat(xmpp): add XMPP platform adapter with optional OMEMO support". That PR
  explored an earlier adapter shape and optional OMEMO availability detection.
  The OMEMO encryption/decryption wiring, BTBV trust handling, and XEP-0384
  plugin registration in this plugin are derived from that exploratory work.
- alien2003 — author of the closed competing XMPP platform-plugin PR #30647,
  who explicitly closed their PR in favor of #17469 and offered XHTML-IM/E2E test
  ideas for future work.
- Nous Research / Hermes Agent contributors — MIT-licensed gateway framework,
  platform adapter interfaces, and plugin registry API this plugin targets.

Samuel Proulx / fastfinge packaged this as an optional third-party plugin to make
XMPP usable without waiting on upstream merge/rebase process. This is not an
attempt to take credit for the original adapter work.
