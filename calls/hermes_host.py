"""Host for the Hermes gateway: STT/TTS through Hermes' own provider functions and agent turns
through the platform adapter, so tools, memory, approvals and the configured providers apply
exactly as in a text chat. Gateway modules are imported lazily; this file is only used inside
a running Hermes."""

import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime
from typing import Any

import numpy as np

from .audio import STT_RATE, decode_to_out_rate, wav_bytes
from .speech import HANGUP

CALL_PROMPT = (
    "This message is spoken on a live phone call and your reply is read aloud. Answer in one to "
    "three short sentences of plain words: no markdown, lists, links or code, and no closing "
    "notes about what you skipped or checked. If the caller says goodbye, bye bye or talk to you "
    f"later, end with {HANGUP}; a bare 'bye' is not enough (it is often a misheard 'by'). "
    "Always answer out loud: on a call never reply with [SILENT] or an empty message."
)
MAX_TITLE = 100  # Hermes' limit for a session title
NO_REPLY = "returned only a silence marker"  # the gateway's text for a bare [SILENT]
NUDGE = "[Your last reply was empty. Answer the caller out loud in one short sentence.] "
TURN_TIMEOUT = 180.0


class HermesSpeech:
    """Transcribe and synthesize with the providers configured in Hermes (`stt`, `tts`)."""

    async def transcribe(self, pcm: np.ndarray) -> str:
        from tools.transcription_tools import transcribe_audio  # type: ignore[import-not-found]

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "caller.wav")
            await asyncio.to_thread(self._write, path, wav_bytes(pcm, STT_RATE))
            result = await asyncio.to_thread(transcribe_audio, path, None, "gateway")
        error = str(result.get("error") or "")
        if not result.get("success") and "wrote no output file" in error and "no stdout" in error:
            return ""  # a command provider that hears no words writes nothing: not a failure
        if not result.get("success"):
            raise RuntimeError(f"STT failed: {error}")
        return " ".join(str(result.get("transcript", "")).split())

    async def synthesize(self, text: str) -> np.ndarray:
        from tools.tts_tool import text_to_speech_tool  # type: ignore[import-not-found]

        result = json.loads(await asyncio.to_thread(text_to_speech_tool, text))
        paths = result.get("file_paths") or [result.get("file_path")]
        if not result.get("success") or not paths[0]:
            raise RuntimeError(f"TTS failed: {result.get('error')}")
        try:
            return np.concatenate([await asyncio.to_thread(decode_to_out_rate, p) for p in paths])
        finally:
            for path in paths:
                await asyncio.to_thread(self._remove, path)

    @staticmethod
    def _write(path: str, data: bytes) -> None:
        with open(path, "wb") as handle:
            handle.write(data)

    @staticmethod
    def _remove(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            logging.debug("calls: could not remove %s", path)


class _Turn:
    def __init__(self, message_id: str) -> None:
        self.message_id = message_id
        self.parts: list[str] = []
        self.done: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    def finish(self) -> None:
        if not self.done.done():
            self.done.set_result("\n".join(self.parts).strip())


class HermesCallHost:
    """Runs call turns as normal DM turns of the peer and captures the reply instead of sending it.

    Capture rule: a `send()` whose `reply_to` is the turn's own message id (or has none) while a
    turn is pending for that chat. Progress messages and anything else go to the chat as usual.
    """

    def __init__(
        self,
        adapter: Any,
        callers: frozenset[str] = frozenset(),
        session: str = "per_call",
        rotate: str = "never",
    ) -> None:
        self.adapter = adapter
        self.session = session  # "per_call": a Hermes session per call; "per_peer": one per person
        self.rotate = rotate  # per_peer only: "never", "daily", "weekly" or "monthly"
        self._threads: dict[str, str] = {}  # call id -> its thread, fixed when the call starts
        self.callers = callers  # who may call or be called; empty: every allowed user
        self.speech = HermesSpeech()
        self._pending: dict[str, _Turn] = {}
        self._silent: set[str] = set()  # message ids whose replies stay out of the chat
        self._count = 0

    def authorized(self, bare_jid: str) -> bool:
        if bare_jid.lower() not in self.callable_jids():
            return False  # e.g. a partner agent that may chat but must never phone
        return self.adapter._is_authorized(chat_type="dm", chat_id=bare_jid, user_jid=bare_jid)

    def callable_jids(self) -> set[str]:
        """Who may call or be called: `calls.allowed_users` if set, else every allowed user
        except the partner agents (`plaintext_peers`)."""
        if self.callers:
            return set(self.callers)
        peers = getattr(self.adapter, "_plaintext_peers", frozenset()) | getattr(
            self.adapter, "_agent_peers", frozenset()
        )
        return {j.lower() for j in self.adapter.allowed_users} - set(peers)

    async def ask(self, peer: str, call_id: str, text: str) -> str:
        reply = await self._ask_once(peer, call_id, text)
        if NO_REPLY in reply or not reply.strip():  # never speak the gateway's warning
            reply = await self._ask_once(peer, call_id, NUDGE + text)
            if NO_REPLY in reply:
                return ""
        return reply

    async def _ask_once(self, peer: str, call_id: str, text: str) -> str:
        from gateway.platforms.base import (  # type: ignore[import-not-found]
            MessageEvent,
            MessageType,
        )

        self._count += 1
        turn = self._pending[peer] = _Turn(f"call-{call_id}-{self._count}")
        source = self._source(peer, call_id)
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=turn.message_id,
            channel_prompt=CALL_PROMPT,
        )
        try:
            await self.adapter.handle_message(event)
            return await asyncio.wait_for(turn.done, TURN_TIMEOUT)
        finally:
            if self._pending.get(peer) is turn:
                del self._pending[peer]

    def _source(self, peer: str, call_id: str) -> Any:
        # A thread id keeps the calls out of the person's text chat session: one thread per call,
        # or one shared thread for all their calls. Either way the session lists its full history.
        thread = self._threads.get(call_id) or self._thread(call_id)
        return self.adapter.build_source(
            chat_id=peer, chat_type="dm", user_id=peer, user_name=None, thread_id=thread
        )

    def _thread(self, call_id: str) -> str:
        """The call's thread, fixed on first use: a call that runs past midnight (or into a new
        week) stays in the session it started in. `per_peer` rotation puts the period in the
        name (`calls-2026-W46`), so each period is a new session; Hermes then numbers its title."""
        if self.session != "per_peer":
            thread = f"call-{call_id}"
        else:
            now = datetime.now()
            period = {
                "daily": now.strftime("-%Y-%m-%d"),
                "weekly": f"-{now.isocalendar().year}-W{now.isocalendar().week:02d}",
                "monthly": now.strftime("-%Y-%m"),
            }.get(self.rotate, "")
            thread = f"calls{period}"
        while len(self._threads) >= 64:  # only calls in progress matter
            del self._threads[next(iter(self._threads))]
        self._threads[call_id] = thread
        return thread

    async def steer(self, peer: str, call_id: str, text: str) -> bool:
        """Mid-turn speech goes to the running agent as `/steer`. Unverified against a live
        gateway: any failure returns False and the speech is queued as the next turn."""
        from gateway.platforms.base import (  # type: ignore[import-not-found]
            MessageEvent,
            MessageType,
        )

        if peer not in self._pending:
            return False
        try:
            self._count += 1
            message_id = f"call-steer-{call_id}-{self._count}"
            self._silent.add(message_id)
            await self.adapter.handle_message(
                MessageEvent(
                    text=f"/steer {text}",
                    message_type=MessageType.TEXT,
                    source=self._source(peer, call_id),
                    message_id=message_id,
                )
            )
        except Exception:
            logging.exception("calls: steer failed; queueing instead")
            return False
        return True

    async def _free_title(self, source: Any, title: str) -> str | None:
        """Hermes keeps session titles unique and refuses a taken one (silently here), so ask first.
        None: the call's session already has a title, keep it (the next call of a per_peer person,
        or a session that was reset and is titled again). Else the title, or Hermes' own numbering
        ("title #2") when another session has it. Without the gateway's session database (older
        Hermes) the title is sent as it is."""
        runner = getattr(self.adapter, "gateway_runner", None)
        db = getattr(runner, "_session_db", None)
        store = getattr(runner, "async_session_store", None)
        if db is None or store is None:
            return title
        try:
            from hermes_state import SessionDB  # type: ignore[import-not-found]

            title = SessionDB.sanitize_title(title) or title
            entry = await store.get_or_create_session(source)
            if await db.get_session_title(entry.session_id):
                return None
            free = str(await db.get_next_title_in_lineage(title))
            if len(free) > MAX_TITLE:  # " #2" does not fit: shorten the base, then number it
                free = str(await db.get_next_title_in_lineage(title[: MAX_TITLE - 5]))[:MAX_TITLE]
            return free
        except Exception:
            logging.debug(
                "calls: could not look up the session title; sending it as it is", exc_info=True
            )
            return title

    async def name_call(self, peer: str, call_id: str, title: str) -> None:
        """Title the call's session with the built-in /title command; its acknowledgement is
        not sent to the chat. Needs a live gateway: any failure is logged and ignored."""
        from gateway.platforms.base import (  # type: ignore[import-not-found]
            MessageEvent,
            MessageType,
        )

        message_id = f"call-title-{call_id}"
        self._silent.add(message_id)
        try:
            source = self._source(peer, call_id)
            if (free := await self._free_title(source, title)) is None:
                return
            await self.adapter.handle_message(
                MessageEvent(
                    text=f"/title {free}",
                    message_type=MessageType.COMMAND,
                    source=source,
                    message_id=message_id,
                )
            )
        except Exception:
            logging.exception("calls: titling the call session failed")

    async def post_text(self, peer: str, text: str) -> None:
        # The sentinel reply_to keeps this out of a call turn that is being captured for speech.
        await self.adapter.send(chat_id=peer, content=text, reply_to="xmpp-calls-text")

    def capture(self, chat_id: str, content: str, reply_to: str | None) -> bool:
        """Called from the adapter's send(). True means the text was taken for speech."""
        if reply_to in self._silent:
            logging.info("calls: not sending %r to the chat (reply to %s)", content[:80], reply_to)
            return True  # e.g. the acknowledgement of a /title or /steer
        turn = self._pending.get(chat_id)
        if turn is None or (reply_to and reply_to != turn.message_id):
            return False  # includes acknowledgements of a /steer: they stay text in the chat
        turn.parts.append(content)
        return True

    def complete(self, chat_id: str, message_id: str | None) -> None:
        """Called from the adapter's on_processing_complete: the turn's reply is whole."""
        turn = self._pending.get(chat_id)
        if turn is not None and message_id == turn.message_id:
            turn.finish()
