"""Offline rehearsal: replays a realistic phone call through the real server + dashboard,
with fake STT/LLM/TTS. No API keys, no phone, no credits.

    python -m scripts.replay_demo              # inbound clinic call; open http://localhost:8000/dashboard
    python -m scripts.replay_demo --campaign   # outbound: Maya calls a contact from campaigns/demo
    python -m scripts.replay_demo --speed 2    # faster playback

It exercises the production call path: Twilio media-stream protocol, sentence chunking,
marks, barge-in with "clear", tool calls, state tracking, metrics, and the agent hanging up.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

os.environ.setdefault("LLM_API_KEY", "")
os.environ.setdefault("STREAM_SECRET", "replay-secret")

import uvicorn  # noqa: E402
from websockets.asyncio.client import connect  # noqa: E402

import app.campaigns as campaigns_mod  # noqa: E402
from app import main  # noqa: E402
from app.call_session import stream_token  # noqa: E402
from app.llm import ScriptedLLM  # noqa: E402
from app.stt import FakeSTT  # noqa: E402
from app.tts import FakeTTS  # noqa: E402


def build_script(rt) -> tuple[list, list, list[tuple[str, str, float]]]:
    sched = rt.schedule
    morning = sched.check_availability("sick_visit", patient_age_group="adult", limit=2)["available"]
    afternoon = sched.check_availability("sick_visit", patient_age_group="adult", time_of_day="afternoon", limit=1)["available"]
    a1, a2 = morning[0]["description"], morning[1]["description"]
    pm = afternoon[0]
    llm_script = [
        ["Oh no, sorry you're feeling rough. Is the visit for you, and can I get your full name?"],
        ["Thanks, Alex. What's your date of birth?"],
        ["Got it. And how high has the fever been?"],
        [{"tool": "check_availability", "args": {"visit_type": "sick_visit", "patient_age_group": "adult"}},
         f"Okay, that sounds like a sick visit. The earliest I have is {a1}, and after that, {a2}. "
         "Both are in the morning, so you'd want to plan to arrive about ten minutes early for check-in."],
        [{"tool": "check_availability", "args": {"visit_type": "sick_visit", "patient_age_group": "adult",
                                                 "time_of_day": "afternoon"}},
         f"No problem. I can do {pm['description']}. Want me to grab that one?"],
        ["Great. And what's the best callback number for you?"],
        [{"tool": "book_appointment", "args": {"slot_id": pm["slot_id"], "patient_name": "Alex Kim",
                                               "date_of_birth": "1990-01-02", "visit_type": "sick_visit",
                                               "reason_for_visit": "fever and sore throat",
                                               "callback_number": "2105550199"}},
         "You're all set, Alex. Bring a photo ID and your insurance card. Anything else I can help with?"],
        [{"tool": "end_call", "args": {"reason": "caller is done"}}, "Feel better soon. Bye!"],
    ]
    tracker = [
        {"intent": "new_appointment", "reason_for_visit": "fever and sore throat since Tuesday", "visit_type": "sick_visit",
         "caller_sentiment": "tired", "next_best_action": "Confirm who the patient is and get their full name."},
        {"patient_name": "Alex Kim", "caller_sentiment": "calm", "next_best_action": "Ask for date of birth."},
        {"date_of_birth": "1990-01-02", "next_best_action": "Check fever severity for same-day triage, then find a slot."},
        {"caller_sentiment": "calm", "next_best_action": "Offer the earliest sick visit for an adult."},
        {"preferred_time": "afternoon", "next_best_action": "Offer an afternoon sick visit."},
        {"next_best_action": "Get the callback number, then read back and book."},
        {"callback_number": "2105550199", "next_best_action": "Book the slot and confirm."},
        {"caller_sentiment": "happy", "next_best_action": "Say goodbye and end the call."},
    ]
    # (kind, text, seconds to wait before sending)
    caller = [
        ("final", "Hi, I've had a fever and a sore throat since Tuesday. Can I get seen?", 0.8),
        ("final", "Yeah, it's for me. Alex Kim.", 0.6),
        ("final", "January second, 1990.", 0.6),
        ("final", "About a hundred and one.", 0.6),
        ("barge", "Actually, do you have anything in the afternoon?", 0.0),
        ("final", "Yes please.", 0.6),
        ("final", "210 555 0199.", 0.6),
        ("final", "No, that's it. Thanks!", 0.6),
    ]
    return llm_script, tracker, caller


def build_campaign_script() -> tuple[list, list, list[tuple[str, str, float]]]:
    llm_script = [
        ["Great! I'm calling to check if you're still coming to Hack Night on Friday, November sixth. "
         "Are you still planning to make it?"],
        ["Awesome. It's in the Student Union Ballroom on the second floor, and check-in opens at five thirty. "
         "What t-shirt size should we save for you?"],
        ["Got it, a medium. Any food allergies or dietary needs we should know about?"],
        [{"tool": "end_call", "args": {"outcome": "completed",
                                       "summary": "Coming; t-shirt M; vegetarian. Asked where the event is."}},
         "Perfect. So you're coming, a medium shirt, and vegetarian food. See you at Hack Night, Alex. Bye!"],
    ]
    tracker = [
        {"caller_sentiment": "friendly", "next_best_action": "Explain why you're calling and ask if they're coming."},
        {"attending": "yes", "next_best_action": "Answer the location question, then ask t-shirt size."},
        {"tshirt_size": "M", "next_best_action": "Ask about dietary needs."},
        {"dietary_needs": "vegetarian", "caller_sentiment": "happy", "next_best_action": "Read back and end the call."},
    ]
    caller = [
        ("final", "Yeah, this is Alex.", 0.6),
        ("final", "Yes, I'll be there. Where is it again?", 0.6),
        ("final", "Medium, please.", 0.6),
        ("final", "I'm vegetarian.", 0.6),
    ]
    return llm_script, tracker, caller


def use_temp_campaign() -> tuple[str, str]:
    """Copy campaigns/demo to a temp folder with a fake contact, so the replay never touches your files."""
    root = Path(tempfile.mkdtemp(prefix="campaigns-replay-"))
    shutil.copytree(campaigns_mod.CAMPAIGN_DIR / "demo", root / "demo")
    (root / "demo" / "contacts.csv").write_text(
        "phone,name,consent,timezone,voice_id,team\n+15555550100,Alex Garcia,yes,America/Chicago,,Team Rowdy\n")
    campaigns_mod.CAMPAIGN_DIR = root
    main.rt.dnc = campaigns_mod.DoNotCallList(root / "do_not_call.txt")
    main.rt.campaigns = {}
    print(f"Replay campaign files and results: {root / 'demo'}")
    return "demo", "15555550100"


class PhoneSimulator:
    """Twilio side: plays audio in real time (8000 bytes = 1 s) and acks marks when 'heard'."""

    def __init__(self, ws, speed: float):
        self.ws, self.speed = ws, speed
        self.play_until = time.monotonic()
        self.pending: list[asyncio.Task] = []
        self.media_started = asyncio.Event()
        self.closed = asyncio.Event()
        self.last_media = time.monotonic()

    async def reader(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if msg["event"] == "media":
                    secs = len(base64.b64decode(msg["media"]["payload"])) / 8000 / self.speed
                    self.play_until = max(self.play_until, time.monotonic()) + secs
                    self.last_media = time.monotonic()
                    self.media_started.set()
                elif msg["event"] == "mark":
                    delay = max(0.0, self.play_until - time.monotonic())
                    self.pending.append(asyncio.create_task(self._ack_later(msg["mark"]["name"], delay)))
                elif msg["event"] == "clear":
                    self.play_until = time.monotonic()
                    for t in self.pending:
                        t.cancel()
                    self.pending.clear()
        except Exception:
            pass
        finally:
            self.closed.set()

    async def _ack_later(self, name, delay):
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            pass  # Twilio still echoes cleared marks
        await self.ws.send(json.dumps({"event": "mark", "streamSid": "MZdemo", "mark": {"name": name}}))

    async def send(self, prefix: str, text: str):
        payload = base64.b64encode(f"{prefix}:{text}".encode()).decode()
        await self.ws.send(json.dumps({"event": "media", "streamSid": "MZdemo",
                                       "media": {"track": "inbound", "payload": payload}}))

    async def wait_quiet(self):
        """Wait until Maya has finished talking: audio played out and no new audio for a moment."""
        while (time.monotonic() < self.play_until or any(not t.done() for t in self.pending)
               or time.monotonic() - self.last_media < 1.0 / self.speed):
            await asyncio.sleep(0.05)


async def run(port: int, speed: float, wait: float, keep_open: bool, campaign: bool = False):
    rt = main.rt
    params: dict[str, str] = {}
    if campaign:
        name, contact = use_temp_campaign()
        params = {"campaign": name, "contact": contact, "mode": "live"}
        llm_script, tracker, caller = build_campaign_script()
    else:
        llm_script, tracker, caller = build_script(rt)
    rt.llm = ScriptedLLM(llm_script, token_delay=0.04 / speed, tracker=tracker)
    rt.tts = FakeTTS(chunk_delay=0.03 / speed)
    rt.stt_factory = lambda keyterms=None: FakeSTT(keyterms)
    main.settings.silence_timeout_s = 60
    main.settings.llm_provider, main.settings.llm_model = "replay", "scripted (offline rehearsal)"
    rt.problems = []

    server = uvicorn.Server(uvicorn.Config(main.app, port=port, log_level="warning"))
    srv = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    print(f"Dashboard: http://localhost:{port}/dashboard  (call starts in {wait:.0f}s)")
    await asyncio.sleep(wait)

    call_sid = "CAreplay0002" if campaign else "CAreplay0001"
    parts = [params.get(k, "") for k in ("campaign", "contact", "mode")]
    async with connect(f"ws://127.0.0.1:{port}/media-stream") as ws:
        phone = PhoneSimulator(ws, speed)
        reader = asyncio.create_task(phone.reader())
        await ws.send(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
        await ws.send(json.dumps({"event": "start", "streamSid": "MZdemo", "start": {
            "streamSid": "MZdemo", "callSid": call_sid, "tracks": ["inbound"],
            "customParameters": {"token": stream_token(main.settings.stream_secret, call_sid, *parts),
                                 "caller": "+1 (210) 555-0199", **params},
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}}}))
        await phone.media_started.wait()
        for kind, text, pause in caller:
            if kind == "barge":
                # The agent has started its long answer: talk over it ~5 s in.
                await asyncio.sleep(5.0 / speed)
                words = text.split()
                for i in range(2, len(words), 2):
                    await phone.send("INTERIM", " ".join(words[:i]))
                    await asyncio.sleep(0.25 / speed)
                await phone.send("FINAL", text)
                await asyncio.sleep(0.3)
                await phone.wait_quiet()
                continue
            await phone.wait_quiet()
            await asyncio.sleep(pause / speed)
            words = text.split()
            for i in range(2, len(words), 3):
                await phone.send("INTERIM", " ".join(words[:i]))
                await asyncio.sleep(0.2 / speed)
            await phone.send("FINAL", text)
            phone.media_started.clear()
            await phone.media_started.wait()
        await asyncio.wait_for(phone.closed.wait(), 30)
        reader.cancel()
    print("Call finished.", "Server still running, Ctrl+C to quit." if keep_open else "")
    if keep_open:
        await srv
    else:
        server.should_exit = True
        await srv


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--wait", type=float, default=4.0, help="seconds before the call starts")
    ap.add_argument("--exit", action="store_true", help="stop the server when the call ends")
    ap.add_argument("--campaign", action="store_true", help="replay an outbound campaign call instead")
    args = ap.parse_args()
    asyncio.run(run(args.port, args.speed, args.wait, not args.exit, args.campaign))
