"""End-to-end: a fake Twilio client talks to the real /media-stream WebSocket over the network.

STT, LLM and TTS are deterministic fakes, everything else (session orchestration, barge-in,
marks, hangup, token check) is the production code path.
"""
import asyncio
import base64
import json
import socket
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import uvicorn
from fastapi import FastAPI, WebSocket
from websockets.asyncio.client import connect

from app.call_session import CallSession, stream_token
from app.config import Settings
from app.events import EventBus
from app.llm import ScriptedLLM
from app.profiles import ClinicProfile
from app.scenario import load_scenario
from app.scheduling import ClinicSchedule
from app.stt import FakeSTT
from app.tts import FakeTTS

NOW = datetime(2026, 10, 5, 9, 0, tzinfo=ZoneInfo("America/Chicago"))


class Harness:
    def __init__(self, llm, tts=None, silence_timeout=30, profile_factory=None):
        self.scenario = load_scenario("clinic")
        self.schedule = ClinicSchedule.build(self.scenario.timezone, self.scenario.raw["demo_records"], now=NOW)
        clinic = ClinicProfile(self.scenario, self.schedule)
        self.profile_factory = profile_factory or (lambda params: clinic)
        self.settings = Settings()
        self.settings.state_tracker_enabled = False
        self.settings.silence_timeout_s = silence_timeout
        self.settings.stream_secret = "test-secret"
        self.llm, self.tts, self.bus = llm, tts or FakeTTS(), EventBus()
        self.sessions: list[CallSession] = []
        app = FastAPI()

        @app.websocket("/media-stream")
        async def media(ws: WebSocket):
            await ws.accept()
            s = CallSession(ws, profile_factory=self.profile_factory, llm=self.llm, tts=self.tts,
                            stt_factory=FakeSTT, bus=self.bus, settings=self.settings)
            self.sessions.append(s)
            await s.run()

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        self.server = uvicorn.Server(uvicorn.Config(app, port=self.port, log_level="warning", lifespan="off"))

    async def __aenter__(self):
        self.task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.01)
        return self

    async def __aexit__(self, *exc):
        self.server.should_exit = True
        await self.task

    def types(self):
        return [e["type"] for e in self.bus.recent()]


class FakeTwilio:
    """Plays the Twilio side of a bidirectional Media Stream.

    A background reader consumes server messages. With `play=True`, marks are echoed back
    right away (the audio 'finished playing'); with `play=False` they are held like audio
    still buffered on the phone. On "clear", held marks are echoed, exactly like Twilio."""

    def __init__(self, ws, call_sid="CA0001"):
        self.ws, self.call_sid = ws, call_sid
        self.media_bytes = 0
        self.play = True
        self.held_marks: list[str] = []
        self.received: list[dict] = []
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.closed = asyncio.Event()
        self._reader: asyncio.Task | None = None

    async def start(self, secret="test-secret", **params):
        self._reader = asyncio.create_task(self._read())
        parts = [params.get(k, "") for k in ("campaign", "contact", "mode")]
        await self.ws.send(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
        await self.ws.send(json.dumps({"event": "start", "streamSid": "MZ1", "start": {
            "streamSid": "MZ1", "callSid": self.call_sid, "tracks": ["inbound"],
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
            "customParameters": {"token": stream_token(secret, self.call_sid, *parts), "caller": "+12105550100",
                                 **params}}}))

    async def _read(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                self.received.append(msg)
                if msg["event"] == "media":
                    self.media_bytes += len(base64.b64decode(msg["media"]["payload"]))
                elif msg["event"] == "mark":
                    if self.play:
                        await self.ack(msg["mark"]["name"])
                    else:
                        self.held_marks.append(msg["mark"]["name"])
                elif msg["event"] == "clear":
                    for name in self.held_marks:
                        await self.ack(name)
                    self.held_marks.clear()
                await self.inbox.put(msg)
        except Exception:
            pass
        finally:
            self.closed.set()

    async def ack(self, name):
        await self.ws.send(json.dumps({"event": "mark", "streamSid": "MZ1", "mark": {"name": name}}))

    async def _media(self, raw: bytes):
        await self.ws.send(json.dumps({"event": "media", "streamSid": "MZ1",
                                       "media": {"track": "inbound", "payload": base64.b64encode(raw).decode()}}))

    async def say(self, text):
        await self._media(b"FINAL:" + text.encode())

    async def interim(self, text):
        await self._media(b"INTERIM:" + text.encode())

    async def expect(self, pred, timeout=5.0):
        async def loop():
            while True:
                msg = await self.inbox.get()
                if pred(msg):
                    return msg
        return await asyncio.wait_for(loop(), timeout)

    def drain(self):
        while not self.inbox.empty():
            self.inbox.get_nowait()

    async def wait_closed(self, timeout=5.0):
        await asyncio.wait_for(self.closed.wait(), timeout)


def is_mark(msg):
    return msg["event"] == "mark"


async def wait_for(pred, timeout=3.0):
    async def loop():
        while not pred():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(loop(), timeout)


async def start_call(h: Harness, ws) -> tuple[FakeTwilio, CallSession]:
    tw = FakeTwilio(ws)
    await tw.start()
    await tw.expect(is_mark)  # opening line was sent
    session = h.sessions[0]
    await wait_for(lambda: session._agent_idle_since is not None)
    tw.drain()
    return tw, session


async def test_full_booking_call_then_agent_hangs_up():
    llm = ScriptedLLM([
        ["Sorry you're not feeling well. What's the patient's full name?"],
        [{"tool": "check_availability", "args": {"visit_type": "sick_visit", "patient_age_group": "adult"}},
         "I have an opening with Priya Shah, NP. Does that work?"],
        [{"tool": "book_appointment", "args": {"slot_id": "PLACEHOLDER", "patient_name": "Alex Kim",
                                               "date_of_birth": "1990-01-02", "visit_type": "sick_visit"}},
         "You're all set. Anything else?"],
        [{"tool": "end_call", "args": {"reason": "caller done"}}, "Feel better soon. Bye!"],
    ])
    async with Harness(llm) as h:
        # Make the scripted booking use a real open slot.
        slot = h.schedule.check_availability("sick_visit", patient_age_group="adult")["available"][0]["slot_id"]
        llm.script[2][0]["args"]["slot_id"] = slot

        async with connect(f"ws://127.0.0.1:{h.port}/media-stream") as ws:
            tw, session = await start_call(h, ws)
            assert tw.media_bytes > 0
            for line in ["I have a sore throat and need to be seen.", "Alex Kim, born January 2nd 1990.",
                         "Yes, book it.", "No, that's it. Thanks!"]:
                rid = session._rid
                await tw.say(line)
                await wait_for(lambda: session._rid > rid and not session.agent_busy())
            # The agent called end_call: the server closes the stream once the goodbye has played.
            await tw.wait_closed(3)

        state = session.state
        assert state.appointment and state.appointment["provider"] == h.schedule.slots[slot].provider
        assert h.schedule.slots[slot].booked
        assert state.end_requested
        roles = [t["role"] for t in state.transcript]
        assert roles[0] == "agent" and roles.count("caller") == 4
        types = h.types()
        for t in ("call_started", "tool_call", "metric", "hangup", "call_ended"):
            assert t in types, t
        spoken = " ".join(h.tts.spoken)
        assert "Riverbend" in spoken and "Bye!" in spoken


async def test_barge_in_clears_audio_and_records_what_was_heard():
    long_reply = ("Our parking garage is behind the building. Validate your ticket at the front desk. "
                  "Elevators are on the left. Suite three hundred is on the third floor. We open at eight.")
    llm = ScriptedLLM([[long_reply], ["Got it, you want to reschedule. What's the patient's name?"]])
    async with Harness(llm) as h:
        async with connect(f"ws://127.0.0.1:{h.port}/media-stream") as ws:
            tw, session = await start_call(h, ws)
            tw.play = False  # audio now stays "buffered on the phone" until we ack it
            await tw.say("Where do I park?")
            first = await tw.expect(is_mark)
            tw.held_marks.remove(first["mark"]["name"])
            await tw.ack(first["mark"]["name"])  # caller heard sentence one...
            await tw.expect(is_mark)  # ...sentence two is queued but not heard yet
            await wait_for(lambda: session._reply.played == {0})

            await tw.interim("actually wait")
            await tw.expect(lambda m: m["event"] == "clear")
            tw.play = True
            rid = session._rid
            await tw.say("actually wait, I need to reschedule")
            await wait_for(lambda: session._rid > rid and not session.agent_busy())

        assert session.state.interruptions == 1
        interrupted = [m for m in session.agent.messages if m["role"] == "assistant"
                       and "[caller interrupted here]" in (m["content"] or "")]
        assert interrupted and interrupted[0]["content"].startswith("Our parking garage is behind the building.")
        assert "Elevators" not in interrupted[0]["content"]
        barge = [e for e in h.bus.recent() if e["type"] == "barge_in"]
        assert barge and barge[0]["heard"] == "Our parking garage is behind the building."


async def test_split_turn_is_merged_when_caller_keeps_talking():
    llm = ScriptedLLM([["Okay."], ["Sure, I can help book for your son. What's his name?"]], token_delay=0.15)
    async with Harness(llm) as h:
        async with connect(f"ws://127.0.0.1:{h.port}/media-stream") as ws:
            tw, session = await start_call(h, ws)
            await tw.say("I need to book an appointment")
            await asyncio.sleep(0.05)  # LLM still "thinking", nothing spoken yet
            rid = session._rid
            await tw.say("for my son")
            await wait_for(lambda: session._rid > rid and session._reply.audio_started and not session.agent_busy())

        users = [m["content"] for m in session.agent.messages if m["role"] == "user"]
        assert users == ["I need to book an appointment for my son"]
        assert any(e["type"] == "retract" for e in h.bus.recent())


async def test_silence_reprompt_then_disconnect():
    llm = ScriptedLLM([["Are you still there?"], ["Hello? Are you still on the line?"]])
    async with Harness(llm, silence_timeout=0.3) as h:
        h.settings.silence_timeout_s = 0.3
        async with connect(f"ws://127.0.0.1:{h.port}/media-stream") as ws:
            tw = FakeTwilio(ws)
            await tw.start()
            await tw.wait_closed(8)  # opening, two re-prompts, goodbye, then hang up
        session = h.sessions[0]
        events = [t["text"] for t in session.state.transcript if t["role"] == "event"]
        assert len(events) == 2 and "silent" in events[0]
        assert "disconnected" in " ".join(h.tts.spoken)


async def test_bad_stream_token_is_rejected():
    async with Harness(ScriptedLLM([])) as h:
        async with connect(f"ws://127.0.0.1:{h.port}/media-stream") as ws:
            tw = FakeTwilio(ws)
            await tw.start(secret="wrong")
            await tw.wait_closed(3)
        assert h.sessions[0].state is None and h.tts.spoken == []


async def test_back_to_back_finals_merge_without_touching_earlier_turns():
    llm = ScriptedLLM([["Sure, what can I help with?"], ["Okay."], ["Happy to help book for your son."]])
    async with Harness(llm) as h:
        async with connect(f"ws://127.0.0.1:{h.port}/media-stream") as ws:
            tw, session = await start_call(h, ws)
            rid = session._rid
            await tw.say("Hi there")
            await wait_for(lambda: session._rid > rid and not session.agent_busy())
            rid = session._rid
            # Both finals land in the same tick: the first reply never even starts.
            await tw.say("I need to book an appointment")
            await tw.say("for my son")
            await wait_for(lambda: session._rid > rid + 1 and not session.agent_busy())
        users = [m["content"] for m in session.agent.messages if m["role"] == "user"]
        assert users == ["Hi there", "I need to book an appointment for my son"]
        replies = [m["content"] for m in session.agent.messages if m["role"] == "assistant"]
        assert replies[1] == "Sure, what can I help with?"


async def test_stt_connection_drop_reconnects_and_call_continues():
    llm = ScriptedLLM([["Yes, I can still hear you."]])
    async with Harness(llm) as h:
        async with connect(f"ws://127.0.0.1:{h.port}/media-stream") as ws:
            tw, session = await start_call(h, ws)
            first_stt = session.stt
            await tw._media(b"DROP")
            await wait_for(lambda: session.stt is not first_stt)
            rid = session._rid
            await tw.say("Can you still hear me?")
            await wait_for(lambda: session._rid > rid and not session.agent_busy())
        assert first_stt.closed
        assert "Yes, I can still hear you." in h.tts.spoken
