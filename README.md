# Hermes XMPP/Jabber plugin

Third-party XMPP/Jabber gateway platform plugin for Hermes Agent.

See [voice messages](docs/voice-messages.md) for audio transport and STT,
and [MUC sender authorization](docs/muc-authorization.md) for room permissions
and migration from room-only authorization.

This packages the work from the upstream Hermes Agent XMPP PRs as an optional
user plugin so self-hosters can use XMPP now instead of waiting for the core PR
to land. It supports:

- 1:1 XMPP chats
- MUC group rooms
- mandatory STARTTLS to the XMPP server
- XEP-0085 typing indicators
- XEP-0363 HTTP file upload for media/files
- XEP-0004 data forms (e.g., clarify prompts)
- XEP-0050 ad-hoc commands
- XEP-0394 message markup (bold, code, lists)
- XEP-0461 threaded message replies
- XEP-0444 message reactions (e.g., 👀/✅/❌ for processing status)
- XEP-0447 stateless file sharing for voice messages
- OMEMO end-to-end encryption (on by default when slixmpp-omemo is installed)
- live audio calls with the agent, in both directions (optional, off by default)
- cron and `send_message` delivery through a standalone sender hook
- Hermes platform plugin registration via `ctx.register_platform(...)`

Traffic is encrypted to your XMPP server with TLS. When OMEMO is enabled and
slixmpp-omemo is installed, 1:1 messages are also end-to-end encrypted so the
server cannot read content. Rooms (MUC) are not OMEMO-encrypted.

## Credits

Most adapter code is derived from Eric Lars Lee's upstream PR #17469. Mibay's
PR #3105 is credited for earlier XMPP/OMEMO exploration. See `ATTRIBUTION.md`
for the full credit note. This repo exists because waiting is annoying, not
because the packager wants credit for other people's work. Tiny open-source goblin
energy, responsibly attributed.

## Quick start

1. Install the plugin and enable it (next section).
2. Put `XMPP_JID`, `XMPP_PASSWORD` and `XMPP_ALLOWED_USERS` in the profile's `~/.hermes/.env`
   (the bot account must already exist on your server).
3. Restart the gateway, then message the bot from an allowed JID.
4. Optional: `XMPP_HOME_CHANNEL=<your bare JID>` so cron jobs and `send_message` can reach you.
5. Optional: audio calls, see [Calls quick start](#calls-quick-start); encryption, see
   [OMEMO](#omemo-end-to-end-encryption-optional); several agents, see
   [Multiple Hermes profiles](#multiple-hermes-profiles).

## Install

For managed Hermes installations, use supported plugin/package management;
do not run direct pip/uv commands against the managed runtime. Preserve the
existing enabled-plugin list.

From the Hermes profile you want to use:

```bash
cd ~/.hermes/plugins
git clone https://github.com/fastfinge/hermes-xmpp-plugin.git
uv pip install --python ~/.hermes/hermes-agent/venv/bin/python -r ~/.hermes/plugins/hermes-xmpp-plugin/requirements.txt
hermes config set plugins.enabled '["hermes-xmpp-plugin"]'
```

If you run Hermes from a different checkout/venv, install requirements into that
Python environment instead.

Restart the gateway after installing or changing env vars:

```bash
hermes gateway restart
```

## Configure with env vars

Add these to `~/.hermes/.env` for the active profile:

```env
XMPP_JID=hermes@example.org
XMPP_PASSWORD=your-password
XMPP_ALLOWED_USERS=sam@example.org
# Optional:
XMPP_HOST=example.org
XMPP_PORT=5222
# XMPP_DIRECT_TLS=true   # force XEP-0368 direct TLS; auto-on when XMPP_PORT=5223
# XMPP_USE_IPV6=false    # disable IPv6 (default: auto-detect a working global v6 route)
# XMPP_CONNECT_TIMEOUT_SECS=240  # default 180s; raise further on a slow/high-latency link
XMPP_MUC_ROOMS=room@conference.example.org/hermes
XMPP_MUC_NICK=hermes
XMPP_HOME_CHANNEL=sam@example.org
```

`XMPP_ALLOWED_USERS` uses personal bare JIDs. MUC requires both a configured
room and verified personal sender in `muc_allowed_users` (defaults to
`allowed_users`). Unknown identities are denied. `XMPP_ALLOW_ALL_USERS=true`
bypasses DM checks only; it never bypasses MUC authorization.

## Configure with config.yaml

You can also use config.yaml:

```yaml
plugins:
  enabled:
    - hermes-xmpp-plugin

xmpp:
  enabled: true
  jid: hermes@example.org
  password: ${XMPP_PASSWORD}
  allowed_users:
    - sam@example.org
  muc_rooms: "room@conference.example.org/hermes"
  home_channel: sam@example.org
```

Set each value in one place. When both are set the adapter reads the `config.yaml` value first
for most keys (`xmpp.*` is copied to the adapter; the env var is the fallback), and
`config.yaml` only fills an env var that is not already set. Not every key has both forms; the
table below lists them.

### All settings

JID lists accept a YAML list or a comma-separated string; write JIDs in lowercase (matching
against `allowed_users` is case-sensitive). Booleans accept `1/true/yes/on` unless noted. In a
multiplexed gateway read [Multiple Hermes profiles](#multiple-hermes-profiles).

| `xmpp.` key | Env var | Default | What it does |
|---|---|---|---|
| `jid` | `XMPP_JID` | required | Bot account (bare JID). |
| `password` | `XMPP_PASSWORD` | required | Bot password. Prefer `${XMPP_PASSWORD}` in YAML. |
| `host` | `XMPP_HOST` | none (SRV / JID domain) | Server host override. |
| `port` | `XMPP_PORT` | `5222` | Client port. |
| `direct_tls` | `XMPP_DIRECT_TLS` | on only if port is `5223` | XEP-0368 TLS from byte one; STARTTLS stays the fallback. Plaintext is refused in every configuration. |
| (env only) | `XMPP_USE_IPV6` | auto-detect | `true`/`false` forces IPv6 on/off; auto uses it only with a working global route. |
| `connect_timeout_secs` | `XMPP_CONNECT_TIMEOUT_SECS` | `180` s | How long the background watchdog waits for the session. Invalid or ≤ 0 values are ignored. |
| `max_message_length` | `XMPP_MAX_MESSAGE_LENGTH` | `10000` characters | Longer replies are split into several stanzas. |
| `allowed_users` | `XMPP_ALLOWED_USERS` | empty (nobody) | Personal bare JIDs allowed to DM the bot. Also gates presence subscriptions. |
| `allow_all_users` | `XMPP_ALLOW_ALL_USERS` | `false` | `true/1/yes` lets anyone DM. Never bypasses MUC authorization and does not make anyone callable. |
| `muc_rooms` | `XMPP_MUC_ROOMS` | none | `room@conference.host[/nick]`, comma-separated. |
| `muc_nick` | `XMPP_MUC_NICK` | `hermes` | Default nickname in rooms. |
| `muc_allowed_users` | `XMPP_MUC_ALLOWED_USERS` | the value of `allowed_users` | Personal JIDs that may talk to the bot in a configured room. |
| `home_channel` | `XMPP_HOME_CHANNEL` | none | Default JID or room for cron and `send_message` delivery. YAML may be a string or `{chat_id: ...}`. |
| `omemo_enabled` / `omemo.enabled` | `XMPP_OMEMO_ENABLED` | `true` | OMEMO is used only when `slixmpp-omemo` and `omemo` are installed; if enabled but missing, a warning is logged and chat is plaintext over TLS. |
| `omemo_storage_path` / `omemo.storage_path` | `XMPP_OMEMO_STORAGE_PATH` | `<profile home>/xmpp_omemo.json` | OMEMO key file. One file per profile. |
| `agent_peers` | `XMPP_AGENT_PEERS` | none | Partner agents (see Several agents). |
| `agent_peer_limit` | none | `4` | Messages accepted from one partner agent per 10 minutes; the rest are dropped with a log warning. Whole number 1-1000, anything else uses the default. |
| `message_tool_limit` | none | `10` | `xmpp_message` sends per recipient per 10 minutes. Whole number 1-1000, anything else uses the default. |
| `disable_clarify_forms` | `XMPP_DISABLE_CLARIFY_FORMS` | `false` | Always use the numbered text clarify instead of XEP-0004 forms (`true/1/yes`). |
| `voice_transcription` | `XMPP_VOICE_TRANSCRIPTION` | `true` | Transcribe inbound voice notes with the Hermes `stt` provider. |
| `voice_trusted_hosts` | `XMPP_VOICE_TRUSTED_HOSTS` | the bot's domain and subdomains | Upload hosts allowed to resolve to private/LAN/tailnet addresses; `none` for no exceptions. |
| (env only) | `XMPP_REACTIONS` | `true` | `false/0/no` turns off the 👀/✅/❌ reactions. |
| `calls.*` | `XMPP_CALLS_ENABLED` | off | Audio calls; see [Call options](#call-options). |

Env vars not listed in `plugin.yaml` (they are optional and read by the adapter):
`XMPP_USE_IPV6`, `XMPP_REACTIONS`, `XMPP_DISABLE_CLARIFY_FORMS`.

## OMEMO end-to-end encryption (optional)

OMEMO is **on by default** (`omemo_enabled` defaults to `true`) but only works when the extra
packages are installed. `plugin.yaml` lists them, so a managed install brings them; otherwise:

```bash
uv pip install --python ~/.hermes/hermes-agent/venv/bin/python slixmpp-omemo omemo
```

Without them the adapter logs a warning and runs without OMEMO (TLS to the server only). To turn
it off on purpose, set `XMPP_OMEMO_ENABLED=false`. Optional settings in `~/.hermes/.env`:

```env
XMPP_OMEMO_ENABLED=true
# Optional — defaults to <profile home>/xmpp_omemo.json
XMPP_OMEMO_STORAGE_PATH=/home/fastfinge/.hermes/xmpp_omemo.json
```

Or in `config.yaml`:

```yaml
xmpp:
  omemo_enabled: true
  omemo_storage_path: /home/fastfinge/.hermes/xmpp_omemo.json
```

On first connect, the adapter generates an OMEMO device key and publishes it to
the server. This may take a few seconds — the adapter waits for initialization
before sending encrypted messages. If the recipient hasn't published device keys
yet, or encryption fails for any other reason, the message is sent **unencrypted** (with a log
warning `OMEMO: encryption failed for ..., sending plaintext`). Inbound OMEMO messages that cannot
be decrypted are dropped with a log warning.

Only 1:1 (`chat`) messages are sent through OMEMO by the send path; treat rooms as visible to the
server. 1:1 encryption is the primary use case.

## Multiple Hermes profiles

XMPP is nice for profile isolation because you can create one JID per profile:

- `sam-hermes@example.org` for your profile
- `mom-hermes@example.org` for Mom's profile
- `dad-hermes@example.org` for Dad's profile

Each Hermes profile gets its own `.env`, memory, sessions, and gateway process.

Rules for several profiles (or several agents) on one server:

- **One XMPP account per profile.** Two adapters on the same JID are refused (`xmpp_lock`).
- Each profile has its own `XMPP_JID`, `XMPP_PASSWORD`, `allowed_users` and OMEMO key file.
- **Multiplexed gateway (`multiplex_profiles`, one process for several profiles):** put each
  profile's `XMPP_*` values in that profile's own `.env`. An adapter never borrows the launch
  profile's environment (it would otherwise bind the wrong JID), and the launch profile's `xmpp:`
  block does not export env vars for the others. The `xmpp_call` and `xmpp_message` tools use the
  adapter of the profile the agent runs in. Tests cover this for credentials only; verify the
  call options per profile on your setup.
- **Agents that talk to each other:** list each other in `allowed_users` **and** `agent_peers`;
  see [Several agents, one operator](#several-agents-one-operator) for who may call whom and for
  the cron hand-off.

## Server notes

Prosody needs modules for MUC, HTTP file upload, and the new XEPs. On Debian/Ubuntu:

```bash
sudo apt install prosody
sudo prosodyctl adduser hermes@example.org
```

Enable/configure in Prosody:
- `muc` — group rooms
- `http_file_share` — file upload (XEP-0363)
- `mod_groups` — optional, for ad-hoc command roster

For audio calls add a TURN service (`mod_turn_external` or `mod_external_services` plus coturn);
see [Server requirements for calls](#server-requirements-for-calls). No Jingle module is needed.

For ejabberd, enable MUC plus `mod_http_upload`. The new features (reactions,
replies, markup) use standard XMPP stanzas and should work on any modern server
supporting XEP-0444, XEP-0461, and XEP-0394.

## Troubleshooting

- `xmpp_auth_failed`: wrong JID/password, or server auth policy issue.
- `xmpp_connect_timeout`: the gateway will report the platform as connected
  right away (connecting doesn't block gateway startup), but a background
  watchdog gives up and retries if the session hasn't actually established
  within `XMPP_CONNECT_TIMEOUT_SECS` (default 180s). A common cause on home
  networks: DNS returns both an A and AAAA record for the server, the IPv6
  route is silently blackholed, and the OS burns through its full TCP retry
  budget (~130s on Linux defaults) before falling back to IPv4 — which then
  connects instantly. Check `python3 -c "import socket;
  print(socket.getaddrinfo('your.server', 5222))"` for a AAAA record, test
  IPv6 reachability separately (`ping6`/`curl -6`), and either fix IPv6
  routing or disable IPv6 for this host if it's not actually usable. If
  raising `XMPP_CONNECT_TIMEOUT_SECS` further fixes it, that confirms this
  cause. Otherwise it's a DNS/firewall/SRV/reachability issue — try
  `XMPP_HOST`/`XMPP_PORT` or check the server is reachable at all.
- `xmpp_connection_lost`: an established session dropped unexpectedly (server
  restart, network blip); the gateway's reconnect watcher should retry.
- `xmpp_lock`: another gateway process is already logged into this JID; stop
  it first, or use a separate bot account per gateway.
- HTTP upload failure: enable XEP-0363 on the server and check max file size.
- DM rejected: add your bare JID to `XMPP_ALLOWED_USERS`.
- MUC silent: add room to `XMPP_MUC_ROOMS`, invite the bot, and put the sender's personal JID in
  `muc_allowed_users` (or `allowed_users`).
- Replies arrive unencrypted although OMEMO is on: look for the `OMEMO: encryption failed ...
  sending plaintext` warning; usually `NoEligibleDevices` (the contact has no published OMEMO
  devices yet).
- Audio calls: see [Troubleshooting calls](#troubleshooting-calls).

## Features in depth

### Message reactions (XEP-0444)

When Hermes starts processing a request, the adapter sends a 👀 reaction. When it
finishes, ✅ or ❌ is sent depending on success or error. Reactions are visible in
clients that support XEP-0444 (Conversations, Dino, Gajim, etc.).

Reactions can also be sent by Hermes tools; the adapter maps the reaction to the
original message using slixmpp's `xep_0444`.

### Threaded replies (XEP-0461)

When you reply to a bot message, the adapter tracks the thread using XEP-0461
message references. Replies from Hermes are sent back into the same thread so
context is preserved.

### Message markup (XEP-0394)

Markdown-like syntax in Hermes responses is converted to XEP-0394 message markup:
- `**bold**` → `<span style="font-weight: bold">bold</span>
- `` `code` `` → `<code>code</code>
- `` ```block``` `` → `<blockcode>block</blockcode>
  
Clients that support XEP-0394 render this natively. Others get the raw text.

### Data forms (XEP-0004) for clarify

When Hermes needs clarification, the adapter sends an XEP-0004 data form instead
of a plain text list. The form includes:
- A hidden `clarify_id` field
- A list-single choice field with the available options
- An "Other (type your own answer)" option for free-form responses

The message body also includes a numbered text fallback so that clients which
do not render `jabber:x:data` forms in `<message>` stanzas (Conversations,
ConverseJS, older Gajim) still display actionable choices. Users on those
clients can reply with the number or option text as normal.

When a client does render the form and the user submits it, the adapter reads
the form response, extracts the `clarify_id` and selected answer, and resolves
the clarification back to Hermes automatically.

**Disabling data forms:** If you prefer to always use plain text clarify
(never send XEP-0004 forms), set the environment variable
`XMPP_DISABLE_CLARIFY_FORMS=true` or add `disable_clarify_forms: true` to your
XMPP platform config in `config.yaml`. This forces the numbered text fallback
path even when the `xep_0004` plugin is registered.

### Voice messages (XEP-0447)

Voice messages from Hermes are sent as XEP-0447 Stateless File Sharing (SFS)
messages. The file is uploaded via XEP-0363 HTTP upload first, then a lightweight
SFS reference is sent. The same upload URL is included in the body and XEP-0066
OOB fallback, with a whole-body XEP-0428 marker. Clients can preview metadata or
use the compatible download reference. The SFS metadata and file hash are kept.

Live audio calls are a separate, optional feature: see [Audio calls](#audio-calls-jingle-rtp-optional).

### Inbound voice notes → speech-to-text

Send exactly one audio attachment without a caption. The plugin accepts OOB,
SFS, and decrypted XEP-0454 links, caches the audio, and passes a `VOICE` event
to Hermes core. Your configured STT provider handles transcription; no provider
endpoint or local audio engine is required by this plugin.

Authorization happens before downloads. MUC requires both a configured room
and a verified personal sender in `muc_allowed_users` (defaults to
`allowed_users`). Room membership alone is no longer sufficient. See
[MUC sender authorization and migration](docs/muc-authorization.md).

Private upload services can use a hostname-scoped exception to Hermes' SSRF
policy. The default trusts the bot's JID domain and subdomains; prefer an exact
upload hostname, or set `"none"` for no private-address exceptions:

```yaml
xmpp:
  voice_trusted_hosts: ["upload.example.org"]
  # voice_transcription: false  # Disable automatic incoming voice processing.
```

The corresponding environment settings are `XMPP_VOICE_TRUSTED_HOSTS` and
`XMPP_VOICE_TRANSCRIPTION`. HTTPS, valid TLS, size limits, and the metadata-address
block remain enforced. Trusted downloads reject redirects and environment
proxies. Encrypted key-bearing URLs are never forwarded as agent text, even
when transcription is disabled or a download fails. See
[voice transport and security details](docs/voice-messages.md).

### Audio calls (Jingle RTP, optional)

Call the bot from Conversations, Dino or any client with audio calls and talk to the agent.
Your speech is transcribed with the configured `stt` provider, the reply is a normal agent
turn (tools, memory and approvals apply), and it is spoken with the configured `tts`
provider. The agent can also call you (see [The agent can call you](#the-agent-can-call-you)).
This is turn-based speech, not full duplex: expect a few seconds of delay per turn, mostly the
agent's own thinking time. Audio only; no video.

#### Calls quick start

1. **Call packages** (aiortc, av, numpy, webrtcvad-wheels; Python 3.13 or newer). Nothing to
   install: with `calls.enabled` on, the first start builds an isolated Hermes environment for
   them (about 230 MB; later starts reuse it). If the first build takes longer than 25 seconds,
   chat starts anyway and calls come with the next reconnect or gateway restart. With calls off nothing is
   downloaded or imported. See [Where the call packages live](#where-the-call-packages-live).
2. **Providers**: `stt` and `tts` must already work in Hermes (try a voice note and a TTS reply
   first). The plugin calls them directly, so whatever they send to a cloud provider includes
   call audio.
3. **Config** (minimal; everything else has a default):

   ```yaml
   xmpp:
     jid: hermes@example.org
     password: ${XMPP_PASSWORD}
     allowed_users: [sam@example.org]   # who may chat AND call
     calls:
       enabled: true                    # or XMPP_CALLS_ENABLED=true
   ```
4. **Server**: TURN reachable by clients and bot (see [Server requirements](#server-requirements-for-calls)).
5. `hermes gateway restart`. The log must say `xmpp: audio calls enabled`. If it says
   `calls are enabled but unavailable (...)`, a call package is missing; chat keeps working.
6. In your client, make sure the bot is a contact (the bot accepts subscription requests only
   from `allowed_users` and subscribes back), then press the call button.

Calls are off by default; while off, no call code is imported.

#### Where the call packages live

The packages are big: the environment is about 230 MB, and PyAV's bundled FFmpeg is most of that
(`av` is 104-131 MB depending on the version, numpy 56 MB, everything else about 10 MB). Plenty of
agents only need chat and voice notes, so they are optional. The plugin builds them with
`pm.ensure_environment()`, Hermes' own API for an isolated dependency set, into
`<hermes root>/environments/hermes-xmpp-calls/` (shared by all profiles):

- **Optional.** Calls off means nothing is built, downloaded or imported.
- **Survives updates.** The environment is outside the plugin folder and outside Hermes' main
  venv, which is what `hermes update` rebuilds. A failed build keeps the previous generation.
- **No second copy of what Hermes has.** The environment's `site-packages` is appended to
  `sys.path`, so Hermes' own numpy, av and cryptography win. The environment is refused if its
  Python differs from Hermes' (compiled wheels would not load).
- **Declared in one place.** `requirements-calls.txt` feeds the environment, CI and the manual
  route. `av` follows aiortc's own range (14 and up; Apple Silicon has wheels only up to 15).

The alternatives were worse: `pip_dependencies` puts the 230 MB into every install, and a
hand-made folder needs a manual step again after changes.

Fallback, only for Hermes versions without `pm.ensure_environment`, or with
`security.allow_lazy_installs` off (Docker): put the packages in a `vendor/` folder next to
`adapter.py` (`uv pip install --python <hermes python> --target vendor -r requirements-calls.txt`
with Hermes' Python; `webrtcvad-wheels` needs a C compiler where no wheel exists). It is used only
when the environment cannot be built (the log says `call packages environment unavailable (...)`).
Without a `vendor/` folder the log prints that command instead.

#### Call options

All under `xmpp.calls` in `config.yaml`. Only `enabled` also has an env var. A bad value (not a
number, NaN or infinity) logs `invalid calls.<key>` and uses the default. Numeric validation
runs even with calls disabled and does not prevent chat startup. `max_seconds`, `ring_timeout`
and `silence_ms` must be positive; `filler_after` cannot be negative. Times are seconds unless noted.

| Option | Type, default | What it does |
|---|---|---|
| `enabled` (`XMPP_CALLS_ENABLED`) | bool, `false` | Master switch. The env var is used only when the YAML key is absent. Accepts `1/true/yes/on`. |
| `allowed_users` | list or comma string, empty | Who may call, and be called by, the agent. Empty: every `xmpp.allowed_users` JID except `agent_peers`. Set: exactly these (they must also pass the normal chat authorization; a listed partner agent would be callable, so do not list one). With `allow_all_users: true` and this empty, nobody can call. Several people are fine; with more than one the `xmpp_call` tool needs `to`. Compared lowercase against the caller's bare JID. |
| `max_seconds` | int or `false`, `600` | Hang up after this long, whoever is talking. `false` lifts the limit (a silent call still ends after `idle_timeout`; set that to `0` too and a call lasts until someone hangs up). |
| `greeting` | string or list, `Hi, I'm here. What can I do for you?` | Spoken when someone calls the bot (incoming calls only; outgoing calls speak the `opening`). Give a list and one is picked at random each call, never the same twice in a row. `""`, `[]` or `false` for silence. The whole phrase is synthesized once and kept (see [Phrase audio cache](#phrase-audio-cache)). Example: `["Hi, I'm here. What can I do for you?", "Hello! What's up?", "Yes? I'm listening.", "Hey there, go ahead.", "Hi! How can I help?", "I'm all ears. What do you need?"]`. |
| `silence_ms` | int ms, `800` | Pause that ends your turn. Raise (1000-1500) if you get cut off while thinking; lower for snappier answers. |
| `vad_level` | int 0-3, `2` | Noise rejection (WebRTC VAD). Higher ignores more background noise but may miss soft speech. Out-of-range values are clamped. Steady hum, fans and hiss are ignored at every level. |
| `ack_tone` | bool or file, `true` | The short beep when your turn is captured. `false` or `""` turns it off. A path plays that sound instead (see [Custom sounds](#custom-sounds)). |
| `warn_tone` | bool or file, `true` | The two falling beeps before a planned hang-up. `false` or `""` turns them off (the `hangup_grace` wait still happens, in silence). A path plays that sound instead. |
| `idle_timeout` | float, `60` | Seconds without anyone speaking (and no reply playing or being worked on) before the agent says goodbye and hangs up. `0` turns it off. |
| `hangup_grace` | float, `5` | Seconds you get to speak, after two falling beeps, before a planned hang-up (a goodbye, or `hang_up_after`). Speaking cancels it. `0` hangs up at once. Negative is treated as `0`. |
| `barge_in` | `queue` \| `stop`, `queue` | `queue`: talking while the agent speaks cuts nothing off; what you say becomes the next turn (or is steered into the running turn, see below). `stop`: your voice cuts the agent off. Any other value means `queue`. |
| `filler_after` | float, `3.0` | Seconds of waiting for the agent before it says the `filler`. |
| `filler` | string or list, six built-in phrases | What the agent says when it is still working after `filler_after` seconds. The built-ins are "One moment.", "Let me check.", "Working on it.", "Just a second.", "Give me a moment." and "Bear with me." Give a list to use your own; one is picked at random each time and never the same twice in a row. A single string always says that. `""` or `[]` turns it off. The audio is made once per phrase and kept in `<HERMES_HOME>/cache/xmpp-calls/` (see [Phrase audio cache](#phrase-audio-cache)). |
| `recap` | bool, `true` | After a call in which you asked for something, the agent texts you one or two sentences saying what you asked and what it did (silent if you asked for nothing). |
| `log_transcripts` | bool, `false` | Log what was said. Off: the log only has word counts. See [Privacy](#privacy). |
| `ring_timeout` | int, `60` | Outgoing calls: ring for up to this long. Phones stop ringing by themselves after about half a minute; the plugin rings again until this time is up (a rejection within 25 s is respected as a refusal and not re-rung; with `ring_attempts` above 1 the next attempt still rings after `ring_pause`). The same limit applies separately to the device accepting and to audio connecting. |
| `ring_attempts` | int 1-5, `1` | Outgoing calls: how many times to ring before the message goes out as text. Out-of-range values are clamped. The agent can ask for more per call (`attempts`, urgent only). |
| `ring_pause` | float, `60` | Seconds between rings when there are several attempts. |
| `busy_wait` | float, `90` | An outgoing call waits this long for the current call to end instead of failing as `busy`. |
| `max_calls_per_hour` | int 1-100, `6` | How many calls the agent may place with `xmpp_call` per hour (a runaway agent must not ring you all day). |
| `opening_max_chars` | int 50-10000, `1500` | Longest `opening` the agent may speak when it places a call. |

Negative `idle_timeout`, `busy_wait`, `ring_pause` and `hangup_grace` count as `0`. `max_seconds` is a whole number of at least 1 or `false`. `greeting: null` is silence, like `""`. `greeting` takes up to 20 non-empty strings of up to 500 characters and `filler` up to 20 of up to 200; anything else is ignored with a warning, and `false` turns either off. `max_calls_per_hour` and `opening_max_chars` are clamped to their range.

Fixed, not configurable: one call at a time, DTLS-SRTP required, Opus the only codec,
ICE-UDP the only transport. Audio/video and multiple-content offers are refused; this plugin
cannot negotiate video upgrades or ICE restarts. Unsupported Jingle actions get an IQ error.

Ringing and setup stop when the controller disconnects; a late answer cannot start a new call.
Retractions and duplicate JMI proposals are tracked for the last 256 proposal IDs in memory.
Delayed proposals older than `ring_timeout` are ignored; the cache does not survive a restart.
Received JMI carbons are accepted only from the account's own bare JID; sent and forged carbons
are ignored. This requires the server/client carbon feature to be enabled. A standalone audio
Jingle initiation without a preceding proposal is supported (Dino uses this path).

#### How a call behaves

**Beeps.** Three short sounds tell you what the agent heard, without a word:

| Sound | Meaning |
|---|---|
| One short beep (`ack_tone`) | It heard you finish speaking and is sending your turn to the agent. Any noise the voice detector takes for speech also gets one, even if the transcript turns out empty. |
| Two falling beeps (`warn_tone`) | The agent is about to hang up (after a goodbye or with `hang_up_after`). Speak within `hangup_grace` seconds to keep the call open. Only recognised words count: room noise that the voice detector takes for speech does not cancel the hang-up (with `barge_in: queue`). |
| "One moment." (spoken) | The agent is still working after `filler_after` seconds. |

Short transcripts that are only a stock phrase such as "Thank you." or "Thanks for watching" are treated
as noise and dropped, because speech-to-text models often invent them from room sound.

**How it knows you finished.** Speech is told from noise by the WebRTC voice detector. After
`silence_ms` without speech the turn is cut, the beep confirms it, and the agent answers. If you
only paused to think and the agent has not replied yet, the two parts are joined into one turn.
Muting your microphone ends the turn at once. Another person talking in the background still
counts as speech; no frame-level detector can tell them apart from you.

**Talking while the agent talks.** With `barge_in: queue` (default) you may keep talking: the
agent finishes its sentence, then answers everything you said as one message. If the agent is
still working (a tool call, a search) when you speak, your words are sent to it as `/steer`, so
it takes them in after its next tool call without restarting; if that is not possible they are
queued (the `/steer` path has not been verified against a live gateway). With `barge_in: stop`
your voice cuts the agent off and its next turn is told how much you heard. Say goodbye and the
agent ends the call. A reply that cannot be spoken (TTS down or busy, after retries) is posted
to the chat as text instead.

Playback applies backpressure before synthesizing further sentences: about five seconds of
queued audio plus the next atomic sentence. A very long sentence can exceed that target.
Failure to synthesize the optional filler does not discard the agent's actual answer.

### Phrase audio cache

The first time a phrase is needed (a `filler` or a `greeting`), it is synthesized and saved as
`<HERMES_HOME>/cache/xmpp-calls/phrase-<hash of the phrase>.npy` (one file per phrase; `HERMES_HOME`
is the active profile's home, so each profile has its own cache). Later calls (and restarts) play
that file without asking the TTS provider. Changing the phrases makes new files. **If you change the bot's voice
or TTS provider, delete the `cache/xmpp-calls/` folder**; the next call regenerates the phrases in
the new voice. A damaged, empty or oversized file is ignored and regenerated. Only fillers and greetings are
cached; other speech is synthesized per call. If synthesis fails, a greeting is spoken the normal way
(and posted as text when that fails too); a failed filler is skipped.

### Custom sounds

`ack_tone` and `warn_tone` take a path to any audio file PyAV can read (WAV, OGG, MP3, ...). `~`
and absolute paths work; a relative path is looked up in the Hermes home. It is resampled to 48 kHz
mono and cut at 5 seconds. A file that cannot be read logs `cannot read the sound ...` and the built-in
beep is used instead. No sounds ship with the plugin, so make or download your own, for example a
short chime: `ffmpeg -f lavfi -i "sine=frequency=1200:duration=0.15" ~/.hermes/chime.wav`
(PyAV is only a binding to FFmpeg's libraries and carries no sound library, but FFmpeg's `sine`
source above makes any tone you like).
`false` or `""` turns a sound off entirely.

```yaml
xmpp:
  calls:
    ack_tone: ~/.hermes/sounds/chime.wav   # your file instead of the built-in beep
    warn_tone: false                       # no falling beeps (the hangup_grace wait stays, silent)
```

**Hanging up does not cancel what you asked.** If you hang up in the middle of a request, or the
time limit ends the call, the plugin still transcribes what you were saying, lets the agent finish
(tools included), and sends the answer to your chat as text. Already queued requests are processed
too, including when a previous reply said goodbye or failed. This finishing work has a 180-second
budget; remaining local tasks are cancelled afterward. Cancelling a task cannot undo completed
tool actions or forcibly stop a provider thread. Late synthesis never adds audio to a closed call.
You can call back later and follow up; each call has a separate session, with missed-call context
only as described below. After a call with answered turns, the recap text tells you what the
agent understood and did (unless `recap` is off or it returns `NONE`).

**Missed calls.** If you do not answer or you decline an agent's call (after `ring_attempts` rings), the opening is
sent to your chat as text and remembered: when you call back, the agent knows what it wanted to
tell you. The memory is in the gateway process only; a restart forgets it.

**Sessions.** Every call is its own Hermes session (a DM thread `call-<id>`, titled "Call from|to
<person> <date time>", plus the `topic` the agent gave to `xmpp_call`, such as a cron job name),
with the full history in your sessions list. The agent has all the tools of its profile in a
call, including delegation and messaging; it is only asked to keep spoken replies short (one to
three plain sentences). A spoken turn waits at most 180 s for the agent.

#### The agent can call you

The plugin registers two tools in toolset `xmpp_calls`: `xmpp_call` and `xmpp_message`. Enable
that toolset for the gateway and for any cron job that should call or message. `xmpp_message`
exists while the profile's XMPP adapter is connected; it needs neither `calls.enabled` nor the
call packages. `xmpp_call` exists only while the adapter is connected with calls enabled. Otherwise
`xmpp_call` reports "calls are not enabled or XMPP is not connected" and `xmpp_message` reports
"XMPP is not connected". They return JSON
`{"success": bool, "status": "..."}`.

`xmpp_call`:

| Argument | What it does |
|---|---|
| `opening` | Required. What the agent says when you pick up: a greeting or a whole briefing in plain spoken sentences. It is synthesized while the phone rings, so there is no gap. |
| `uninterruptible` | `true`: the opening is delivered in full, whatever you say meanwhile (your speech is ignored until it ends). Default `false`. |
| `hang_up_after` | `true`: end the call when the opening is done (after the `hangup_grace` beeps). Default `false`: the conversation continues and the agent remembers what it said. |
| `to` | Bare JID to call; optional when only one user is callable. Must be callable (see `allowed_users`), else the tool lists who is. |
| `topic` | Short name of what the call is about, such as a cron job name (60 characters max); it ends up in the session title. If the call follows a `CALL REQUEST` message from a partner agent, the plugin takes `topic=` from it when the model leaves it out. |
| `context` | Background the agent keeps for the call and never speaks (a reference and the steps to follow if you ask for a change; 3000 characters max). Taken from the same `CALL REQUEST` message when omitted. |
| `attempts` | Urgent only: ring this many times (1-5) before the text fallback. Default `ring_attempts`. |
| `pause_seconds` | Seconds between those rings (0-3600). Default `ring_pause`. |

Success means `status` starts with `connected`. Other statuses: `declined: the person rejected
the call`, `no answer: ...` (both followed by "the message was sent as text instead"),
`busy: another call is in progress`, `refused: <jid> is not allowed to be called`,
`failed: ...` (the call was accepted but audio never connected, or an internal error),
`terminated: <reason>` (the peer ended setup; returned promptly without a false timeout or
missed-call text), and `closed: ...` (the controller disconnected). If the person is already on
a call with you, the opening is spoken inside that call instead of ringing again. A call placed
while another is running waits up to `busy_wait` seconds for the line. The tool's wait covers
`attempts × (ring_timeout + pause_seconds) + 2 × ring_timeout + 30` seconds, using the configured
defaults for omitted arguments. Cancellation retracts ringing and closes setup. A connected call
keeps running after the tool returns.

`xmpp_message`: `to` (an allowed user or a partner agent) and `text`, sent through the running
adapter (never a second process). At most
10 messages per recipient per 10 minutes, or `xmpp.message_tool_limit` (then `too many messages to ...`).
Hermes itself has no model-callable send tool (its `send_message` helper is for cron and the CLI and
sends through a short-lived second connection that never uses OMEMO), so this is the only way for an
agent to message someone, encrypted, on its own.

Example cron prompt ending: *"When the job is done, call me with xmpp_call: tell me the top
options you considered, why you chose the one you published, and read me today's note.
Use uninterruptible=true."* The tool returns whether the call connected, was declined or
went unanswered, and the call then runs on its own after the job's turn ends.

#### Several agents, one operator

Give each agent its own XMPP account and list the other agents under `xmpp.agent_peers` (bare
JIDs, also `XMPP_AGENT_PEERS`). Rules:

- A partner must **also** be in `allowed_users` (on both sides), or its messages and presence
  requests are ignored.
- A partner agent can message the agent but is never called and can never call (unless you put it
  in `calls.allowed_users`, which you should not).
- Because two agents that answer each other never stop, messages from a partner are limited to 4
  per 10 minutes, or `xmpp.agent_peer_limit` (the rest are dropped with a log warning). The gateway's own status
  lines are neither sent to nor acted on from a partner: lines starting with ⚡ Interrupting, ⚙, 📞,
  ⏳, 💭, 🔧, ⏩, 🔄, 💾, ⏸, 💡, 🔁, 🗜, 📦 or "♻️ Gateway", and the transient warnings the gateway writes while
  a turn is still retrying ("I seem to be stuck", "No response from provider", "trying fallback",
  "Retrying in", "This conversation is paused", ...). A real result or warning written by the agent
  passes, even with a ⚠️ or ❌ in front. Tool-progress lines start with a different emoji per tool, so
  they cannot be recognised reliably: **give partner profiles `display.tool_progress: off`**, or the
  other agent receives a message per tool call.
- The agent is told the sender is a machine and to reply exactly `[SILENT]` when nothing needs doing.

**Two-profile example** (a phone agent that talks to you and a worker that runs cron jobs; each
profile has its own `.env`, JID and OMEMO key file):

```yaml
# phone profile: hermes-phone@example.org
xmpp:
  allowed_users: [sam@example.org, hermes-worker@example.org]
  agent_peers: [hermes-worker@example.org]
  calls: {enabled: true}                  # callable: sam only (the worker is excluded)

# worker profile: hermes-worker@example.org
xmpp:
  allowed_users: [sam@example.org, hermes-phone@example.org]
  agent_peers: [hermes-phone@example.org]
  # no calls section needed: xmpp_message works without calls
```

Which side may call: only a profile with `calls.enabled` can place calls, and only to a
callable JID (people, not partners). The worker only sends text with `xmpp_message`, which works
with calls off; nobody has to call the worker.

**Hand-off pattern (cron job → phone agent → you).** A convention between two prompts; the
plugin enforces only the steps marked *(Plugin)*.

1. The worker's cron job finishes and sends the phone agent (`xmpp_message`, or cron delivery to
   the phone agent's JID) a message that **starts with** `CALL REQUEST`. Optional
   `topic=<word>` on that first line (letters, digits, `.`, `:`, `-`, `_`), then free text
   (reference, steps for requested changes), then a line `BRIEFING:` followed by what to say.
2. *(Plugin)* The phone agent's adapter remembers `topic` and the text before `BRIEFING:` as
   context for 10 minutes (the latest request only), so `xmpp_call` fills in `topic` and
   `context` when the model forgets them. Metadata is consumed once; the guard and parser run
   after OMEMO decryption, not on the encrypted message's fallback text. The message is still
   delivered to the agent as an ordinary machine message.
3. The phone agent calls you with `xmpp_call`. If you ask for a change, it sends the task back
   with `xmpp_message`.
4. The worker replies `RESULT: ...` (plain text; the plugin does not parse it). The phone agent
   speaks it in the open call, or calls you again.

*(Plugin)* The 4-per-10-minutes partner limit applies to every step; keep exchanges short.

#### Server requirements for calls

| Need | Why |
|---|---|
| `mod_turn_external` (or `mod_external_services`, XEP-0215) with a coturn reachable by the clients **and** the bot | Clients and the bot get short-lived TURN credentials. The bot asks its own server (10 s timeout); on no answer it continues with no ICE servers, i.e. direct paths only. It uses only `turn:` services over UDP; `turns:`, TCP-only entries and STUN are ignored. |
| TLS on c2s | Already required for chat. |
| Presence subscription between the person and the bot | The bot accepts subscriptions only from `allowed_users` and subscribes back. Conversations shows the call button only for mutual contacts. |
| `mod_carbons`, `mod_smacks`, `mod_csi_simple`, `mod_cloud_notify` | Reliable delivery and wake-up of mobile clients (call proposals reach all devices). Present on a typical Prosody server. |
| No Jingle module | Jingle is carried as ordinary stanzas. The bot advertises its call features in its entity caps (XEP-0115), so run it as a normal account. |

The server's ports for TURN (UDP 3478 and coturn's relay range) must be reachable from the
phone and from the bot's host; on a Tailscale network that is an ACL matter (see
[Troubleshooting](#troubleshooting-calls)). Public-Internet deployments are untested.

#### Client compatibility

| Client | Status |
|---|---|
| Dino (Linux) | Incoming and outgoing calls tested with the bot. Sends no RTP while muted (handled). Silently skips a resource that does not advertise Jingle RTP; the bot advertises it after login. |
| Conversations (Android) | Incoming and outgoing calls tested with the bot. Needs mutual presence for the call button. |
| Monal (iOS) | Calls with the bot and voice messages tested. Needs mutual presence for the call button. |
| Monocles Chat (Android) | Incoming calls to the bot tested. |
| Others (Gajim, etc.) | Untested. The bot speaks Jingle Message Initiation (XEP-0353) and Jingle RTP with ICE-UDP, DTLS-SRTP and Opus; a client offering those may work, none of the others has been tried. |

#### Privacy

- **Audio** is never kept. It is held in memory and, per utterance, written to a temporary file
  for STT (and TTS output to a temporary file for decoding) that is deleted right after. It goes
  to whatever your `stt`/`tts` providers send it to (a cloud provider sees it).
- **Log**: by default only word counts, plus call ids, end reasons and RTP packet counts.
  `log_transcripts: true` puts both sides' full text in the log at INFO level. Exception: when
  a clip yields no words, the log line `heard no words ...` shows the (empty or stock-phrase)
  transcript.
- **Sessions**: every call is a Hermes session with its full text history, regardless of
  `log_transcripts`. Delete it from your sessions list if you do not want it kept.
- **Chat**: the recap, a missed call's full `opening` (which can be a whole briefing), and a reply
  that could not be spoken are sent to your chat as text. They are OMEMO-encrypted unless OMEMO is
  off. Set `recap: false` to skip recaps.
- **Signalling** (who calls whom, when) is ordinary XMPP, not OMEMO. Media is DTLS-SRTP (an offer
  without a DTLS fingerprint is refused), so a TURN relay sees only encrypted packets.
- The `xmpp_call` / `xmpp_message` tools can reach only `allowed_users` (and, for messages, `agent_peers`).

#### Limits

| What | Limit |
|---|---|
| Concurrent calls | 1 per adapter (a second caller hears busy) |
| Call length | `max_seconds` (600, `false` for none) |
| Spoken turn | waits at most 180 s for the agent |
| Partner agent → this agent | 4 messages / 10 min (`agent_peer_limit`) |
| `xmpp_message` | 10 / recipient / 10 min (`message_tool_limit`) |
| `xmpp_call` | 6 calls / hour (`max_calls_per_hour`); `opening` at most 1500 chars (`opening_max_chars`); `attempts` 1-5, `pause_seconds` 0-3600, `topic` 60 chars, `context` 3000 chars; the tool waits for the whole ring schedule plus the connect time |
| Call that never connects | ended after 40 s; a call whose ICE/DTLS fails mid-call is ended at once |
| Remote ICE candidates | UDP to a public/private unicast address, port 1024-65535, at most 16 per call; loopback, link-local, multicast and host names are ignored. Values that carry whitespace or control characters are refused |
| Codec / transport | Opus, ICE-UDP, DTLS-SRTP only; audio only |

#### Troubleshooting calls

First look in the gateway log. Lines to know: `xmpp: audio calls enabled`, `calls: ending <id> (<reason>)`,
`calls: <id> rtp received/sent (r, s)` (every 10 s), `calls: refusing call ...`, `media state ...`.

| Symptom | Likely cause and fix |
|---|---|
| **No call button / no ring on the person's phone (agent calls out)** | Tool says `calls are not enabled or XMPP is not connected`: calls off, dependencies missing (see the `unavailable` log line) or gateway not connected. `cannot call X; callable people: ...`: X not callable (not in `allowed_users`, is a partner agent, or not in `calls.allowed_users`). `no answer: nobody picked up in time`: no device answered the proposal within `ring_timeout` (client offline, bot not a mutual contact, or a client without call support; Dino skips resources that do not advertise Jingle RTP). |
| **Bot ignores an incoming call** (rings out, no reply) | Caller is not callable. Unauthorized calls get no answer at all. Check `allowed_users`, `calls.allowed_users`, that the caller is not in `agent_peers`, and that `allow_all_users` is not your only list (it does not make anyone callable). |
| **"Busy"** | One call at a time. A second incoming call is rejected/terminated with `busy`; an outgoing call waits `busy_wait` s, then returns `busy: another call is in progress`. A stuck call ends at `idle_timeout` or `max_seconds`. |
| **Call rings on the phone but is rejected / drops at once** | Log says `calls: refusing call ...`: the offer had no DTLS fingerprint (`security-error`), no Opus or no audio (`unsupported-applications`), or the offer was malformed (`general-error`). Use a client with DTLS-SRTP and Opus. On outgoing calls, `declined: the person rejected the call` means the person (or a quick phone timeout, under 25 s) rejected it. |
| **Connects but one-way audio or silence** | Read the `rtp received/sent` log lines. `received` 0: the person's audio does not reach the bot; `sent` growing but the person hears nothing: their path from the bot is blocked. Almost always ICE/TURN reachability: see the next row. If both counters grow and you hear nothing, it is a TTS/playback issue (below). |
| **`failed: the call was accepted but audio never connected` / DTLS timeout** | ICE or DTLS never completed within `ring_timeout`. On a Tailscale network, ACLs often allow TCP 5222 but not UDP: allow UDP 3478 and coturn's relay port range between the phone, the bot host and the coturn host (and UDP between peers if you rely on direct paths). Confirm the server really advertises a UDP `turn:` service (the bot ignores `turns:`, TCP and STUN). Give the phone a path to the relay (not battery-restricted, same tailnet). |
| **Voice reply silent, or text appears in chat instead** | TTS failed (`speech synthesis failed; sending the rest as text`): fix the `tts` provider. Nothing in chat either and `TTS also failed; staying silent`: provider down. The agent answered `[SILENT]`/empty: the host nudges once, then says nothing; check the model/prompt. Empty `greeting` is silent by design. |
| **STT empty: agent never answers, or "heard no words"** | `heard no words in a N s utterance`: noise, a stock phrase dropped as a phantom, or STT returned nothing. `transcription failed` / `STT failed:` : the `stt` provider is not configured or erroring. Lower `vad_level`, raise `silence_ms`, or check the microphone (Dino sends nothing while muted). |
| **Cut off mid-sentence / hears background noise** | Raise `silence_ms` (1000-1500) / raise `vad_level`. |
| **Hangs up by itself** | `idle_timeout` (60 s of quiet), `max_seconds` (600, unless `false`), or the agent said goodbye (two falling beeps, then `hangup_grace`). |
| **Agent has no `xmpp_call` tool** | Enable toolset `xmpp_calls` for the gateway/cron job; the tools appear only while the adapter is connected with calls enabled. |
| **OMEMO `NoEligibleDevices` on recap / missed-call text** | The bot has no device list for the person. Open the person's OMEMO-capable client once and have them send a message. Note the fallback: after an encryption failure the bot sends that text **unencrypted** and logs `OMEMO: encryption failed for ..., sending plaintext`. |

### Ad-hoc commands (XEP-0050)

The adapter registers ad-hoc commands on the bot's JID. Users can discover them
with their client's command list (e.g., `/cmd` in Conversations). Currently a
basic command list is exposed; future releases may add Hermes-specific commands.

## Development

Use an isolated test environment with `requirements.txt` and
`requirements-dev.txt`; do not modify the managed Hermes environment. Gateway
imports are mocked, while voice tests use real Slixmpp XML and cryptography.

```bash
python -m pytest tests -q -W error -o asyncio_default_fixture_loop_scope=function
```

The optional core-transport tests skip without a Hermes checkout. To include
them, provide its source and transport dependencies in the test environment:

```bash
HERMES_SOURCE=/path/to/hermes-agent python -m pytest tests -q -W error \
  -o asyncio_default_fixture_loop_scope=function
```

Run real `hermes plugins doctor /path/to/plugin --ci` separately. A standalone
suite pass does not establish live gateway or client interoperability.
