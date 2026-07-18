# Hermes XMPP/Jabber plugin

Third-party XMPP/Jabber gateway platform plugin for Hermes Agent.

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
- OMEMO end-to-end encryption (optional, needs slixmpp-omemo)
- cron and `send_message` delivery through a standalone sender hook
- Hermes platform plugin registration via `ctx.register_platform(...)`

Traffic is encrypted to your XMPP server with TLS. When OMEMO is enabled and
slixmpp-omemo is installed, 1:1 messages are also end-to-end encrypted so the
server cannot read content. MUC OMEMO support depends on client/device
availability.

## Credits

Most adapter code is derived from Eric Lars Lee's upstream PR #17469. Mibay's
PR #3105 is credited for earlier XMPP/OMEMO exploration. See `ATTRIBUTION.md`
for the full credit note. This repo exists because waiting is annoying, not
because the packager wants credit for other people's work. Tiny open-source goblin
energy, responsibly attributed.

## Install

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
# XMPP_CONNECT_TIMEOUT_SECS=240  # default 180s; raise further on a slow/high-latency link
XMPP_MUC_ROOMS=room@conference.example.org/hermes
XMPP_MUC_NICK=hermes
XMPP_HOME_CHANNEL=sam@example.org
```

`XMPP_ALLOWED_USERS` uses bare JIDs. MUC access is gated by room membership: if
the bot joins a room listed in `XMPP_MUC_ROOMS`, messages in that room are accepted.
For quick local testing only, set `XMPP_ALLOW_ALL_USERS=true`.

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
  muc_rooms:
    - room@conference.example.org/hermes
  home_channel: sam@example.org
```

Env vars win when both are set.

## OMEMO end-to-end encryption (optional)

Install the extra dependencies:

```bash
uv pip install --python ~/.hermes/hermes-agent/venv/bin/python slixmpp-omemo omemo
```

Then set in `~/.hermes/.env`:

```env
XMPP_OMEMO_ENABLED=true
# Optional — defaults to ~/.hermes/xmpp_omemo.json
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
yet, messages fall back to plaintext (with a log warning).

MUC OMEMO is supported when all participants have compatible devices, but group
encryption reliability varies by client. 1:1 encryption is the primary use case.

## Multiple Hermes profiles

XMPP is nice for profile isolation because you can create one JID per profile:

- `sam-hermes@example.org` for your profile
- `mom-hermes@example.org` for Mom's profile
- `dad-hermes@example.org` for Dad's profile

Each Hermes profile gets its own `.env`, memory, sessions, and gateway process.

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
- MUC silent: add room to `XMPP_MUC_ROOMS` and invite the bot.

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

Users pick one option and submit it. The adapter reads the form response and
forwards it back to Hermes.

### Voice messages (XEP-0447)

Voice messages from Hermes are sent as XEP-0447 Stateless File Sharing (SFS)
messages. The file is uploaded via XEP-0363 HTTP upload first, then a lightweight
SFS reference is sent. This allows clients to preview metadata before downloading.

Voice calls (Jingle) are not yet supported because slixmpp does not include a
Jingle RTP implementation.

### Ad-hoc commands (XEP-0050)

The adapter registers ad-hoc commands on the bot's JID. Users can discover them
with their client's command list (e.g., `/cmd` in Conversations). Currently a
basic command list is exposed; future releases may add Hermes-specific commands.

## Development

The test suite runs standalone — no Hermes checkout required. It uses mocks for the
Hermes gateway and slixmpp internals.

```bash
python -m pytest -q
```

For testing against a real Hermes checkout, set `PYTHONPATH`:

```bash
PYTHONPATH=/path/to/hermes-agent python -m pytest -q
```
