> **Superseded.** This is the original feasibility note (2025). Audio calls are now implemented in `calls/`; see the README, section "Audio calls".

# Jingle Voice Calls — Feasibility Investigation

**Date:** 2025-05-31
**Status:** Research complete — recommendation at bottom

---

## 1. Does slixmpp 1.15.0 include Jingle RTP support?

**No.** slixmpp 1.15.0 ships ~100 XEP plugins but **not** XEP-0166 (Jingle core) or XEP-0167 (Jingle RTP Sessions). It does include:

| XEP | Purpose | Present? |
|-----|---------|----------|
| XEP-0166 | Jingle (session management) | ❌ |
| XEP-0167 | Jingle RTP Sessions (audio/video) | ❌ |
| XEP-0176 | Jingle ICE-UDP Transport | ❌ |
| XEP-0177 | Jingle Raw-UDP Transport | ❌ |
| XEP-0234 | Jingle File Transfer | ✅ (in slixmpp core) |
| XEP-0353 | Jingle Message Initiation | ✅ (in slixmpp core) |

XEP-0234 and XEP-0353 exist for **file transfer** signalling, not voice calls.

---

## 2. Third-party library: slixmpp-jingle 0.9.0

**Found on PyPI:** https://pypi.org/project/slixmpp-jingle/

- **Version:** 0.9.0 (released 2025-03-06)
- **Author:** apprenticius (Elmar Meyer)
- **License:** Appears to follow slixmpp's MIT license
- **Size:** 12.3 kB source / 27.8 kB wheel (very small)

### What it provides

`slixmpp-jingle` is a **signalling-only** library that registers as slixmpp plugins and handles XMPP Jingle stanza construction/parsing. It covers:

| XEP | Module | Description |
|-----|--------|-------------|
| XEP-0166 | `xep_0166/jingle.py` | Core Jingle session management (initiate/accept/terminate) |
| XEP-0167 | `xep_0167/rtp.py` | RTP session descriptions (payload types, SDP↔Jingle conversion) |
| XEP-0176 | `xep_0176/transport.py` | ICE-UDP transport (candidate parsing from SDP) |
| XEP-0177 | `xep_0177/transport.py` | Raw-UDP transport |
| XEP-0215 | `xep_0215/services.py` | External Service Discovery |
| XEP-0262 | `xep_0262/zrtp.py` | ZRTP key agreement in RTP |
| XEP-0293 | `xep_0293/rtcpfb.py` | RTP Feedback Negotiation |
| XEP-0294 | `xep_0294/hdrext.py` | RTP Header Extensions |
| XEP-0320 | `xep_0320/fingerprint.py` | DTLS-SRTP Fingerprint |
| XEP-0338 | `xep_0338/group.py` | Jingle Grouping Framework |
| XEP-0339 | `xep_0339/source.py` | Source-Specific Media Attributes |

### What it does NOT provide (critical gaps)

This is **signalling only** — it converts between SDP and Jingle XML stanzas. It does NOT include:

1. **RTP media transport** — no audio capture, playback, or packet handling
2. **ICE connectivity checks** — no STUN/TURN interaction, no actual peer connection
3. **DTLS-SRTP** — parses fingerprint stanzas but doesn't perform key negotiation
4. **Audio codecs** — no Opus/PCM encoding/decoding
5. **Media pipeline** — no connection to any audio hardware or streaming framework

The code works by parsing SDP strings with regex and converting them to Jingle XML. Example from `xep_0167/rtp.py`:

```python
def make_description(self, sdp, media):
    m = re.search(r'^m='+media+' +(\d+) +([\w/]+)([ \d]*)
$', sdp, re.M)
    # ... parses SDP lines into Jingle Description stanzas
```

### Quality concerns

- **SyntaxWarnings** on invalid regex escapes (`\d`, `\w` in raw strings) — suggests limited Python 3.12+ testing
- **German comments** left in production code ("hier müsste noch geprüft werden")
- **Test stubs**: `md5` hash used for ICE candidate IDs with `# Test Elmar!` comments
- **No documentation** on PyPI (blank project description)
- **No visible GitHub repository** — could not find public source or issue tracker
- **0.9.0 version** suggests pre-release quality
- **No test suite** shipped with the package

### Integration model

`slixmpp-jingle` uses slixmpp's `BasePlugin` / `register_plugin` mechanism. Integration would look like:

```python
from slixmpp_jingle.xep_0166 import XEP_0166
from slixmpp_jingle.xep_0167 import XEP_0167
from slixmpp_jingle.xep_0176 import XEP_0176

client.register_plugin('xep_0166')
client.register_plugin('xep_0167')
client.register_plugin('xep_0176')

# Handle incoming Jingle session requests
client.add_event_handler('jingle_session_initiate', on_jingle_initiate)
```

---

## 3. Debian packaging

```
$ apt-cache search slixmpp
python-slixmpp-doc       - documentation
python3-slixmpp          - core library
python3-slixmpp-lib      - optional binary module
python3-slixmpp-omemo    - OMEMO encryption plugin
```

**No Jingle-related Debian packages exist.** The OMEMO plugin is packaged but Jingle is not.

---

## 4. Other Python libraries considered

| Library | Status | Notes |
|---------|--------|-------|
| **aiortc** | Available, mature | WebRTC for Python (asyncio). Handles ICE, DTLS, SRTP, media. No XMPP/Jingle integration. |
| **nbxmpp** | Available | Gajim's XMPP library. Has some Jingle support but tied to Gajim's GTK UI. Not slixmpp-compatible. |
| **poezio** | Available | Console XMPP client using slixmpp. No Jingle RTP support. |
| **giggle.js** | JavaScript only | Jingle in browser — not Python. |
| **libjingle** | C++ only | Google's original. Deprecated in favor of WebRTC. |

### The aiortc bridge option

`aiortc` could theoretically provide the media transport layer that `slixmpp-jingle` lacks:

- `aiortc` handles: ICE, DTLS, SRTP, media capture/playback, codecs (Opus, VP8)
- `slixmpp-jingle` handles: Jingle XML stanza generation/parsing, SDP↔Jingle conversion
- **Missing piece:** A bridge that feeds `aiortc`'s SDP offers/answers through `slixmpp-jingle`'s Jingle signalling, and vice versa.

This bridge does not exist and would need to be written.

---

## 5. Hermes adapter compatibility

The Hermes `BasePlatformAdapter` is **message-oriented**, not session-oriented:

- **Inbound:** `MessageEvent` with `MessageType` enum (TEXT, VOICE, AUDIO, etc.)
- **Outbound:** `send()`, `send_voice()`, `play_tts()` — all send pre-recorded or generated content
- **Voice:** `send_voice()` sends a file attachment; `MessageType.VOICE` receives one
- **No live call API** — no bidirectional real-time media stream abstraction

A Jingle voice call would require a fundamentally different interaction model:

1. **New event types:** CallOffer, CallAnswer, CallTerminate, CallIceCandidate
2. **New adapter methods:** `accept_call()`, `reject_call()`, `end_call()`, `mute_call()`
3. **Persistent media session:** Long-lived RTP stream, not request/response
4. **Audio pipeline:** Microphone input → codec → RTP → network and network → RTP → codec → speaker

This is a **major architectural extension** to the gateway, not a simple plugin addition.

---

## 6. Complexity estimate

If pursued, the work breaks into four layers:

| Layer | Effort | Description |
|-------|--------|-------------|
| **L1: Jingle signalling** | 1–2 weeks | Integrate `slixmpp-jingle`, handle session-initiate/accept/terminate events in the adapter |
| **L2: Media transport** | 3–4 weeks | Bridge `aiortc` ↔ `slixmpp-jingle`. Implement ICE candidate exchange, DTLS handshake, RTP packet flow |
| **L3: Audio pipeline** | 2–3 weeks | Audio capture (mic/file/TTS), Opus encoding, playback routing. Depends on deployment target (server vs desktop) |
| **L4: Gateway integration** | 2–3 weeks | Extend BasePlatformAdapter with call lifecycle events, expose to agent ("call user X", "hang up"), handle multi-call state |

**Total estimated effort: 8–12 weeks** for a minimally functional voice call feature.

Major risk: `slixmpp-jingle` is immature and may need patches for real-world interop (Conversations, Dino, Gajim).

---

## 7. Recommendation

### 🟡 Defer — pursue later, not now

**Rationale:**

1. **No production-ready library exists.** `slixmpp-jingle` is signalling-only, undocumented, and appears to be a personal/research project. It would need significant hardening and a media transport bridge before any voice call works.

2. **Hermes lacks the call abstraction.** The gateway is message-oriented. Voice calls require a session-oriented model with persistent media streams — a major architectural change to `BasePlatformAdapter`.

3. **Deployment model mismatch.** Hermes typically runs headless on a server. Voice calls imply real-time audio I/O (microphone, speaker) that doesn't map cleanly to a server-side LLM gateway. The more natural path for "voice" in Hermes is TTS/STT over text messages, which already works.

4. **Interop testing is expensive.** Jingle RTP calls must interoperate with Conversations (Android), Dino (Linux), Gajim (Linux), and Monal (iOS). Each has quirks. Testing requires multiple devices and accounts.

**When to revisit:**

- If `slixmpp-jingle` matures (gets a public repo, tests, documentation, real users)
- If a `slixmpp-aiortc` bridge project emerges
- If Hermes gains a WebRTC gateway (browser-based live audio) that could reuse Jingle signalling
- If user demand for live XMPP voice calls becomes a top request

**Near-term alternative:** Enhance the existing `send_voice()` + TTS pipeline for higher-quality asynchronous voice messaging, which works within the current architecture and doesn't require Jingle at all.
