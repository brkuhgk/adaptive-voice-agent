"""One live phone call: Twilio Media Stream <-> Deepgram <-> Agent (LLM) <-> ElevenLabs.

    caller audio ──► Twilio ──► /media-stream ──► Deepgram STT ──► turn text
                                                                     │
                       ┌───────────── Agent (state + context + LLM) ◄┘
                       ▼
     sentence chunks ──► ElevenLabs TTS (mu-law) ──► Twilio ──► caller hears it

Also handles: barge-in (caller talks over the agent), merging a caller turn that was split
by a pause, silence re-prompts, a max call length, and hanging up after the goodbye plays.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable

from fastapi import WebSocket, WebSocketDisconnect

from .agent import Agent
from .config import Settings
from .events import EventBus
from .llm import LLMClient
from .profiles import AgentProfile
from .sentence import SentenceChunker
from .state import ConversationState
from .stt import STTClient, STTEvent
from .tts import TTSClient

log = logging.getLogger(__name__)

BACKCHANNELS = {"uh huh", "uh-huh", "mhm", "mm hmm", "mm-hmm", "hmm", "mm", "uh", "um", "ah"}
TROUBLE_LINE = "Sorry, I'm having technical trouble on my end. Please try calling back in a minute. Goodbye!"
DISCONNECT_LINE = "It sounds like we got disconnected. Feel free to call back anytime. Goodbye!"


STREAM_PARAMS = ("campaign", "contact", "mode")


def stream_token(secret: str, call_sid: str, *parts: str) -> str:
    """HMAC over the CallSid (and campaign/contact/mode for outbound calls), so only streams
    started by our own TwiML are accepted, and their parameters can't be swapped."""
    msg = "|".join([call_sid, *parts]) if any(parts) else call_sid
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()[:32]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z\- ]", "", text.lower()).strip()


@dataclass
class Reply:
    rid: int
    triggered_by_user: bool
    segments: list[str] = field(default_factory=list)
    played: set[int] = field(default_factory=set)
    audio_started: bool = False
    first_token_seen: bool = False


class CallSession:
    def __init__(self, ws: WebSocket, *, profile_factory: Callable[[dict], AgentProfile], llm: LLMClient | None,
                 tts: TTSClient | None, stt_factory: Callable[[list[str]], STTClient], bus: EventBus,
                 settings: Settings, require_token: bool = True):
        self.ws = ws
        self.profile_factory = profile_factory
        self.profile: AgentProfile | None = None
        self.llm = llm
        self.tts = tts
        self.stt_factory = stt_factory
        self.bus = bus
        self.settings = settings
        self.require_token = require_token

        self.stream_sid: str | None = None
        self.call_id = "pending"
        self.state: ConversationState | None = None
        self.agent: Agent | None = None
        self.stt: STTClient | None = None

        self._send_lock = asyncio.Lock()
        self._bg: set[asyncio.Task] = set()
        self._reply: Reply | None = None
        self._reply_task: asyncio.Task | None = None
        self._rid = 0
        self._pending_marks: set[str] = set()
        self._hangup_task: asyncio.Task | None = None
        self._hanging_up = False
        self._closed = False

        self._turn_end_ts: float | None = None
        self._agent_idle_since: float | None = None
        self._last_user_activity = time.monotonic()
        self._silence_count = 0
        self._wrapup_sent = False
        self._t0 = time.monotonic()

    # ================================================================ plumbing
    def _publish(self, type_: str, **data) -> None:
        self.bus.publish(self.call_id, type_, **data)

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task

    async def _send(self, payload: dict) -> None:
        if self._closed:
            return
        async with self._send_lock:
            try:
                await self.ws.send_text(json.dumps(payload))
            except Exception:  # socket already gone
                self._closed = True

    async def _send_media(self, audio: bytes) -> None:
        for i in range(0, len(audio), 16000):
            await self._send({"event": "media", "streamSid": self.stream_sid,
                              "media": {"payload": base64.b64encode(audio[i:i + 16000]).decode()}})

    async def _send_mark(self, name: str) -> None:
        await self._send({"event": "mark", "streamSid": self.stream_sid, "mark": {"name": name}})

    def agent_busy(self) -> bool:
        return bool(self._pending_marks) or (self._reply_task is not None and not self._reply_task.done())

    def agent_audible(self) -> bool:
        return self._reply is not None and self._reply.audio_started and self.agent_busy()

    # ================================================================ main loop
    async def run(self) -> None:
        try:
            while True:
                msg = json.loads(await self.ws.receive_text())
                event = msg.get("event")
                if event == "media":
                    if self.stt is not None and msg["media"].get("track", "inbound") == "inbound":
                        await self.stt.send_audio(base64.b64decode(msg["media"]["payload"]))
                elif event == "start":
                    if not await self._on_start(msg):
                        break
                elif event == "mark":
                    self._on_mark(msg.get("mark", {}).get("name", ""))
                elif event == "dtmf":
                    await self._on_dtmf(str(msg.get("dtmf", {}).get("digit", "")))
                elif event == "stop":
                    break
        except (WebSocketDisconnect, RuntimeError):
            pass
        except Exception:
            log.exception("call loop crashed")
        finally:
            await self._cleanup()

    async def _on_start(self, msg: dict) -> bool:
        start = msg.get("start", {})
        self.stream_sid = start.get("streamSid") or msg.get("streamSid")
        call_sid = start.get("callSid") or self.stream_sid or "unknown"
        params = start.get("customParameters") or {}
        expected = stream_token(self.settings.stream_secret, call_sid, *(params.get(k, "") for k in STREAM_PARAMS))
        if self.require_token and not hmac.compare_digest(params.get("token", ""), expected):
            log.warning("rejected media stream with bad token for call %s", call_sid)
            await self._close_ws()
            return False
        try:
            self.profile = profile = self.profile_factory(params)
        except Exception as exc:
            log.warning("no profile for stream %s: %s", call_sid, exc)
            await self._close_ws()
            return False

        self.call_id = call_sid
        self.state = profile.new_state(call_sid, params.get("caller"))
        who = self.state.labels.get("contact_name") or params.get("caller")
        self._publish("call_started", profile=profile.kind, caller=who, agent=profile.agent_name,
                      org=profile.org_name, labels=self.state.labels)

        if self.llm is None or self.tts is None:
            log.error("LLM or TTS not configured; ending call")
            self._publish("error", where="setup", detail="LLM or TTS is not configured (check .env)")
            await self._close_ws()
            return False

        self.agent = Agent(profile, self.state, self.llm, self.bus, self.settings)
        self.agent.publish_state("start")
        if params.get("mode") == "voicemail" and profile.voicemail_text:
            # Twilio already waited for the beep: leave the message and hang up. No listening needed.
            self.state.end_requested = True
            self._start_fixed_reply(profile.voicemail_text)
            return True
        try:
            self.stt = self.stt_factory(profile.keyterms)
            await self.stt.start()
        except Exception as exc:
            log.exception("STT failed to start")
            self._publish("error", where="stt", detail=str(exc))
            self.stt = None
            self.state.end_requested = True
            self._start_fixed_reply(TROUBLE_LINE)
            return True

        self._spawn(self._stt_loop())
        self._spawn(self._watchdog())
        self._start_fixed_reply(profile.opening_line)
        return True

    async def _on_dtmf(self, digit: str) -> None:
        self._publish("dtmf", digit=digit)
        p, state = self.profile, self.state
        if p is None or state is None or self.agent is None or not p.opt_out_digit or digit != p.opt_out_digit:
            return
        if state.opted_out or self._hanging_up:
            return
        # Keypad opt-out: stop talking, record it, confirm, hang up.
        if self.agent_busy():
            await self._interrupt("keypad opt-out")
        p.opt_out(state, "keypad")
        state.end_requested = True
        self.agent.publish_state("keypad")
        self._start_fixed_reply("Okay, I've removed your number and we won't call again. Sorry to bother you. Goodbye.")

    # ================================================================ speaking
    def _start_fixed_reply(self, text: str) -> None:
        assert self.agent is not None
        self.agent.add_assistant_text(text)

        async def once() -> AsyncIterator[str]:
            yield text

        self._start_reply(once(), triggered_by_user=False)

    def _start_reply(self, source: AsyncIterator[str], *, triggered_by_user: bool) -> None:
        self._rid += 1
        self._reply = Reply(rid=self._rid, triggered_by_user=triggered_by_user)
        self._agent_idle_since = None
        self._reply_task = asyncio.create_task(self._run_reply(self._reply, source))

    async def _run_reply(self, reply: Reply, source: AsyncIterator[str]) -> None:
        sentences: asyncio.Queue[str | None] = asyncio.Queue()

        async def produce() -> None:
            chunker = SentenceChunker()
            try:
                async for piece in source:
                    if not reply.first_token_seen:
                        reply.first_token_seen = True
                        if reply.triggered_by_user and self._turn_end_ts:
                            self._publish("metric", rid=reply.rid, name="llm_first_token_ms",
                                          value=round((time.monotonic() - self._turn_end_ts) * 1000))
                    for s in chunker.push(piece):
                        sentences.put_nowait(s)
                for s in chunker.flush():
                    sentences.put_nowait(s)
            finally:
                await source.aclose()
                sentences.put_nowait(None)

        async def consume() -> None:
            assert self.tts is not None
            previous: str | None = None
            while (sentence := await sentences.get()) is not None:
                idx = len(reply.segments)
                reply.segments.append(sentence)
                try:
                    voice = self.profile.voice_id if self.profile else None
                    async for audio in self.tts.stream(sentence, previous_text=previous, voice_id=voice):
                        if not reply.audio_started:
                            reply.audio_started = True
                            if reply.triggered_by_user and self._turn_end_ts:
                                self._publish("metric", rid=reply.rid, name="first_audio_ms",
                                              value=round((time.monotonic() - self._turn_end_ts) * 1000))
                        await self._send_media(audio)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.exception("TTS failed")
                    self._publish("error", where="tts", detail=str(exc))
                    continue
                name = f"r{reply.rid}-{idx}"
                self._pending_marks.add(name)
                await self._send_mark(name)
                self._publish("agent_said", rid=reply.rid, text=sentence)
                previous = sentence

        producer, consumer = asyncio.create_task(produce()), asyncio.create_task(consume())
        try:
            await asyncio.gather(producer, consumer)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("reply pipeline failed")
            self._publish("error", where="reply", detail=str(exc))
        finally:
            for t in (producer, consumer):
                if not t.done():
                    t.cancel()
            await asyncio.gather(producer, consumer, return_exceptions=True)

        if self.state is not None and self.state.end_requested and not self._hanging_up:
            self._hangup_task = self._spawn(self._hangup_after_playback())
        if not self._pending_marks:
            self._mark_idle()

    async def _cancel_reply(self) -> None:
        task = self._reply_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    def _on_mark(self, name: str) -> None:
        if name not in self._pending_marks:
            return  # stale mark echoed back after a "clear"
        self._pending_marks.discard(name)
        m = re.fullmatch(r"r(\d+)-(\d+)", name)
        if m and self._reply is not None and int(m.group(1)) == self._reply.rid:
            self._reply.played.add(int(m.group(2)))
        if not self.agent_busy():
            self._mark_idle()

    def _mark_idle(self) -> None:
        self._agent_idle_since = time.monotonic()
        self._publish("agent_idle")

    async def _interrupt(self, reason: str) -> None:
        """Caller talked over the agent: stop audio now, record what they actually heard."""
        assert self.agent is not None and self.state is not None
        reply = self._reply
        heard = " ".join(reply.segments[i] for i in sorted(reply.played)) if reply else ""
        self._pending_marks.clear()
        await self._cancel_reply()  # stop producing audio first, then flush what Twilio has buffered
        await self._send({"event": "clear", "streamSid": self.stream_sid})
        if self._hangup_task is not None and not self._hangup_task.done() and not self.state.opted_out:
            self._hangup_task.cancel()  # "wait, one more thing" during the goodbye
            self.state.end_requested = False
        self.agent.record_interruption(heard)
        self._publish("barge_in", reason=reason, heard=heard)

    # ================================================================ listening
    async def _stt_loop(self) -> None:
        while self.stt is not None:
            ev: STTEvent = await self.stt.events.get()
            if self._hanging_up:
                continue
            try:
                await self._handle_stt(ev)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("error handling STT event %s", ev.kind)

    async def _reconnect_stt(self) -> None:
        self._publish("error", where="stt", detail="speech-to-text connection dropped; reconnecting")
        old = self.stt
        try:
            new = self.stt_factory(self.profile.keyterms if self.profile else [])
            await new.start()
            self.stt = new
        except Exception as exc:
            log.exception("STT reconnect failed")
            self._publish("error", where="stt", detail=f"reconnect failed: {exc}")
            return
        if old is not None:
            self._spawn(old.close())

    async def _handle_stt(self, ev: STTEvent) -> None:
        now = time.monotonic()
        if ev.kind == "speech_started":
            self._last_user_activity = now
        elif ev.kind == "interim":
            self._last_user_activity = now
            self._silence_count = 0
            self._publish("caption", text=ev.text)
            words = _norm(ev.text)
            if (self.agent_audible() and len(words) >= self.settings.barge_in_min_chars
                    and words not in BACKCHANNELS):
                await self._interrupt("caller started talking")
        elif ev.kind == "final":
            self._last_user_activity = now
            self._silence_count = 0
            await self._on_user_turn(ev.text)
        elif ev.kind == "error":
            self._publish("error", where="stt", detail=ev.text)
        elif ev.kind == "closed" and not self._closed:
            await self._reconnect_stt()

    async def _on_user_turn(self, text: str) -> None:
        assert self.agent is not None
        if _norm(text) in BACKCHANNELS and self.agent_audible():
            self._publish("backchannel", text=text)
            return
        merged = text
        thinking = (self._reply_task is not None and not self._reply_task.done()
                    and self._reply is not None and not self._reply.audio_started and self._reply.triggered_by_user)
        if thinking:
            # The caller paused mid-thought and kept going before we said anything:
            # drop the half-finished reply and answer the whole thing together.
            await self._cancel_reply()
            previous = self.agent.retract_last_turn()
            if previous:
                merged = f"{previous} {text}"
            else:
                self.agent.record_interruption("")
        elif self.agent_busy():
            await self._interrupt("caller spoke")
        self._turn_end_ts = time.monotonic()
        self._start_reply(self.agent.respond(merged), triggered_by_user=True)

    # ================================================================ timers
    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            try:
                self._watchdog_tick()
            except Exception:
                log.exception("watchdog tick failed")

    def _watchdog_tick(self) -> None:
        assert self.agent is not None and self.state is not None
        if self._hanging_up or self.agent_busy() or self._agent_idle_since is None:
            return
        now = time.monotonic()
        if self.profile is not None and now - self._t0 > self.profile.max_call_seconds and not self._wrapup_sent:
            self._wrapup_sent = True
            self._start_reply(self.agent.respond(
                "(call time limit reached: wrap up politely in one sentence and call end_call)", is_event=True),
                triggered_by_user=False)
            return
        quiet = now - max(self._agent_idle_since, self._last_user_activity)
        if quiet < self.settings.silence_timeout_s:
            return
        self._silence_count += 1
        if self._silence_count <= 2:
            self._start_reply(self.agent.respond(f"(caller silent for {int(quiet)} seconds)", is_event=True),
                              triggered_by_user=False)
        else:
            self.state.end_requested = True
            self._start_fixed_reply(DISCONNECT_LINE)

    async def _hangup_after_playback(self) -> None:
        deadline = time.monotonic() + 25
        while self._pending_marks and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.6)
        self._hanging_up = True
        self._publish("hangup", reason=self.state.end_reason if self.state else None)
        await self._close_ws()

    async def _close_ws(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self.ws.close()  # Twilio moves past <Connect>; with no more TwiML, the call ends
        except Exception:
            pass

    async def _cleanup(self) -> None:
        self._closed = True
        await self._cancel_reply()
        for t in list(self._bg):
            if t is not asyncio.current_task():
                t.cancel()
        await asyncio.gather(*[t for t in self._bg if t is not asyncio.current_task()], return_exceptions=True)
        if self.stt is not None:
            await self.stt.close()
        if self.agent is not None:
            await self.agent.close()
        if self.state is not None and self.profile is not None:
            try:
                self.profile.on_call_end(self.state)
            except Exception:
                log.exception("saving call results failed")
            self._publish("call_ended", state=self.state.to_dict(), transcript=self.state.transcript)
