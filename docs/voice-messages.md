# Voice messages

## Outgoing attachments

Hermes generates audio through its configured TTS provider. The plugin uploads
the file with XEP-0363 and sends XEP-0447 Stateless File Sharing (SFS) metadata.
Append the populated SFS stanza to the message; assigning it through
`msg["sfs"]` does not copy its XML in Slixmpp 1.15.0.

For client compatibility, the upload URL is also sent as the body and an
XEP-0066 out-of-band URL. An XEP-0428 marker identifies the entire body as SFS
fallback, following [XEP-0447's compatibility example](https://xmpp.org/extensions/xep-0447.html#usecases).
Metadata, file hashes, and the download URL remain in SFS. This does not add
outbound media encryption; upload links are visible to parties that can read
the plaintext stanza or its archive.

## Incoming attachments

A voice note is exactly one audio attachment without a caption. An empty body
or a body containing only that attachment's URL is accepted. The plugin reads
XEP-0066 OOB, XEP-0447 SFS, and decrypted XEP-0454 `aesgcm://` links, caches the
audio, and emits a Hermes `VOICE` event. Hermes core chooses the configured STT
provider; no local engine, HTTP endpoint, operating system, or scheduler is
hardcoded in the plugin.

Captioned, multiple, and non-audio attachments keep the ordinary text path.
Authorization runs before decryption, form handling, downloads, or dispatch.
See [MUC sender authorization](muc-authorization.md) for room permissions.
Encrypted links contain keys: failed or disabled encrypted attachments and
key-bearing reply text are not forwarded to the agent as text or logged by
the attachment helper. Failed ordinary HTTPS downloads retain URL-text fallback.

## Private upload services

Downloads use Hermes' inbound-media size limit and URL-safety transport. The
bot's JID domain and its subdomains are trusted by default to support private
upload services. Prefer an exact hostname, or disable private-host exceptions:

```yaml
xmpp:
  voice_trusted_hosts: ["upload.example.org"]
  # Use "none" to apply the normal SSRF policy to every host.
```

`XMPP_VOICE_TRUSTED_HOSTS` accepts comma-separated hostnames. Explicit lists
replace the default; `*.example.org` matches subdomains, not the parent domain.
These settings do not change Hermes' global URL policy. Trusted downloads still
require HTTPS and valid TLS, reject redirects and environment proxies, enforce
the cloud-metadata address floor, and connect only to validated DNS answers.
Untrusted hosts use Hermes' full SSRF checks on every redirect. Redirect bodies
are discarded without buffering; HTTPS downgrade is refused. DNS safety checks
run off the gateway event loop. Redirect count and final streamed payload size
are bounded, even when Content-Length is absent or dishonest.

The private-address exception uses an internal Hermes network-backend API.
It fails closed when that API is incompatible; there is no unguarded HTTP client
fallback. Test against the actual Hermes core revision before upgrading.

Set `voice_transcription: false` or `XMPP_VOICE_TRANSCRIPTION=false` to disable
incoming voice processing. Ordinary HTTPS URLs then remain text; encrypted
key-bearing URLs are still withheld.

## Verification

`tests/test_outgoing_voice_serialization.py` uses real Slixmpp message creation,
SFS metadata/hash generation, and wire serialization for DM and groupchat.
Only upload and stream boundaries are replaced. Inbound tests cover carrier
detection, encrypted attachments, failure handling, authorization, size limits,
trusted-host policy, and key-bearing text suppression.

The optional `tests/test_installed_voice_transport.py` checks load an actual
Hermes checkout selected by `HERMES_SOURCE`. They do not read operator config or
credentials. They skip unless `HERMES_SOURCE` is set; an explicitly requested but missing
checkout fails rather than skips. CI runs standalone and pinned-core jobs on
Python 3.13 and 3.14. The supported core revision is recorded in the workflow.
A standalone suite pass does not establish core transport compatibility.

Offline fixtures do not establish live client playback or network reachability.
After deployment, send new voice messages and verify recipient download and
audible playback in both DM and MUC. Recheck denied occupants and inbound STT.
Other clients, anonymous room policies, upload endpoints, and Hermes revisions
need their own acceptance tests.
